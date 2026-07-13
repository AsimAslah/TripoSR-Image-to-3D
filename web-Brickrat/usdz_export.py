"""Create a textured, Apple Quick Look compatible USDZ from a Trimesh mesh."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


class UsdzExportError(RuntimeError):
    """Raised when a portable mesh cannot be represented as a USDZ asset."""


def _texture_image(mesh) -> Image.Image:
    material = getattr(getattr(mesh, "visual", None), "material", None)
    texture = getattr(material, "baseColorTexture", None) if material is not None else None
    if texture is None and material is not None:
        texture = getattr(material, "image", None)
    if texture is None:
        raise UsdzExportError("The mesh has no base-colour texture for USDZ export.")
    if isinstance(texture, Image.Image):
        return texture.convert("RGBA")
    if isinstance(texture, np.ndarray):
        return Image.fromarray(texture).convert("RGBA")
    try:
        with Image.open(texture) as image:
            return image.convert("RGBA")
    except (OSError, TypeError) as exc:
        raise UsdzExportError("The mesh base-colour texture could not be read.") from exc


def _material_number(mesh, attribute: str, default: float) -> float:
    material = getattr(getattr(mesh, "visual", None), "material", None)
    value = getattr(material, attribute, default) if material is not None else default
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def _calculate_vertex_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    triangles = vertices[faces]
    face_normals = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    lengths = np.linalg.norm(face_normals, axis=1)
    usable = lengths > np.finfo(np.float32).eps
    face_normals[usable] /= lengths[usable, None]
    face_normals[~usable] = (0.0, 1.0, 0.0)
    normals = np.zeros_like(vertices, dtype=np.float32)
    np.add.at(normals, faces[:, 0], face_normals)
    np.add.at(normals, faces[:, 1], face_normals)
    np.add.at(normals, faces[:, 2], face_normals)
    lengths = np.linalg.norm(normals, axis=1)
    usable = lengths > np.finfo(np.float32).eps
    normals[usable] /= lengths[usable, None]
    normals[~usable] = (0.0, 1.0, 0.0)
    return np.ascontiguousarray(normals)


def export_textured_mesh_to_usdz(mesh, usdz_path: str | Path) -> Path:
    """Author a binary USD stage and package it with its PNG texture.

    OpenUSD's package writer creates the uncompressed, 64-byte-aligned archive
    required by USDZ. Geometry is moved vertically so its lowest Y coordinate
    rests on the AR placement surface.
    """
    try:
        from pxr import Kind, Sdf, Usd, UsdGeom, UsdShade, Vt
    except ImportError as exc:
        raise UsdzExportError(
            "OpenUSD is not installed. Install the usd-core dependency."
        ) from exc

    vertices = np.asarray(getattr(mesh, "vertices", None), dtype=np.float32)
    faces = np.asarray(getattr(mesh, "faces", None), dtype=np.int32)
    uvs = np.asarray(getattr(getattr(mesh, "visual", None), "uv", None), dtype=np.float32)
    if vertices.ndim != 2 or vertices.shape[1:] != (3,) or len(vertices) == 0:
        raise UsdzExportError("The mesh has no valid vertices for USDZ export.")
    if faces.ndim != 2 or faces.shape[1:] != (3,) or len(faces) == 0:
        raise UsdzExportError("The mesh must contain triangular faces for USDZ export.")
    if faces.min() < 0 or faces.max() >= len(vertices):
        raise UsdzExportError("The mesh contains an out-of-range face index.")
    if uvs.shape != (len(vertices), 2):
        raise UsdzExportError("The mesh has no per-vertex UVs for USDZ export.")
    if not all(np.isfinite(values).all() for values in (vertices, uvs)):
        raise UsdzExportError("The mesh contains non-finite geometry or UV values.")

    texture = _texture_image(mesh)
    vertices = np.ascontiguousarray(vertices.copy())
    vertices[:, 1] -= float(vertices[:, 1].min())
    faces = np.ascontiguousarray(faces)
    normals = _calculate_vertex_normals(vertices, faces)
    uvs = np.ascontiguousarray(uvs)
    face_counts = np.full(len(faces), 3, dtype=np.int32)
    face_indices = np.ascontiguousarray(faces.reshape(-1))

    usdz_path = Path(usdz_path).resolve()
    usdz_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".usdz-", dir=usdz_path.parent) as temporary:
        temporary_dir = Path(temporary)
        stage_path = temporary_dir / "model.usdc"
        texture_path = temporary_dir / "baseColor.png"
        texture.save(texture_path, format="PNG", optimize=True)

        stage = Usd.Stage.CreateNew(str(stage_path))
        if stage is None:
            raise UsdzExportError("OpenUSD could not create the USD stage.")
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)

        root = UsdGeom.Xform.Define(stage, "/Furniture")
        stage.SetDefaultPrim(root.GetPrim())
        Usd.ModelAPI(root.GetPrim()).SetKind(Kind.Tokens.component)

        usd_mesh = UsdGeom.Mesh.Define(stage, "/Furniture/Mesh")
        usd_mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(vertices))
        usd_mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(face_counts))
        usd_mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(face_indices))
        usd_mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(normals))
        usd_mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        usd_mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        usd_mesh.CreateDoubleSidedAttr(True)
        extent = np.ascontiguousarray(
            np.stack((vertices.min(axis=0), vertices.max(axis=0))).astype(np.float32)
        )
        usd_mesh.CreateExtentAttr(Vt.Vec3fArray.FromNumpy(extent))

        st_primvar = UsdGeom.PrimvarsAPI(usd_mesh).CreatePrimvar(
            "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex,
        )
        st_primvar.Set(Vt.Vec2fArray.FromNumpy(uvs))

        material = UsdShade.Material.Define(stage, "/Furniture/Material")
        surface = UsdShade.Shader.Define(stage, "/Furniture/Material/PreviewSurface")
        surface.CreateIdAttr("UsdPreviewSurface")
        surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(
            _material_number(mesh, "roughnessFactor", 0.78)
        )
        surface.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(
            _material_number(mesh, "metallicFactor", 0.0)
        )
        surface.CreateInput("useSpecularWorkflow", Sdf.ValueTypeNames.Int).Set(0)

        texcoord = UsdShade.Shader.Define(stage, "/Furniture/Material/TexCoordReader")
        texcoord.CreateIdAttr("UsdPrimvarReader_float2")
        texcoord.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")
        texcoord.CreateOutput("result", Sdf.ValueTypeNames.Float2)

        texture_shader = UsdShade.Shader.Define(stage, "/Furniture/Material/BaseColorTexture")
        texture_shader.CreateIdAttr("UsdUVTexture")
        texture_shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(
            Sdf.AssetPath(texture_path.name)
        )
        texture_shader.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
        texture_shader.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
        texture_shader.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
        texture_shader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
            texcoord.ConnectableAPI(), "result"
        )
        texture_shader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        texture_shader.CreateOutput("a", Sdf.ValueTypeNames.Float)
        surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            texture_shader.ConnectableAPI(), "rgb"
        )
        surface.CreateInput("opacity", Sdf.ValueTypeNames.Float).ConnectToSource(
            texture_shader.ConnectableAPI(), "a"
        )
        surface.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput().ConnectToSource(surface.ConnectableAPI(), "surface")
        UsdShade.MaterialBindingAPI.Apply(usd_mesh.GetPrim()).Bind(material)

        stage.GetRootLayer().Save()
        stage = None
        usdz_path.unlink(missing_ok=True)
        package = Sdf.ZipFileWriter.CreateNew(str(usdz_path))
        packaged = bool(
            package
            and package.AddFile(str(stage_path), "model.usdc")
            and package.AddFile(str(texture_path), texture_path.name)
            and package.Save()
        )
        if not packaged or not usdz_path.is_file() or usdz_path.stat().st_size == 0:
            if package:
                package.Discard()
            usdz_path.unlink(missing_ok=True)
            raise UsdzExportError("OpenUSD could not package the USDZ asset.")
    return usdz_path
