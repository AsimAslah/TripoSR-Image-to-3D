import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_diagnostics import inspect_glb, inspect_usdz


def write_glb(path: Path, document: dict) -> None:
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    total = 12 + 8 + len(payload)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(payload), 0x4E4F534A)
        + payload
    )


def write_usdz(path: Path, files: dict[str, bytes]) -> None:
    """Write a small uncompressed archive with 64-byte-aligned file data."""
    with zipfile.ZipFile(path, "w") as package:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED
            offset = package.fp.tell()
            base_data_offset = offset + 30 + len(name.encode("utf-8"))
            padding = (-base_data_offset) % 64
            if 0 < padding < 4:
                padding += 64
            if padding:
                info.extra = struct.pack("<HH", 0xCAFE, padding - 4) + bytes(padding - 4)
            package.writestr(info, data)


class ModelDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_embedded_textured_glb(self):
        path = self.root / "model.glb"
        write_glb(path, {
            "asset": {"version": "2.0"},
            "accessors": [{"count": 3}],
            "bufferViews": [{}],
            "images": [{"bufferView": 0, "mimeType": "image/png"}],
            "textures": [{"source": 0}],
            "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
            "meshes": [{"primitives": [{
                "attributes": {"POSITION": 0, "TEXCOORD_0": 0}, "material": 0,
            }]}],
        })
        report = inspect_glb(path)
        self.assertTrue(report["valid"])
        self.assertEqual(report["embedded_image_count"], 1)
        self.assertTrue(report["has_uv"])

    def test_glb_without_embedded_texture_is_invalid(self):
        path = self.root / "model.glb"
        write_glb(path, {
            "asset": {"version": "2.0"},
            "accessors": [{"count": 3}],
            "materials": [{}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        })
        self.assertFalse(inspect_glb(path)["valid"])

    def test_glb_with_external_buffer_is_invalid_for_mobile_delivery(self):
        path = self.root / "external.glb"
        write_glb(path, {
            "asset": {"version": "2.0"},
            "buffers": [{"uri": "local-model.bin", "byteLength": 12}],
            "accessors": [{"count": 3}],
            "bufferViews": [{}],
            "images": [{"bufferView": 0, "mimeType": "image/png"}],
            "textures": [{"source": 0}],
            "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
            "meshes": [{"primitives": [{
                "attributes": {"POSITION": 0, "TEXCOORD_0": 0}, "material": 0,
            }]}],
        })
        report = inspect_glb(path)
        self.assertFalse(report["valid"])
        self.assertEqual(report["external_buffer_uris"], ["local-model.bin"])

    def test_usdz_requires_scene_and_texture(self):
        valid = self.root / "valid.usdz"
        write_usdz(valid, {"scene.usdc": b"usd", "textures/baseColor.png": b"png"})
        report = inspect_usdz(valid)
        self.assertTrue(report["valid"])
        self.assertTrue(report["aligned_to_64_bytes"])

        invalid = self.root / "invalid.usdz"
        write_usdz(invalid, {"scene.usdc": b"usd"})
        self.assertFalse(inspect_usdz(invalid)["valid"])

    def test_usdz_rejects_an_unaligned_zip(self):
        path = self.root / "unaligned.usdz"
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("scene.usdc", b"usd")
            package.writestr("base.png", b"png")
        report = inspect_usdz(path)
        self.assertFalse(report["valid"])
        self.assertFalse(report["aligned_to_64_bytes"])
