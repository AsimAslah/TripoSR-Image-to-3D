import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
from pxr import Usd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_diagnostics import inspect_usdz
from usdz_export import export_textured_mesh_to_usdz


class UsdzExportTests(unittest.TestCase):
    def test_textured_mesh_is_packaged_for_quick_look(self):
        mesh = SimpleNamespace(
            vertices=np.array([
                [0, -2, 0], [1, -2, 0], [0, -1, 0], [0, -2, 1],
            ], dtype=np.float32),
            faces=np.array([
                [0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3],
            ], dtype=np.int32),
            visual=SimpleNamespace(
                uv=np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=np.float32),
                material=SimpleNamespace(
                    baseColorTexture=Image.new("RGBA", (8, 8), (180, 80, 40, 255)),
                    roughnessFactor=0.78,
                    metallicFactor=0.0,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.usdz"
            export_textured_mesh_to_usdz(mesh, path)
            report = inspect_usdz(path)
            stage = Usd.Stage.Open(str(path))

            self.assertTrue(report["valid"])
            self.assertEqual(report["package_files"], ["model.usdc", "baseColor.png"])
            self.assertTrue(report["aligned_to_64_bytes"])
            self.assertTrue(stage)
            self.assertEqual(str(stage.GetDefaultPrim().GetPath()), "/Furniture")

            points = stage.GetPrimAtPath("/Furniture/Mesh").GetAttribute("points").Get()
            self.assertEqual(min(point[1] for point in points), 0.0)
            texture = stage.GetPrimAtPath(
                "/Furniture/Material/BaseColorTexture"
            ).GetAttribute("inputs:file").Get()
            self.assertTrue(texture.resolvedPath)
            self.assertIn("model.usdz[baseColor.png]", texture.resolvedPath)


if __name__ == "__main__":
    unittest.main()
