"""Headless Blender entrypoint: pack external files into the open blend.

Invoked by pack_ops as: blender -b <blend> --python pack_all_blend.py [-- <config.json>]
Config JSON keys: pack_root (optional; for recovering stale text/image paths by basename).

Ensures textures and external texts are embedded for headless/SheepIt (incl. has_data when filepath is dead).
"""

from __future__ import annotations

import json
import sys
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


def _find_file_by_basename(name: str, roots: list[Path]) -> Path | None:
    """Find a file by basename under pack_root (prefer non-_bbp_linked)."""
    if not name:
        return None
    hits: list[Path] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for p in root.rglob(name):
                try:
                    if p.is_file():
                        hits.append(p)
                        if len(hits) >= 12:
                            break
                except OSError:
                    continue
            if len(hits) >= 12:
                break
        except OSError:
            continue
    if not hits:
        return None
    hits.sort(key=lambda p: (1 if "_bbp_linked" in p.parts else 0, -len(p.parts), str(p).lower()))
    return hits[0]


def run_pack_all(pack_root: Path | None = None) -> None:
    """Pack all external files; explicitly pack images/texts that still have data or recoverable paths."""
    blend_dir = Path(bpy.data.filepath).parent
    roots = []
    for r in (pack_root, blend_dir):
        if r is None:
            continue
        p = Path(r)
        if p not in roots:
            roots.append(p)
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

    def _make_text_internal(txt) -> bool:
        """Embed Text in blend (Blender 5.x: text.make_internal; no Text.pack())."""
        if getattr(txt, "is_in_memory", False) and not (getattr(txt, "filepath", None) or ""):
            return True
        win = bpy.context.window_manager.windows[0] if bpy.context.window_manager.windows else None
        if win is None:
            return False
        area = win.screen.areas[0]
        old_type = area.type
        try:
            area.type = "TEXT_EDITOR"
            area.spaces.active.text = txt
            region = area.regions[-1] if area.regions else None
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.text.make_internal()
            return bool(getattr(txt, "is_in_memory", False) or not (txt.filepath or ""))
        except Exception as e:
            print("make_internal failed:", txt.name, e)
            return False
        finally:
            try:
                area.type = old_type
            except Exception:
                pass

    # Embed external text scripts (e.g. handles.py); recover by basename when path is stale.
    texts_packed = 0
    for txt in list(getattr(bpy.data, "texts", []) or []):
        if getattr(txt, "is_linked_packed", False):
            continue
        if getattr(txt, "is_in_memory", False) and not (getattr(txt, "filepath", None) or ""):
            continue
        fp = getattr(txt, "filepath", None) or ""
        if not fp or fp in ("", "<builtin>", "<memory>"):
            continue
        abs_p = Path(bpy.path.abspath(fp))
        if not abs_p.is_file():
            found = _find_file_by_basename(abs_p.name or Path(fp).name, roots)
            if found is not None:
                print(f"Recovered text path: {txt.name}: {abs_p} -> {found}")
                txt.filepath = str(found)
                try:
                    txt.filepath = bpy.path.relpath(str(found))
                except Exception:
                    pass
                try:
                    txt.reload()
                except Exception:
                    pass
            elif not len(txt.lines):
                print(f"UNPACKED_TEXT: {txt.name} | fp={fp}")
                continue
        if _make_text_internal(txt):
            texts_packed += 1
            print("Packed text (make_internal):", txt.name)
        else:
            print(f"UNPACKED_TEXT: {txt.name} | fp={txt.filepath}")
    if texts_packed:
        print("Packed", texts_packed, "external text script(s)")

    # Report leftovers that still break SheepIt / headless GPU texture loads.
    for img in list(bpy.data.images):
        if getattr(img, "source", "FILE") not in ("FILE", "TILED"):
            continue
        if getattr(img, "packed_file", None) and getattr(img.packed_file, "size", 0) > 0:
            continue
        fp = getattr(img, "filepath", None) or ""
        if fp in ("", "<builtin>", "<memory>"):
            continue
        lib = getattr(img, "library", None)
        lib_label = lib.filepath if lib else "(local)"
        print(f"UNPACKED_IMAGE: {img.name} | source={img.source} | fp={fp} | lib={lib_label}")

    try:
        bpy.ops.wm.save_mainfile(compress=True)
    except Exception as e:
        print("Save failed:", e)


def main() -> None:
    """Parse optional config JSON from argv after --."""
    pack_root = None
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1 :]
        if args:
            cfg_path = Path(args[0])
            if cfg_path.is_file():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict) and payload.get("pack_root"):
                    pack_root = Path(payload["pack_root"])
    run_pack_all(pack_root=pack_root)


if __name__ == "__main__":
    main()
