import shutil
import threading
import mimetypes
import json
import hashlib
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from supabase_store import (
    SupabaseNotConfigured, save_product, undo_product_save,
    validate_table_name,
)
from triposr_service import triposr
from model_diagnostics import inspect_model

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(exist_ok=True)
mimetypes.add_type("model/vnd.usdz+zip", ".usdz")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("text/plain", ".obj")
mimetypes.add_type("text/plain", ".mtl")


def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.js", "styles.css", "service-worker.js", "manifest.json", "obj-preview.js"):
        path = BASE_DIR / "static" / name
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="TripoSR Product Studio")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
conversion_lock = threading.Lock()
save_state_lock = threading.Lock()
active_saves: set[UUID] = set()
saved_products: dict[UUID, tuple[str, str, dict | None, str | None]] = {}
LOGGER = logging.getLogger("furniture_ar.web")
SAFE_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PREVIEW_SUFFIXES = {".glb", ".obj", ".usdz", ".png", ".jpg", ".jpeg", ".webp", ".mtl"}
DOWNLOAD_NAMES = {"model.glb", "model.obj", "model.usdz"}
ASSET_MEDIA_TYPES = {
    ".glb": "model/gltf-binary",
    ".obj": "text/plain; charset=utf-8",
    ".mtl": "text/plain; charset=utf-8",
    ".usdz": "model/vnd.usdz+zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _first_forwarded_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def external_base_url(request: Request) -> str:
    """Build public URLs from proxy headers without ever falling back to a path."""
    forwarded = request.headers.get("forwarded", "")
    forwarded_parts = {}
    if forwarded:
        for item in forwarded.split(",", 1)[0].split(";"):
            key, separator, value = item.strip().partition("=")
            if separator:
                forwarded_parts[key.lower()] = value.strip().strip('"')
    scheme = _first_forwarded_value(request.headers.get("x-forwarded-proto"))
    scheme = scheme or forwarded_parts.get("proto") or request.url.scheme
    if scheme not in {"http", "https"}:
        scheme = request.url.scheme if request.url.scheme in {"http", "https"} else "https"
    host = _first_forwarded_value(request.headers.get("x-forwarded-host"))
    host = host or forwarded_parts.get("host") or request.headers.get("host", "")
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
        host = request.url.netloc
    return f"{scheme}://{host}/"


def _external_url(request: Request, path: str) -> str:
    return urljoin(external_base_url(request), path.lstrip("/"))


def _resolve_generated_asset(conversion_id: UUID, filename: str, *, download: bool = False) -> Path:
    if not SAFE_ASSET_NAME.fullmatch(filename) or Path(filename).name != filename:
        raise HTTPException(400, "Unsafe model filename.")
    if Path(filename).suffix.lower() not in PREVIEW_SUFFIXES:
        raise HTTPException(404, "Model asset not found.")
    if download and filename not in DOWNLOAD_NAMES:
        raise HTTPException(404, "Download asset not found.")
    work_dir = (GENERATED_DIR / str(conversion_id)).resolve()
    path = (work_dir / filename).resolve()
    if path.parent != work_dir:
        raise HTTPException(400, "Unsafe model path.")
    if not path.is_file() or path.stat().st_size <= 0:
        raise HTTPException(404, "Model asset not found.")
    return path


def _asset_metadata(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    size = path.stat().st_size
    value = float(size)
    unit = "bytes"
    for candidate in ("KB", "MB", "GB"):
        if value < 1024 or candidate == "GB":
            break
        value /= 1024
        unit = candidate
    display = f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
    return {"bytes": size, "display": display}


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/service-worker.js"} or response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"asset_version": ASSET_VERSION},
    )


@app.get("/service-worker.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    return FileResponse(
        BASE_DIR / "static" / "manifest.json",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/models/{conversion_id}/preview/{filename}", name="preview_asset")
def preview_asset(request: Request, conversion_id: UUID, filename: str):
    path = _resolve_generated_asset(conversion_id, filename)
    media_type = ASSET_MEDIA_TYPES[path.suffix.lower()]
    LOGGER.info(
        "Serving preview conversion_id=%s asset=%s bytes=%s media_type=%s range=%s host=%s",
        conversion_id, filename, path.stat().st_size, media_type,
        request.headers.get("range", "none"), external_base_url(request),
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
        stat_result=path.stat(),
        headers={
            "Cache-Control": "private, no-store, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/models/{conversion_id}/download/{filename}", name="download_asset")
def download_asset(request: Request, conversion_id: UUID, filename: str):
    path = _resolve_generated_asset(conversion_id, filename, download=True)
    media_type = ASSET_MEDIA_TYPES[path.suffix.lower()]
    LOGGER.info(
        "Serving download conversion_id=%s asset=%s bytes=%s host=%s",
        conversion_id, filename, path.stat().st_size, external_base_url(request),
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="attachment",
        stat_result=path.stat(),
        headers={
            "Cache-Control": "private, no-store, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/convert", response_class=HTMLResponse)
def convert_image(
    request: Request,
    image: UploadFile = File(...),
    remove_background: bool = Form(True),
    foreground_ratio: float = Form(0.85),
    resolution: int = Form(320),
    preserve_details: bool = Form(False),
    density_threshold: float = Form(20.0),
):
    if not image.content_type or not image.content_type.startswith("image/"):
        return templates.TemplateResponse(
            request, "conversion_result.html",
            {"error": "Please upload a valid image."}, status_code=400,
        )
    if resolution not in range(32, 321, 32):
        raise HTTPException(400, "Resolution must be between 32 and 320 in steps of 32.")
    if not 10 <= density_threshold <= 40:
        raise HTTPException(400, "Geometry threshold must be between 10 and 40.")

    if not conversion_lock.acquire(blocking=False):
        return templates.TemplateResponse(
            request, "conversion_result.html",
            {"error": "A 3D conversion is already running. Please wait for it to finish."},
            status_code=409,
        )

    try:
        conversion_id = uuid4()
        work_dir = GENERATED_DIR / str(conversion_id)
        work_dir.mkdir(parents=True)
        suffix = Path(image.filename or "source.png").suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".png"
        image_path = work_dir / f"source{suffix}"
        with image_path.open("wb") as destination:
            shutil.copyfileobj(image.file, destination)

        triposr.convert(
            image_path, work_dir,
            remove_bg=remove_background,
            foreground_ratio=max(0.5, min(foreground_ratio, 1.0)),
            resolution=resolution,
            preserve_details=preserve_details,
            density_threshold=density_threshold,
        )
    except Exception:
        LOGGER.exception("3D conversion failed")
        if "work_dir" in locals():
            shutil.rmtree(work_dir, ignore_errors=True)
        return templates.TemplateResponse(
            request, "conversion_result.html",
            {
                "error": "3D conversion failed. Please check the server log for the technical cause and try again.",
                "error_code": "conversion_failed",
            }, status_code=500,
        )
    finally:
        conversion_lock.release()

    obj_path = work_dir / "model.obj"
    glb_path = work_dir / "model.glb"
    usdz_path = work_dir / "model.usdz"
    material_report_path = work_dir / "material_report.json"
    material_report = {}
    if material_report_path.is_file():
        try:
            material_report = json.loads(material_report_path.read_text(encoding="utf-8"))
        except Exception:
            material_report = {"material_error": "Material report could not be read."}
    glb_inspection = material_report.get("glb_inspection", {})
    usdz_inspection = material_report.get("usdz_inspection", {})
    glb_valid = bool(glb_path.is_file() and glb_inspection.get("valid"))
    usdz_valid = bool(usdz_path.is_file() and usdz_inspection.get("valid"))
    preview_path = f"/models/{conversion_id}/preview"
    download_path = f"/models/{conversion_id}/download"
    obj_url = _external_url(request, f"{preview_path}/model.obj") if obj_path.is_file() else None
    glb_url = _external_url(request, f"{preview_path}/model.glb") if glb_valid else None
    usdz_url = _external_url(request, f"{preview_path}/model.usdz") if usdz_valid else None
    LOGGER.info(
        "Conversion ready conversion_id=%s public_base=%s glb_valid=%s glb_bytes=%s "
        "obj_bytes=%s usdz_valid=%s usdz_bytes=%s",
        conversion_id, external_base_url(request), glb_valid,
        glb_path.stat().st_size if glb_path.is_file() else 0,
        obj_path.stat().st_size if obj_path.is_file() else 0,
        usdz_valid, usdz_path.stat().st_size if usdz_path.is_file() else 0,
    )
    return templates.TemplateResponse(
        request, "conversion_result.html",
        {
            "conversion_id": conversion_id,
            "image_url": _external_url(request, f"{preview_path}/{image_path.name}"),
            "processed_url": _external_url(request, f"{preview_path}/processed.png"),
            "obj_url": obj_url,
            "glb_url": glb_url,
            "usdz_url": usdz_url,
            "obj_download_url": _external_url(request, f"{download_path}/model.obj") if obj_path.is_file() else None,
            "glb_download_url": _external_url(request, f"{download_path}/model.glb") if glb_valid else None,
            "usdz_download_url": _external_url(request, f"{download_path}/model.usdz") if usdz_valid else None,
            "obj_file": _asset_metadata(obj_path),
            "glb_file": _asset_metadata(glb_path) if glb_valid else None,
            "usdz_file": _asset_metadata(usdz_path) if usdz_valid else None,
            "glb_valid": glb_valid,
            "usdz_valid": usdz_valid,
            "material_report": material_report,
            "material_error": material_report.get("material_error"),
            "usdz_error": material_report.get("usdz_error"),
            "fallback_material_applied": material_report.get("fallback_material_applied", False),
            "material_strategy": material_report.get("material_strategy"),
            "original_texture_preserved": material_report.get("original_texture_preserved", False),
            "vertex_colors_preserved": material_report.get("vertex_colors_preserved", False),
        },
    )


@app.post("/products", response_class=HTMLResponse)
def create_product(
    request: Request,
    conversion_id: UUID = Form(...),
    name: str = Form(...),
    category: str = Form(...),
    subcategory: str = Form(...),
    description: str = Form(""),
    price: float | None = Form(None),
    table_name: str = Form(...),
):
    try:
        table_name = validate_table_name(table_name)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "save_result.html", {"error": str(exc)}, status_code=400,
        )

    with save_state_lock:
        if conversion_id in active_saves or conversion_id in saved_products:
            return templates.TemplateResponse(
                request, "save_result.html",
                {"error": "This conversion has already been saved or is currently saving."},
                status_code=409,
            )
        active_saves.add(conversion_id)

    work_dir = GENERATED_DIR / str(conversion_id)
    images = [*work_dir.glob("source.*")]
    if not images or not (work_dir / "model.obj").is_file() or not (work_dir / "model.glb").is_file():
        with save_state_lock:
            active_saves.discard(conversion_id)
        return templates.TemplateResponse(
            request, "save_result.html", {"error": "Conversion files were not found."},
            status_code=404,
        )
    product = {
        "name": name.strip(),
        "category": category.strip(),
        "subcategory": subcategory.strip(),
        "description": description.strip(),
        "price": price,
    }
    try:
        usdz_path = work_dir / "model.usdz"
        outcome = save_product(
            conversion_id=conversion_id,
            product=product,
            image_path=images[0],
            obj_path=work_dir / "model.obj",
            glb_path=work_dir / "model.glb",
            usdz_path=usdz_path if usdz_path.is_file() else None,
            table_name=table_name,
        )
    except SupabaseNotConfigured as exc:
        with save_state_lock:
            active_saves.discard(conversion_id)
        return templates.TemplateResponse(
            request, "save_result.html", {"error": str(exc)}, status_code=503,
        )
    except Exception as exc:
        with save_state_lock:
            active_saves.discard(conversion_id)
        return templates.TemplateResponse(
            request, "save_result.html",
            {"error": f"Supabase save failed: {exc}"}, status_code=500,
        )
    saved = outcome.product
    product_id = str(saved.get("id", ""))
    if not product_id:
        with save_state_lock:
            active_saves.discard(conversion_id)
        return templates.TemplateResponse(
            request, "save_result.html",
            {"error": "Supabase saved the row but did not return its ID; Undo is unavailable."},
            status_code=502,
        )
    with save_state_lock:
        active_saves.discard(conversion_id)
        saved_products[conversion_id] = (
            table_name, product_id, outcome.previous_product, outcome.new_image_path,
        )
    return templates.TemplateResponse(
        request, "save_result.html",
        {
            "product": saved, "conversion_id": conversion_id, "table_name": table_name,
            "image_reused": outcome.image_reused,
        },
    )


@app.delete("/products/{conversion_id}", response_class=HTMLResponse)
def undo_product(request: Request, conversion_id: UUID):
    with save_state_lock:
        saved = saved_products.get(conversion_id)
    if saved is None:
        return templates.TemplateResponse(
            request, "save_result.html",
            {"error": "This save was already undone or is no longer available."},
            status_code=404,
        )

    table_name, product_id, previous_product, new_image_path = saved
    try:
        undo_product_save(
            conversion_id=conversion_id, table_name=table_name, product_id=product_id,
            previous_product=previous_product, new_image_path=new_image_path,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request, "save_result.html", {"error": f"Undo failed: {exc}"},
            status_code=500,
        )
    with save_state_lock:
        saved_products.pop(conversion_id, None)
    return templates.TemplateResponse(
        request, "save_result.html",
        {"undone": True, "restored": previous_product is not None},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/models/{conversion_id}", include_in_schema=False)
def debug_model(conversion_id: UUID):
    if os.getenv("MODEL_DEBUG", "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(404, "Model diagnostics are disabled.")
    work_dir = GENERATED_DIR / str(conversion_id)
    glb_path = work_dir / "model.glb"
    usdz_path = work_dir / "model.usdz"
    return inspect_model(glb_path, usdz_path if usdz_path.is_file() else None)
