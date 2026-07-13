import os
import re
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID


class SupabaseNotConfigured(RuntimeError):
    pass


TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
PRODUCT_FIELDS = {
    "name", "category", "subcategory", "description", "price", "image_url",
    "image_sha256", "obj_url", "model_url", "usdz_url",
}
LOGGER = logging.getLogger("furniture_ar.supabase")


@dataclass
class SaveOutcome:
    product: dict
    image_reused: bool
    previous_product: dict | None
    new_image_path: str | None


def validate_table_name(table_name: str) -> str:
    table_name = table_name.strip()
    if not table_name:
        raise ValueError("Please enter a Supabase table name.")
    if not TABLE_NAME_PATTERN.fullmatch(table_name):
        raise ValueError(
            "Table names may contain only letters, numbers, and underscores, "
            "and cannot start with a number."
        )
    return table_name


def _client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise SupabaseNotConfigured(
            "Set SUPABASE_URL and SUPABASE_KEY before saving products."
        )
    from supabase import create_client
    return create_client(url, key)


def save_product(*, conversion_id: UUID, product: dict, image_path: Path,
                 obj_path: Path, glb_path: Path, usdz_path: Path | None = None,
                 table_name: str) -> SaveOutcome:
    table_name = validate_table_name(table_name)
    client = _client()
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "products")
    model_prefix = f"models/{conversion_id}"
    uploaded_paths: list[str] = []

    def validate_public_url(value: str, asset_name: str) -> str:
        public_url = str(value)
        parsed = urlparse(public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Supabase returned an invalid public URL for {asset_name}.")
        return public_url

    def upload(path: Path, remote_name: str, content_type: str) -> str:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"Cannot upload missing or empty asset: {path.name}")
        remote_path = remote_name
        with path.open("rb") as file:
            client.storage.from_(bucket).upload(
                remote_path,
                file,
                {"content-type": content_type, "upsert": True},
            )
        uploaded_paths.append(remote_path)
        public_url = validate_public_url(
            client.storage.from_(bucket).get_public_url(remote_path), path.name,
        )
        LOGGER.info(
            "Supabase upload complete asset=%s bytes=%s content_type=%s public_url=%s",
            path.name, path.stat().st_size, content_type, public_url,
        )
        return public_url

    try:
        image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        existing_result = (
            client.table(table_name).select("*").eq("image_sha256", image_sha256)
            .limit(1).execute()
        )
        existing = existing_result.data[0] if existing_result.data else None
        image_object_name = f"{image_sha256}{image_path.suffix.lower()}"
        image_remote_path = f"images/{image_object_name}"
        stored_images = client.storage.from_(bucket).list(
            "images", {"search": image_object_name}
        )
        image_in_storage = any(item.get("name") == image_object_name for item in stored_images)
        new_image_path = None
        if existing:
            image_url = validate_public_url(existing["image_url"], image_path.name)
        elif image_in_storage:
            image_url = validate_public_url(
                client.storage.from_(bucket).get_public_url(image_remote_path), image_path.name,
            )
        else:
            image_types = {".png": "image/png", ".webp": "image/webp"}
            new_image_path = image_remote_path
            image_url = upload(
                image_path, new_image_path,
                image_types.get(image_path.suffix.lower(), "image/jpeg"),
            )
        obj_url = upload(obj_path, f"{model_prefix}/model.obj", "text/plain")
        glb_url = upload(glb_path, f"{model_prefix}/model.glb", "model/gltf-binary")
        usdz_url = None
        if usdz_path is not None and usdz_path.is_file():
            usdz_url = upload(
                usdz_path, f"{model_prefix}/model.usdz", "model/vnd.usdz+zip",
            )
        row = {
            **product,
            "image_url": image_url,
            "image_sha256": image_sha256,
            "obj_url": obj_url,
            "model_url": glb_url,
            "usdz_url": usdz_url,
        }
        if existing:
            previous_product = {key: existing[key] for key in PRODUCT_FIELDS if key in existing}
            result = client.table(table_name).update(row).eq("id", existing["id"]).execute()
        else:
            previous_product = None
            result = client.table(table_name).insert(row).execute()
        saved = result.data[0] if result.data else {**row, "id": existing.get("id") if existing else None}
        LOGGER.info(
            "Supabase row saved conversion_id=%s table=%s operation=%s model_url=%s usdz_url=%s",
            conversion_id, table_name, "update" if existing else "insert", glb_url, usdz_url,
        )
        return SaveOutcome(saved, bool(existing or image_in_storage), previous_product, new_image_path)
    except Exception:
        if uploaded_paths:
            try:
                client.storage.from_(bucket).remove(uploaded_paths)
                LOGGER.info("Cleaned up %s uploaded objects after failed save", len(uploaded_paths))
            except Exception:
                LOGGER.exception("Failed to clean uploaded objects after failed save")
        raise


def undo_product_save(*, conversion_id: UUID, table_name: str, product_id: str,
                      previous_product: dict | None,
                      new_image_path: str | None) -> None:
    table_name = validate_table_name(table_name)
    client = _client()
    if previous_product is None:
        client.table(table_name).delete().eq("id", product_id).execute()
    else:
        client.table(table_name).update(previous_product).eq("id", product_id).execute()

    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "products")
    try:
        paths = [
            f"models/{conversion_id}/model.obj",
            f"models/{conversion_id}/model.glb",
            f"models/{conversion_id}/model.usdz",
        ]
        if new_image_path:
            paths.append(new_image_path)
        if paths:
            client.storage.from_(bucket).remove(paths)
    except Exception:
        # The row deletion is the Undo action; asset cleanup is best-effort.
        pass
