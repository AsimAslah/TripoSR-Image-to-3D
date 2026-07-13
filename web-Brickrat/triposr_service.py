from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import rembg
import torch
import trimesh
from PIL import Image, ImageOps
from trimesh.exchange.obj import export_obj
from trimesh.visual.material import PBRMaterial
from trimesh.visual.texture import TextureVisuals

from model_diagnostics import inspect_glb, inspect_usdz
from usdz_export import UsdzExportError, export_textured_mesh_to_usdz

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from tsr.system import TSR
from tsr.utils import remove_background, resize_foreground, to_gradio_3d_orientation


LOGGER = logging.getLogger("furniture_ar.export")
FURNITURE_ROUGHNESS = 0.78
FURNITURE_METALLIC = 0.0
MAX_ATLAS_SIZE = 4096


class TripoSRService:
    """Lazy, process-local wrapper around TripoSR and the portable export pipeline."""

    def __init__(self) -> None:
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self._model = None
        self._rembg_session = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            self._model = TSR.from_pretrained(
                "stabilityai/TripoSR",
                config_name="config.yaml",
                weight_name="model.ckpt",
            )
            self._model.renderer.set_chunk_size(8192)
            self._model.to(self.device)
            self._rembg_session = rembg.new_session()

    @staticmethod
    def _fill_background(image: Image.Image) -> Image.Image:
        rgba = np.asarray(image.convert("RGBA")).astype(np.float32) / 255.0
        rgb = rgba[:, :, :3] * rgba[:, :, 3:4] + (1 - rgba[:, :, 3:4]) * 0.5
        return Image.fromarray((rgb * 255.0).astype(np.uint8), "RGB")

    @staticmethod
    def _normalise_vertex_colors(vertex_colors, vertex_count: int) -> np.ndarray | None:
        if vertex_colors is None:
            return None
        colors = np.asarray(vertex_colors)
        if colors.ndim != 2 or len(colors) != vertex_count or colors.shape[1] < 3:
            return None
        if not np.isfinite(colors[:, :3]).all():
            return None
        colors = colors[:, :4].astype(np.float32)
        if colors.size and float(colors.max()) <= 1.0:
            colors *= 255.0
        colors = np.clip(np.rint(colors), 0, 255).astype(np.uint8)
        if colors.shape[1] == 3:
            colors = np.column_stack((colors, np.full(len(colors), 255, dtype=np.uint8)))
        return colors

    @staticmethod
    def _dominant_object_color(image: Image.Image) -> np.ndarray:
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        visible = rgba[:, :, 3] >= 32
        if visible.all():
            height, width = visible.shape
            visible[:] = False
            visible[height // 5:height * 4 // 5, width // 5:width * 4 // 5] = True
        pixels = rgba[:, :, :3][visible]
        if len(pixels) == 0:
            pixels = rgba[:, :, :3].reshape(-1, 3)
        # Quantized median is deterministic, robust to highlights, and comes from
        # the uploaded object rather than a random furniture colour.
        sample = pixels[::max(1, len(pixels) // 100_000)]
        median = np.median(sample.astype(np.float32), axis=0)
        return np.array([*np.clip(np.rint(median), 0, 255).astype(np.uint8), 255])

    @staticmethod
    def _pbr_material(texture: Image.Image, name: str) -> PBRMaterial:
        return PBRMaterial(
            name=name,
            baseColorTexture=texture.convert("RGBA"),
            baseColorFactor=[255, 255, 255, 255],
            metallicFactor=FURNITURE_METALLIC,
            roughnessFactor=FURNITURE_ROUGHNESS,
            doubleSided=True,
        )

    def _bake_vertex_color_atlas(
        self, mesh: trimesh.Trimesh, colors: np.ndarray,
    ) -> tuple[trimesh.Trimesh, Image.Image]:
        """Bake TripoSR vertex colours to a portable per-face texture atlas.

        Apple Quick Look does not reliably render glTF vertex colours. A compact
        colour atlas with one padded tile per dense triangle preserves the
        available TripoSR colour signal using ordinary UV/PBR materials.
        """
        face_count = len(mesh.faces)
        if face_count == 0:
            raise ValueError("The generated mesh has no faces.")

        grid = math.ceil(math.sqrt(face_count))
        tile_size = max(1, min(4, MAX_ATLAS_SIZE // grid))
        atlas_size = 1 << math.ceil(math.log2(max(1, grid * tile_size)))
        atlas_size = min(atlas_size, MAX_ATLAS_SIZE)
        if grid * tile_size > atlas_size:
            tile_size = max(1, atlas_size // grid)
        if grid > atlas_size:
            raise ValueError("Mesh has too many faces for the texture atlas.")

        face_colors = np.rint(colors[mesh.faces].astype(np.float32).mean(axis=1))
        face_colors = np.clip(face_colors, 0, 255).astype(np.uint8)
        atlas_base = np.median(face_colors.astype(np.float32), axis=0)
        atlas_base = np.clip(np.rint(atlas_base), 0, 255).astype(np.uint8)
        atlas = np.empty((atlas_size, atlas_size, 4), dtype=np.uint8)
        atlas[:] = atlas_base
        uvs = np.empty((face_count * 3, 2), dtype=np.float64)
        for index, color in enumerate(face_colors):
            column, row = index % grid, index // grid
            x0, y0 = column * tile_size, row * tile_size
            atlas[y0:y0 + tile_size, x0:x0 + tile_size] = color
            u = (x0 + tile_size / 2.0) / atlas_size
            v = 1.0 - ((y0 + tile_size / 2.0) / atlas_size)
            uvs[index * 3:(index + 1) * 3] = (u, v)

        vertices = mesh.vertices[mesh.faces].reshape((-1, 3))
        faces = np.arange(face_count * 3, dtype=np.int64).reshape((-1, 3))
        baked = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        baked.fix_normals()
        texture = Image.fromarray(atlas, "RGBA")
        baked.visual = TextureVisuals(
            uv=uvs,
            material=self._pbr_material(texture, "TripoSR vertex-colour atlas"),
        )
        return baked, texture

    def _prepare_portable_material(
        self, mesh: trimesh.Trimesh, object_image: Image.Image,
    ) -> tuple[trimesh.Trimesh, dict]:
        visual = mesh.visual
        material = getattr(visual, "material", None)
        uv = getattr(visual, "uv", None)
        source_texture = (
            getattr(material, "baseColorTexture", None)
            if material is not None else None
        ) or (getattr(material, "image", None) if material is not None else None)
        colors = self._normalise_vertex_colors(
            getattr(visual, "vertex_colors", None), len(mesh.vertices),
        )
        report = {
            "source_visual": getattr(visual, "kind", "unknown"),
            "source_has_uv": bool(uv is not None and len(uv) == len(mesh.vertices)),
            "source_has_texture": source_texture is not None,
            "source_has_vertex_colors": colors is not None,
            "source_material_count": 1 if material is not None else 0,
            "fallback_material_applied": False,
            "original_texture_preserved": False,
            "base_color_color_space": "sRGB",
        }

        if source_texture is not None and report["source_has_uv"]:
            if isinstance(source_texture, Image.Image):
                texture = source_texture
            elif isinstance(source_texture, np.ndarray):
                texture = Image.fromarray(source_texture)
            else:
                texture = Image.open(source_texture)
            mesh.visual = TextureVisuals(
                uv=np.asarray(uv, dtype=np.float64),
                material=self._pbr_material(texture, "Source base-colour texture"),
            )
            report.update({
                "material_strategy": "source_texture",
                "original_texture_preserved": True,
                "texture_size": list(texture.size),
            })
            return mesh, report

        if colors is not None:
            baked_mesh, texture = self._bake_vertex_color_atlas(mesh, colors)
            report.update({
                "material_strategy": "vertex_color_atlas",
                "vertex_colors_preserved": True,
                "texture_size": list(texture.size),
                "note": "TripoSR produced vertex colours, not an original UV photo texture; colours were baked to a portable atlas.",
            })
            return baked_mesh, report

        fallback = self._dominant_object_color(object_image)
        texture = Image.new("RGBA", (4, 4), tuple(int(value) for value in fallback))
        mesh.visual = TextureVisuals(
            uv=np.zeros((len(mesh.vertices), 2), dtype=np.float64),
            material=self._pbr_material(texture, "Image-derived fallback material"),
        )
        report.update({
            "material_strategy": "dominant_image_color_fallback",
            "fallback_material_applied": True,
            "fallback_base_color_rgba": fallback.tolist(),
            "texture_size": [4, 4],
            "note": "No usable texture or vertex colours were available; the fallback colour was derived from the uploaded object image.",
        })
        return mesh, report

    @staticmethod
    def _export_obj_bundle(mesh: trimesh.Trimesh, output_dir: Path) -> tuple[Path, list[dict]]:
        obj_text, resources = export_obj(
            mesh,
            include_normals=True,
            include_color=True,
            include_texture=True,
            return_texture=True,
            mtl_name="model.mtl",
        )
        obj_path = output_dir / "model.obj"
        obj_path.write_text(obj_text, encoding="utf-8")
        exported = []
        for filename, data in resources.items():
            safe_name = Path(filename).name
            destination = output_dir / safe_name
            destination.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
            exported.append({"filename": safe_name, "size": destination.stat().st_size})
        return obj_path, exported

    @staticmethod
    def _converter_command(glb_path: Path, usdz_path: Path) -> tuple[list[str] | None, str | None]:
        configured = os.getenv("USDZ_CONVERTER")
        if configured:
            converter = shutil.which(configured) or configured
            return [converter, str(glb_path), str(usdz_path)], "USDZ_CONVERTER"
        xcrun = shutil.which("xcrun")
        if xcrun:
            return [xcrun, "usdzconvert", str(glb_path), str(usdz_path)], "xcrun usdzconvert"
        converter = shutil.which("usd_from_gltf")
        if converter:
            return [converter, str(glb_path), str(usdz_path)], "usd_from_gltf"
        return None, None

    @classmethod
    def _export_usdz(
        cls, glb_path: Path, usdz_path: Path, *, mesh: trimesh.Trimesh | None = None,
    ) -> dict:
        command, converter_name = cls._converter_command(glb_path, usdz_path)
        external_error = None
        if command is not None:
            try:
                completed = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                LOGGER.info(
                    "USDZ converter completed converter=%s stdout=%s",
                    converter_name, completed.stdout[-500:],
                )
                inspection = inspect_usdz(usdz_path)
                inspection["converter"] = converter_name
                if inspection["valid"]:
                    return inspection
                external_error = inspection.get("error", "USDZ validation failed.")
            except subprocess.TimeoutExpired:
                external_error = "USDZ conversion timed out."
            except (OSError, subprocess.CalledProcessError) as exc:
                detail = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
                external_error = f"USDZ conversion failed: {str(detail).strip()[-1000:]}"
            usdz_path.unlink(missing_ok=True)
            LOGGER.warning(
                "External USDZ converter failed converter=%s error=%s; trying OpenUSD",
                converter_name, external_error,
            )

        if mesh is None:
            return {
                "valid": False,
                "converter": converter_name,
                "error": external_error or (
                    "No GLB-to-USDZ converter is installed and no in-memory mesh "
                    "was supplied to the OpenUSD exporter."
                ),
            }
        try:
            export_textured_mesh_to_usdz(mesh, usdz_path)
        except (UsdzExportError, OSError, RuntimeError) as exc:
            return {
                "valid": False,
                "converter": "OpenUSD",
                "error": f"OpenUSD USDZ export failed: {exc}",
            }
        inspection = inspect_usdz(usdz_path)
        inspection["converter"] = "OpenUSD"
        if not inspection["valid"]:
            usdz_path.unlink(missing_ok=True)
        return inspection

    def convert(
        self,
        image_path: Path,
        output_dir: Path,
        *,
        remove_bg: bool = True,
        foreground_ratio: float = 0.85,
        resolution: int = 256,
        preserve_details: bool = True,
        density_threshold: float = 20.0,
    ) -> tuple[Path, Path]:
        self._load()
        source = ImageOps.exif_transpose(Image.open(image_path)).convert("RGBA")
        object_image = source
        if remove_bg:
            rembg_options = {}
            if preserve_details:
                rembg_options = {
                    "alpha_matting": True,
                    "alpha_matting_foreground_threshold": 240,
                    "alpha_matting_background_threshold": 10,
                    "alpha_matting_erode_size": 0,
                }
            object_image = remove_background(
                source.convert("RGB"), self._rembg_session, **rembg_options,
            )
            object_image = resize_foreground(object_image, foreground_ratio)
        inference_image = self._fill_background(object_image)

        output_dir.mkdir(parents=True, exist_ok=True)
        inference_image.save(output_dir / "processed.png", optimize=True)
        glb_path = output_dir / "model.glb"
        usdz_path = output_dir / "model.usdz"
        report_path = output_dir / "material_report.json"

        with self._inference_lock, torch.no_grad():
            scene_codes = self._model(inference_image, device=self.device)
            mesh = self._model.extract_mesh(
                scene_codes,
                True,
                resolution=resolution,
                threshold=density_threshold,
            )[0]
            mesh.remove_unreferenced_vertices()
            mesh.fix_normals()
            mesh = to_gradio_3d_orientation(mesh)
            portable_mesh, report = self._prepare_portable_material(mesh, object_image)

            obj_path, obj_resources = self._export_obj_bundle(portable_mesh, output_dir)
            report["obj_resources"] = obj_resources
            report["texture_files"] = [
                item for item in obj_resources
                if Path(item["filename"]).suffix.lower() in {".png", ".jpg", ".jpeg"}
            ]
            LOGGER.info(
                "Material source uv=%s vertex_colors=%s materials=%s strategy=%s textures=%s",
                report["source_has_uv"], report["source_has_vertex_colors"],
                report["source_material_count"], report["material_strategy"],
                report["texture_files"],
            )

            portable_mesh.export(glb_path, file_type="glb")
            glb_report = inspect_glb(glb_path)
            report["glb_inspection"] = glb_report
            LOGGER.info(
                "GLB exported valid=%s size=%s embedded_images=%s uv=%s vertex_colors=%s",
                glb_report["valid"], glb_report["file_size"],
                glb_report.get("embedded_image_count"), glb_report.get("has_uv"),
                glb_report.get("has_vertex_colors"),
            )
            if not glb_report["valid"]:
                report["material_error"] = glb_report.get("error", "GLB validation failed.")
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                raise RuntimeError(report["material_error"])

            usdz_report = self._export_usdz(glb_path, usdz_path, mesh=portable_mesh)
            report["usdz_inspection"] = usdz_report
            report["usdz_created"] = usdz_report["valid"]
            if not usdz_report["valid"]:
                report["usdz_error"] = usdz_report.get("error", "USDZ validation failed.")
            LOGGER.info(
                "USDZ export valid=%s size=%s textures=%s converter=%s",
                usdz_report["valid"], usdz_report.get("file_size", 0),
                usdz_report.get("texture_files", []), usdz_report.get("converter"),
            )
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return obj_path, glb_path


triposr = TripoSRService()
