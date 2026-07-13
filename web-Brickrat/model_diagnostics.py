"""Dependency-free GLB and USDZ inspection for export validation and debugging."""

from __future__ import annotations

import argparse
import json
import struct
import zipfile
from pathlib import Path


GLB_MAGIC = b"glTF"
GLB_JSON_CHUNK = 0x4E4F534A
TEXTURE_SUFFIXES = {".png", ".jpg", ".jpeg"}
USD_SUFFIXES = {".usd", ".usda", ".usdc"}


def _read_glb_json(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise ValueError("GLB header is incomplete.")
        magic, version, declared_length = struct.unpack("<4sII", header)
        if magic != GLB_MAGIC or version != 2:
            raise ValueError("File is not a GLB 2.0 asset.")
        if declared_length != path.stat().st_size:
            raise ValueError("GLB declared length does not match its file size.")

        while stream.tell() < declared_length:
            chunk_header = stream.read(8)
            if len(chunk_header) != 8:
                raise ValueError("GLB chunk header is incomplete.")
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            chunk = stream.read(chunk_length)
            if len(chunk) != chunk_length:
                raise ValueError("GLB chunk is incomplete.")
            if chunk_type == GLB_JSON_CHUNK:
                return json.loads(chunk.rstrip(b"\x00 \t\r\n").decode("utf-8"))
    raise ValueError("GLB has no JSON chunk.")


def inspect_glb(path: str | Path) -> dict:
    path = Path(path)
    report = {
        "path": str(path),
        "exists": path.is_file(),
        "file_size": path.stat().st_size if path.is_file() else 0,
        "valid": False,
    }
    if not path.is_file():
        report["error"] = "GLB file does not exist."
        return report

    try:
        document = _read_glb_json(path)
        meshes = document.get("meshes", [])
        materials = document.get("materials", [])
        textures = document.get("textures", [])
        images = document.get("images", [])
        buffers = document.get("buffers", [])
        primitives = [
            primitive
            for mesh in meshes
            for primitive in mesh.get("primitives", [])
        ]
        attributes = [primitive.get("attributes", {}) for primitive in primitives]
        embedded_images = [
            image for image in images
            if "bufferView" in image or str(image.get("uri", "")).startswith("data:")
        ]
        external_images = [
            image.get("uri") for image in images
            if image.get("uri") and not str(image["uri"]).startswith("data:")
        ]
        external_buffers = [
            buffer.get("uri") for buffer in buffers
            if buffer.get("uri") and not str(buffer["uri"]).startswith("data:")
        ]
        base_color_texture_count = sum(
            1 for material in materials
            if material.get("pbrMetallicRoughness", {}).get("baseColorTexture") is not None
        )
        position_accessors = [
            attributes_["POSITION"] for attributes_ in attributes
            if "POSITION" in attributes_
        ]
        accessors = document.get("accessors", [])
        vertex_count = sum(
            accessors[index].get("count", 0)
            for index in position_accessors
            if isinstance(index, int) and 0 <= index < len(accessors)
        )
        report.update({
            "mesh_count": len(meshes),
            "primitive_count": len(primitives),
            "vertex_count": vertex_count,
            "material_count": len(materials),
            "texture_count": len(textures),
            "image_count": len(images),
            "embedded_image_count": len(embedded_images),
            "external_image_uris": external_images,
            "external_buffer_uris": external_buffers,
            "base_color_texture_count": base_color_texture_count,
            "has_uv": any("TEXCOORD_0" in item for item in attributes),
            "has_vertex_colors": any("COLOR_0" in item for item in attributes),
        })
        report["valid"] = bool(
            report["file_size"] > 0
            and report["mesh_count"] > 0
            and report["primitive_count"] > 0
            and report["material_count"] > 0
            and report["has_uv"]
            and report["base_color_texture_count"] > 0
            and report["embedded_image_count"] == report["image_count"]
            and report["image_count"] > 0
            and not report["external_buffer_uris"]
        )
        if not report["valid"]:
            report["error"] = (
                "GLB is missing an embedded base-colour texture, UVs, material, or mesh, "
                "or it references an external buffer."
            )
    except Exception as exc:
        report["error"] = str(exc)
    return report


def inspect_usdz(path: str | Path) -> dict:
    path = Path(path)
    report = {
        "path": str(path),
        "exists": path.is_file(),
        "file_size": path.stat().st_size if path.is_file() else 0,
        "valid": False,
        "package_files": [],
        "texture_files": [],
        "usd_files": [],
    }
    if not path.is_file():
        report["error"] = "USDZ file does not exist."
        return report
    try:
        with zipfile.ZipFile(path) as package:
            entries = [item for item in package.infolist() if not item.is_dir()]
            names = [item.filename for item in entries]
            bad_entries = package.testzip()
            stored = all(item.compress_type == zipfile.ZIP_STORED for item in entries)
            unencrypted = all(not item.flag_bits & 0x1 for item in entries)
            data_offsets = []
            for item in entries:
                package.fp.seek(item.header_offset)
                local_header = package.fp.read(30)
                if len(local_header) != 30:
                    raise zipfile.BadZipFile("USDZ local file header is incomplete.")
                signature, *_, filename_length, extra_length = struct.unpack(
                    "<IHHHHHIIIHH", local_header
                )
                if signature != 0x04034B50:
                    raise zipfile.BadZipFile("USDZ local file header is invalid.")
                data_offsets.append(
                    item.header_offset + 30 + filename_length + extra_length
                )
        report["package_files"] = names
        report["texture_files"] = [
            name for name in names if Path(name).suffix.lower() in TEXTURE_SUFFIXES
        ]
        report["usd_files"] = [
            name for name in names if Path(name).suffix.lower() in USD_SUFFIXES
        ]
        report["texture_count"] = len(report["texture_files"])
        report["first_file_is_usd"] = bool(
            names and Path(names[0]).suffix.lower() in USD_SUFFIXES
        )
        report["stored_without_compression"] = stored
        report["unencrypted"] = unencrypted
        report["data_offsets"] = data_offsets
        report["aligned_to_64_bytes"] = bool(
            data_offsets and all(offset % 64 == 0 for offset in data_offsets)
        )
        report["valid"] = bool(
            report["file_size"] > 0
            and report["usd_files"]
            and report["texture_files"]
            and bad_entries is None
            and report["first_file_is_usd"]
            and report["stored_without_compression"]
            and report["unencrypted"]
            and report["aligned_to_64_bytes"]
        )
        if not report["valid"]:
            report["error"] = (
                "USDZ is missing a default USD scene or packaged texture, or its "
                "ZIP storage/alignment does not meet the USDZ specification."
            )
    except (OSError, zipfile.BadZipFile) as exc:
        report["error"] = str(exc)
    return report


def inspect_model(glb_path: str | Path, usdz_path: str | Path | None = None) -> dict:
    report = {"glb": inspect_glb(glb_path)}
    if usdz_path is not None:
        report["usdz"] = inspect_usdz(usdz_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect generated GLB/USDZ model assets.")
    parser.add_argument("glb", type=Path, help="Path to model.glb")
    parser.add_argument("--usdz", type=Path, help="Optional path to model.usdz")
    args = parser.parse_args()
    report = inspect_model(args.glb, args.usdz)
    print(json.dumps(report, indent=2))
    return 0 if report["glb"]["valid"] and (
        args.usdz is None or report["usdz"]["valid"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
