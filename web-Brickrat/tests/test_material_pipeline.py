import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import trimesh
from PIL import Image
from trimesh.visual.material import PBRMaterial
from trimesh.visual.texture import TextureVisuals

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_diagnostics import inspect_glb
from triposr_service import triposr


def tetrahedron(colors=None):
    mesh = trimesh.Trimesh(
        vertices=np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float),
        faces=np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]]),
        vertex_colors=colors,
        process=False,
    )
    return mesh


class MaterialPipelineTests(unittest.TestCase):
    def test_vertex_colours_become_embedded_texture(self):
        colors = np.array([
            [220, 35, 35, 255], [35, 210, 60, 255],
            [35, 60, 220, 255], [230, 190, 35, 255],
        ], dtype=np.uint8)
        mesh, report = triposr._prepare_portable_material(
            tetrahedron(colors), Image.new("RGBA", (16, 16), "white"),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, resources = triposr._export_obj_bundle(mesh, root)
            glb = root / "model.glb"
            mesh.export(glb, file_type="glb")
            inspection = inspect_glb(glb)

            self.assertEqual(report["material_strategy"], "vertex_color_atlas")
            self.assertTrue(report["vertex_colors_preserved"])
            self.assertTrue(inspection["valid"])
            self.assertEqual(inspection["embedded_image_count"], 1)
            self.assertTrue((root / "model.mtl").is_file())
            self.assertTrue(any(Path(item["filename"]).suffix == ".png" for item in resources))

    def test_existing_uv_texture_is_preserved(self):
        mesh = tetrahedron()
        mesh.visual = TextureVisuals(
            uv=np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=float),
            material=PBRMaterial(
                baseColorTexture=Image.new("RGB", (8, 8), (20, 80, 160)),
            ),
        )
        _, report = triposr._prepare_portable_material(
            mesh, Image.new("RGBA", (16, 16), "white"),
        )
        self.assertEqual(report["material_strategy"], "source_texture")
        self.assertTrue(report["original_texture_preserved"])
        self.assertFalse(report["fallback_material_applied"])

    def test_missing_colour_uses_image_derived_fallback(self):
        mesh = tetrahedron()
        mesh.visual = TextureVisuals(uv=None, material=None)
        prepared, report = triposr._prepare_portable_material(
            mesh, Image.new("RGBA", (16, 16), (18, 24, 32, 255)),
        )
        self.assertEqual(report["material_strategy"], "dominant_image_color_fallback")
        self.assertTrue(report["fallback_material_applied"])
        self.assertEqual(report["fallback_base_color_rgba"][:3], [18, 24, 32])
        self.assertIsNotNone(prepared.visual.material.baseColorTexture)

    def test_usdz_is_unavailable_without_converter(self):
        with patch.dict("os.environ", {}, clear=True), patch("triposr_service.shutil.which", return_value=None):
            report = triposr._export_usdz(Path("model.glb"), Path("model.usdz"))
        self.assertFalse(report["valid"])
        self.assertIn("No GLB-to-USDZ converter", report["error"])


if __name__ == "__main__":
    unittest.main()
