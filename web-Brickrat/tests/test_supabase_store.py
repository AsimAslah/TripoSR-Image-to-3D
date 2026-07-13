import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import supabase_store


class FakeQuery:
    def __init__(self, client, table):
        self.client, self.table, self.action, self.payload = client, table, "select", None

    def select(self, *_args):
        self.action = "select"
        return self

    def eq(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def insert(self, row):
        self.action, self.payload = "insert", row
        return self

    def update(self, row):
        self.action, self.payload = "update", row
        return self

    def execute(self):
        if self.action == "select":
            return SimpleNamespace(data=self.client.existing)
        self.client.write = (self.action, self.table, self.payload)
        product_id = self.client.existing[0]["id"] if self.client.existing else "row-1"
        return SimpleNamespace(data=[{"id": product_id, **self.payload}])


class FakeStorage:
    def __init__(self, existing_names=None):
        self.uploads = []
        self.upload_options = []
        self.removed = []
        self.existing_names = existing_names or []
        self.fail_after = None

    def from_(self, _bucket):
        return self

    def upload(self, path, *args, **kwargs):
        if self.fail_after is not None and len(self.uploads) >= self.fail_after:
            raise RuntimeError("simulated upload failure")
        self.uploads.append(path)
        self.upload_options.append(args[-1] if args and isinstance(args[-1], dict) else kwargs)

    def list(self, *_args, **_kwargs):
        return [{"name": name} for name in self.existing_names]

    def get_public_url(self, path):
        return f"https://assets.test/{path}"

    def remove(self, paths):
        self.removed.extend(paths)


class FakeClient:
    def __init__(self, existing=None, storage_existing=None):
        self.storage = FakeStorage(storage_existing)
        self.existing = existing or []
        self.write = None

    def table(self, table):
        return FakeQuery(self, table)


class SupabaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = [
            Path(self.temp_dir.name) / name
            for name in ("source.png", "model.obj", "model.glb")
        ]
        for path in self.paths:
            path.write_bytes(b"same-image" if path.suffix == ".png" else b"model")

    def tearDown(self):
        self.temp_dir.cleanup()

    def save(self, client):
        fake_module = SimpleNamespace(create_client=lambda *_: client)
        with patch.dict(
            os.environ,
            {"SUPABASE_URL": "https://example.test", "SUPABASE_KEY": "secret"},
        ), patch.dict(sys.modules, {"supabase": fake_module}):
            return supabase_store.save_product(
                conversion_id=uuid4(), product={"name": "Chair"},
                image_path=self.paths[0], obj_path=self.paths[1],
                glb_path=self.paths[2], table_name="chairs",
            )

    def test_rejects_invalid_table_names(self):
        for name in ("", "9products", "product-items", "public.products"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                supabase_store.validate_table_name(name)

    def test_new_image_is_uploaded_and_inserted(self):
        client = FakeClient()
        outcome = self.save(client)

        self.assertFalse(outcome.image_reused)
        self.assertEqual(client.write[0:2], ("insert", "chairs"))
        self.assertEqual(len(client.storage.uploads), 3)
        self.assertTrue(client.storage.uploads[0].startswith("images/"))
        self.assertEqual(client.storage.upload_options[1]["content-type"], "text/plain")
        self.assertEqual(client.storage.upload_options[2]["content-type"], "model/gltf-binary")
        self.assertEqual(outcome.product["id"], "row-1")

    def test_existing_image_url_is_reused_without_image_upload(self):
        existing = [{
            "id": "old-row", "name": "Old chair", "image_sha256": "ignored-by-fake",
            "image_url": "https://assets.test/images/existing.png",
            "obj_url": "old.obj", "model_url": "old.glb",
        }]
        client = FakeClient(existing)
        outcome = self.save(client)

        self.assertTrue(outcome.image_reused)
        self.assertEqual(client.write[0:2], ("update", "chairs"))
        self.assertEqual(len(client.storage.uploads), 2)
        self.assertTrue(all(path.startswith("models/") for path in client.storage.uploads))
        self.assertEqual(outcome.product["image_url"], existing[0]["image_url"])
        self.assertEqual(outcome.previous_product["name"], "Old chair")

    def test_image_in_shared_storage_is_reused_for_another_table(self):
        import hashlib

        digest = hashlib.sha256(self.paths[0].read_bytes()).hexdigest()
        client = FakeClient(storage_existing=[f"{digest}.png"])
        outcome = self.save(client)

        self.assertTrue(outcome.image_reused)
        self.assertEqual(client.write[0], "insert")
        self.assertEqual(len(client.storage.uploads), 2)
        self.assertEqual(
            outcome.product["image_url"], f"https://assets.test/images/{digest}.png",
        )

    def test_failed_upload_cleans_assets_and_does_not_write_row(self):
        client = FakeClient()
        client.storage.fail_after = 1
        with self.assertRaises(RuntimeError):
            self.save(client)

        self.assertIsNone(client.write)
        self.assertEqual(client.storage.removed, client.storage.uploads)


if __name__ == "__main__":
    unittest.main()
