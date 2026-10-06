"""Headless Blender entrypoint: pack external files into the open blend.

Invoked by pack_ops as: blender -b <blend> --python pack_all_blend.py
Ensures textures are embedded for headless/SheepIt (incl. has_data when filepath is dead).
"""

from __future__ import annotations

from pathlib import Path

import bpy


def _enable_autopack() -> None:
    """Turn on whatever autopack preference flag this Blender build exposes."""
    fp = getattr(bpy.context.preferences, "filepaths", None)
    if not fp:
        return
    for k in ("use_autopack", "use_autopack_files", "use_auto_pack"):
        if hasattr(fp, k):
            try:
                setattr(fp, k, True)
            except Exception:
                pass


def _force_load_image(img) -> None:
    """Touch image size so Blender loads pixels when possible."""
    try:
        _ = img.size[0]
    except Exception:
        pass


def run_pack_all() -> None:
    """Pack all external files; explicitly pack images that still have pixels."""
    blend_dir = Path(bpy.data.filepath).parent
    _enable_autopack()

    try:
        bpy.ops.file.make_paths_relative(basedir=str(blend_dir))
    except Exception:
        pass

    try:
        bpy.ops.file.pack_all()
    except Exception as e:
        print("Pack all (operator) failed:", e)

    for img in list(bpy.data.images):
        if getattr(img, "source", "FILE") not in ("FILE", "TILED"):
            continue
        _force_load_image(img)

    try:
        bpy.ops.file.pack_all()
    except Exception as e:
        print("Pack all (second pass) failed:", e)

    n = 0
    for img in list(bpy.data.images):
        if getattr(img, "source", "FILE") not in ("FILE", "TILED"):
            continue
        if getattr(img, "packed_file", None) and getattr(img.packed_file, "size", 0) > 0:
            continue
        # Force-load then pack from memory when filepath is dead (SheepIt GPU texture errors).
        _force_load_image(img)
        size = tuple(getattr(img, "size", (0, 0)) or (0, 0))
        has_pixels = bool(getattr(img, "has_data", False)) or size != (0, 0)
        fp = getattr(img, "filepath", None) or ""
        if not has_pixels and fp in ("", "<builtin>", "<memory>"):
            continue
        if not has_pixels:
            continue
        try:
            if hasattr(img, "pack"):
                img.pack()
                n += 1
        except Exception as e:
            print("Pack image failed:", img.name, e)

    if n:
        print("Packed", n, "images explicitly (incl. has_data / dead filepath)")

    try:
        bpy.ops.wm.save_mainfile(compress=True)
    except Exception as e:
        print("Save failed:", e)


# Blender executes this file with --python; run immediately.
run_pack_all()
