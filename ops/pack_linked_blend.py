"""Headless Blender entrypoint: pack linked .blend libraries into the open file.

Invoked by pack_ops as: blender -b <blend> --python pack_linked_blend.py -- <config.json>
Config JSON keys: max_size_bytes (optional; default 2GiB).

Blender's pack_libraries() aborts the whole op if any library path is absolute (different path anchors) or otherwise unpackable — so we localize those into _bbp_linked/ as blend-relative paths first.
"""

from __future__ import annotations

import json
import shutil
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


def _is_under(path: Path, root: Path) -> bool:
    """True when path resolves inside root."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _lib_abs(lib) -> Path:
    """Absolute filesystem path for a library datablock."""
    return Path(bpy.path.abspath(lib.filepath))


def _needs_localization(lib, blend_dir: Path) -> bool:
    """True when pack_libraries would reject this lib (absolute/different path anchors, or outside blend dir)."""
    fp = lib.filepath or ""
    if not fp.startswith("//"):
        return True
    try:
        return not _is_under(_lib_abs(lib), blend_dir)
    except Exception:
        return True


def _local_lib_dest(blend_dir: Path, src: Path) -> Path:
    """Destination under blend_dir/_bbp_linked/ for a library file copy."""
    dest_dir = blend_dir / "_bbp_linked"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    try:
        if dest.is_file() and dest.resolve() == src.resolve():
            return dest
        if dest.is_file() and dest.stat().st_size == src.stat().st_size:
            return dest
    except OSError:
        pass
    if dest.is_file():
        # Name collision with a different file — disambiguate.
        stamp = abs(hash(str(src).lower().replace("\\", "/"))) % 10_000_000
        dest = dest_dir / f"{src.stem}_{stamp}{src.suffix}"
    return dest


def _localize_library(lib, blend_dir: Path) -> Path | None:
    """Copy lib into _bbp_linked/ and rebind to a blend-relative path. Returns new abs or None."""
    src = _lib_abs(lib)
    if not src.is_file():
        return None
    dest = _local_lib_dest(blend_dir, src)
    try:
        if not dest.is_file():
            shutil.copy2(src, dest)
            print(f"  Localized library copy: {src} -> {dest}")
        else:
            print(f"  Localized library reuse: {dest}")
    except OSError as e:
        print(f"  ERROR copying library for localization: {src}: {e}")
        return None
    try:
        lib.filepath = bpy.path.relpath(str(dest))
    except Exception:
        lib.filepath = str(dest)
        try:
            lib.filepath = bpy.path.relpath(str(dest))
        except Exception as e:
            print(f"  WARNING: could not make localized path relative: {e}")
    print(f"  Rebound {lib.name} -> {lib.filepath}")
    return dest


def run_pack_linked(max_size_bytes: int) -> None:
    """Localize libs with different path anchors (or escaping the blend dir), then pack_libraries + pack_all and save."""
    blend_path = Path(bpy.data.filepath)
    blend_dir = blend_path.parent
    print("=== Pack Linked Operation ===")
    print("Processing:", blend_path.name)
    print("Libraries found:", len(bpy.data.libraries))

    missing_files: list[str] = []
    oversized_files: list[str] = []
    localized = 0

    for lib in list(bpy.data.libraries):
        if getattr(lib, "packed_file", None):
            print(f"  Library (already packed): {lib.name}")
            continue
        try:
            abs_path = _lib_abs(lib)
        except Exception as e:
            missing_files.append(str(lib.filepath))
            print(f"  Library (bad path): {lib.name}, path: {lib.filepath}, err: {e}")
            continue
        if not abs_path.is_file():
            missing_files.append(str(abs_path))
            print(f"  Library (MISSING): {lib.name}, path: {lib.filepath}")
            continue
        try:
            file_size = abs_path.stat().st_size
        except OSError as e:
            missing_files.append(str(abs_path))
            print(f"  Library (inaccessible): {lib.name}, path: {lib.filepath}, err: {e}")
            continue
        size_gb = file_size / (1024 * 1024 * 1024)
        if file_size > max_size_bytes:
            oversized_files.append(str(abs_path))
            print(f"  Library (OVER limit, cannot pack): {lib.name}, path: {lib.filepath}, size: {size_gb:.2f} GB")
            continue
        if _needs_localization(lib, blend_dir):
            new_abs = _localize_library(lib, blend_dir)
            if new_abs is None:
                missing_files.append(str(abs_path))
                continue
            localized += 1
            print(f"  Library (localized, {size_gb:.2f} GB): {lib.name}, path: {lib.filepath}")
        else:
            print(f"  Library (ok, {size_gb:.2f} GB): {lib.name}, path: {lib.filepath}")

    print(f"Localized {localized} library path(s) with different path anchors (or outside blend dir)")

    # Remove missing/oversized library datablocks so pack_libraries won't abort the batch.
    remove_names = set()
    for lib in list(bpy.data.libraries):
        if getattr(lib, "packed_file", None):
            continue
        abs_path = _lib_abs(lib)
        if not abs_path.is_file():
            remove_names.add(lib.name)
            continue
        try:
            if abs_path.stat().st_size > max_size_bytes:
                remove_names.add(lib.name)
        except OSError:
            remove_names.add(lib.name)
    if remove_names:
        print(f"WARNING: removing {len(remove_names)} unpackable libraries before pack_libraries")
        for name in list(remove_names):
            lib = bpy.data.libraries.get(name)
            if not lib:
                continue
            try:
                bpy.data.libraries.remove(lib)
                print(f"  Removed library: {name}")
            except Exception as e:
                print(f"  Could not remove library {name}: {e}")

    try:
        bpy.ops.file.make_paths_relative()
        print("Made paths relative")
    except Exception as e:
        print("Warning: make_paths_relative failed:", e)

    print("Libraries before pack:", len(bpy.data.libraries))
    for lib in bpy.data.libraries:
        print(f"  - {lib.name}: {lib.filepath}")

    pack_errors: list[str] = []

    def _do_pack_libraries() -> bool:
        try:
            print("Starting pack_libraries()...")
            result = bpy.ops.file.pack_libraries()
            print("pack_libraries() returned:", result)
            return True
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            pack_errors.append(msg)
            print("Warning: pack_libraries() failed:", msg)
            return False

    if not _do_pack_libraries():
        # Last resort: localize every remaining unpacked lib, then retry once.
        print("Retry: localizing all remaining libraries into _bbp_linked/")
        for lib in list(bpy.data.libraries):
            if getattr(lib, "packed_file", None):
                continue
            _localize_library(lib, blend_dir)
        try:
            bpy.ops.file.make_paths_relative()
        except Exception:
            pass
        _do_pack_libraries()

    unpacked_after = []
    print("Libraries after pack:", len(bpy.data.libraries))
    for lib in bpy.data.libraries:
        is_packed = bool(getattr(lib, "packed_file", None))
        print(f"  - {lib.name}: packed={is_packed} fp={lib.filepath}")
        if not is_packed:
            unpacked_after.append(lib.name)
            print(f"UNPACKED_LIB: {lib.name}")

    try:
        print("Running pack_all() to ensure all data is packed...")
        bpy.ops.file.pack_all()
        print("pack_all() completed")
    except Exception as e:
        print("pack_all() warning:", e)

    _enable_autopack()
    print("Saving file...")
    bpy.ops.wm.save_mainfile(compress=True)

    packed_n = sum(1 for lib in bpy.data.libraries if getattr(lib, "packed_file", None))
    print(
        f"=== Pack Linked Complete (packed_libs={packed_n}, "
        f"unpacked_left={len(unpacked_after)}, missing={len(missing_files)}, "
        f"oversized={len(oversized_files)}, localized={localized}) ==="
    )
    for mf in missing_files:
        print("MISSING_FILE:", mf)
    for of in oversized_files:
        print("OVERSIZED_FILE:", of)
    for err in pack_errors:
        print("PACK_ERROR:", err)
    if unpacked_after:
        # Non-zero exit so the host packer surfaces a real failure instead of a non-chain-packed blend.
        sys.exit(3)


def main() -> None:
    """Parse optional config JSON from argv after --."""
    max_size_bytes = 2 * 1024 * 1024 * 1024
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1 :]
        if args:
            cfg_path = Path(args[0])
            if cfg_path.is_file():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict) and payload.get("max_size_bytes") is not None:
                    max_size_bytes = int(payload["max_size_bytes"])
    run_pack_linked(max_size_bytes)


if __name__ == "__main__":
    main()
