import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import main


class AssetDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.generated = Path(self.temp.name)
        self.generated_patch = patch.object(main, "GENERATED_DIR", self.generated)
        self.generated_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.generated_patch.stop()
        self.temp.cleanup()

    def make_asset(self, name: str, payload: bytes = b"model-data"):
        conversion_id = uuid4()
        root = self.generated / str(conversion_id)
        root.mkdir()
        (root / name).write_bytes(payload)
        return conversion_id

    def test_glb_preview_is_inline_with_correct_mime_and_range_support(self):
        conversion_id = self.make_asset("model.glb", b"0123456789")
        response = self.client.get(f"/models/{conversion_id}/preview/model.glb")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "model/gltf-binary")
        self.assertTrue(response.headers["content-disposition"].startswith("inline"))
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertIn("no-store", response.headers["cache-control"])

        partial = self.client.get(
            f"/models/{conversion_id}/preview/model.glb", headers={"Range": "bytes=2-5"},
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"2345")

    def test_glb_download_is_attachment(self):
        conversion_id = self.make_asset("model.glb")
        response = self.client.get(f"/models/{conversion_id}/download/model.glb")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "model/gltf-binary")
        self.assertTrue(response.headers["content-disposition"].startswith("attachment"))

    def test_usdz_preview_has_quick_look_mime_and_is_inline(self):
        conversion_id = self.make_asset("model.usdz")
        response = self.client.get(f"/models/{conversion_id}/preview/model.usdz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "model/vnd.usdz+zip")
        self.assertTrue(response.headers["content-disposition"].startswith("inline"))

    def test_missing_and_unsafe_assets_are_rejected(self):
        conversion_id = uuid4()
        self.assertEqual(
            self.client.get(f"/models/{conversion_id}/preview/model.glb").status_code, 404,
        )
        with self.assertRaises(HTTPException) as raised:
            main._resolve_generated_asset(conversion_id, "../model.glb")
        self.assertEqual(raised.exception.status_code, 400)

    def test_external_base_url_respects_forwarded_ngrok_headers(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8000"),
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"studio-example.ngrok-free.app"),
            ],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 1),
            "root_path": "",
        })
        self.assertEqual(
            main.external_base_url(request), "https://studio-example.ngrok-free.app/",
        )

    def test_conversion_result_has_sizes_public_urls_and_no_ios_ar_without_usdz(self):
        def fake_convert(_image_path, output_dir, **_options):
            (output_dir / "processed.png").write_bytes(b"png")
            (output_dir / "model.obj").write_bytes(b"obj-bytes")
            (output_dir / "model.glb").write_bytes(b"glb-bytes-longer")
            (output_dir / "material_report.json").write_text(json.dumps({
                "glb_inspection": {"valid": True, "file_size": 16},
                "usdz_inspection": {"valid": False},
                "usdz_error": "No converter is configured.",
            }), encoding="utf-8")

        with patch.object(main.triposr, "convert", side_effect=fake_convert):
            response = self.client.post(
                "/convert",
                files={"image": ("chair.png", b"image", "image/png")},
                data={"resolution": "320", "density_threshold": "20"},
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "studio-example.ngrok-free.app",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("16 bytes", response.text)
        self.assertIn("https://studio-example.ngrok-free.app/models/", response.text)
        self.assertNotIn("127.0.0.1", response.text)
        self.assertNotIn('rel="ar"', response.text)
        self.assertIn("iPhone AR file is not available", response.text)

    def test_conversion_result_exposes_valid_usdz_to_iphone_quick_look(self):
        def fake_convert(_image_path, output_dir, **_options):
            (output_dir / "processed.png").write_bytes(b"png")
            (output_dir / "model.obj").write_bytes(b"obj")
            (output_dir / "model.glb").write_bytes(b"glb")
            (output_dir / "model.usdz").write_bytes(b"validated-usdz")
            (output_dir / "material_report.json").write_text(json.dumps({
                "glb_inspection": {"valid": True, "file_size": 3},
                "usdz_inspection": {"valid": True, "file_size": 14},
            }), encoding="utf-8")

        with patch.object(main.triposr, "convert", side_effect=fake_convert):
            response = self.client.post(
                "/convert",
                files={"image": ("chair.png", b"image", "image/png")},
                data={"resolution": "320", "density_threshold": "20"},
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "studio-example.ngrok-free.app",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Validated for Apple Quick Look", response.text)
        self.assertIn('rel="ar"', response.text)
        self.assertIn('data-usdz-ready="true"', response.text)
        self.assertIn("https://studio-example.ngrok-free.app/models/", response.text)
        self.assertIn("/preview/model.usdz", response.text)


if __name__ == "__main__":
    unittest.main()
