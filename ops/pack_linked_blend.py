"""Headless Blender entrypoint: pack linked .blend libraries into the open file.

Invoked by pack_ops as: blender -b <blend> --python pack_linked_blend.py -- <config.json>
Config JSON keys: max_size_bytes (optional; default 2GiB), pack_root (optional), search_roots (optional list).

Blender's pack_libraries() aborts the whole op if any library path is absolute (different path anchors) or otherwise unpackable — so we localize those into _bbp_linked/ as blend-relative paths first. Missing libs are recovered by basename under pack_root/search_roots (never removed — removing creates ghost Library stubs that reappear on the next load/save).
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


def _lib_is_embedded(lib) -> bool:
    """True when library bytes already live in this .blend (packed_file / archive / archive parent).

    Blender 5.0+ pack-linked assets (any ID type) use an ``is_archive`` child under a parent with ``archive_libraries``. The library filepath is only an origin marker and may be dead; data is not missing.
    """
    if lib is None:
        return False
    if getattr(lib, "packed_file", None) is not None:
        return True
    if getattr(lib, "is_archive", False):
        return True
    archives = getattr(lib, "archive_libraries", None)
    try:
        return bool(archives is not None and len(archives) > 0)
    except Exception:
        return False


def _ensure_embedded_lib_packable_path(lib, blend_dir: Path, pack_root: Path, roots: list[Path]) -> str:
    """Make pack-linked/archive origin paths pack_libraries-safe without treating them as missing assets.

    ``pack_libraries()`` aborts the *entire* op on any absolute library filepath — including archive parents whose bytes already live in the .blend. Rebind/localize (or stub) so the path is blend-relative under pack_root.
    Returns: 'ok' | 'rebound' | 'localized' | 'stubbed' | 'missing'
    """
    fp = lib.filepath or ""
    if fp.startswith("//"):
        try:
            abs_path = _lib_abs(lib)
            if _is_under(abs_path, pack_root) or abs_path.is_file():
                return "ok"
        except Exception:
            pass
    status = _ensure_packable_lib_path(lib, blend_dir, pack_root)
    if status != "missing":
        return status
    # Dead origin marker: find a real twin, else write a tiny stub under pack_root/_bbp_linked.
    name = _blend_basename(Path(fp).name or lib.name) or "embedded_lib.blend"
    found = _find_lib_by_basename(name, roots)
    if found is not None:
        _rebind_library(lib, found)
        return _ensure_packable_lib_path(lib, blend_dir, pack_root)
    stub = pack_root / "_bbp_linked" / name
    try:
        stub.parent.mkdir(parents=True, exist_ok=True)
        if not stub.is_file():
            # Placeholder so filepath can be blend-relative; archive child holds the real packed IDs.
            stub.write_bytes(b"BBP_PACK_LINKED_ORIGIN_STUB\n")
            print(f"  Stub pack-linked origin path: {stub}")
        _rebind_library(lib, stub)
        return "stubbed"
    except OSError as e:
        print(f"  ERROR stubbing pack-linked origin for {lib.name}: {e}")
        return "missing"


def _ensure_packable_lib_path(lib, blend_dir: Path, pack_root: Path) -> str:
    """Make library path pack_libraries-safe. Prefer rebind-in-place under pack_root; copy only to pack_root/_bbp_linked.

    Copying into blend_dir/_bbp_linked (e.g. Char/Rig/_bbp_linked) breaks nested relatives like //../Lib/ into Char/Rig/Lib/ ghosts.
    Returns: 'ok' | 'rebound' | 'localized' | 'missing'
    """
    try:
        abs_path = _lib_abs(lib)
    except Exception:
        return "missing"
    if not abs_path.is_file():
        return "missing"

    # Already under the pack tree: make blend-relative without copying (keeps original folder layout intact).
    if _is_under(abs_path, pack_root):
        fp = lib.filepath or ""
        if fp.startswith("//"):
            try:
                # Relative but resolves outside pack_root (broken nested relative) — rebind to real abs under pack_root.
                if not _is_under(Path(bpy.path.abspath(fp)), pack_root):
                    _rebind_library(lib, abs_path)
                    print(f"  Rebound broken relative under pack tree: {lib.name} -> {lib.filepath}")
                    return "rebound"
            except Exception:
                _rebind_library(lib, abs_path)
                return "rebound"
            return "ok"
        _rebind_library(lib, abs_path)
        print(f"  Rebound absolute under pack tree: {lib.name} -> {lib.filepath}")
        return "rebound"

    # Outside pack tree (different path anchors): stage under pack_root/_bbp_linked only.
    dest = _local_lib_dest(pack_root, abs_path)
    try:
        if not dest.is_file():
            shutil.copy2(abs_path, dest)
            print(f"  Localized library copy: {abs_path} -> {dest}")
        else:
            print(f"  Localized library reuse: {dest}")
    except OSError as e:
        print(f"  ERROR copying library for localization: {abs_path}: {e}")
        return "missing"
    _rebind_library(lib, dest)
    print(f"  Rebound {lib.name} -> {lib.filepath}")
    return "localized"


def _local_lib_dest(pack_root: Path, src: Path) -> Path:
    """Destination under pack_root/_bbp_linked/ for a library file copy (never per-blend Lib/_bbp_linked)."""
    dest_dir = pack_root / "_bbp_linked"
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


def _localize_library(lib, pack_root: Path, src: Path | None = None) -> Path | None:
    """Copy lib into pack_root/_bbp_linked/ and rebind to a blend-relative path. Returns new abs or None."""
    src = src or _lib_abs(lib)
    if not src.is_file():
        return None
    dest = _local_lib_dest(pack_root, src)
    try:
        if not dest.is_file():
            shutil.copy2(src, dest)
            print(f"  Localized library copy: {src} -> {dest}")
        else:
            print(f"  Localized library reuse: {dest}")
    except OSError as e:
        print(f"  ERROR copying library for localization: {src}: {e}")
        return None
    _rebind_library(lib, dest)
    print(f"  Rebound {lib.name} -> {lib.filepath}")
    return dest


def _blend_basename(name: str) -> str:
    """Normalize 'foo.blend.004' / path / datablock name to 'foo.blend'."""
    if not name:
        return ""
    base = Path(name).name
    lower = base.lower()
    if lower.endswith(".blend"):
        return base
    idx = lower.rfind(".blend")
    if idx >= 0:
        return base[: idx + len(".blend")]
    return base


def _find_file_by_basename(name: str, roots: list[Path], *, suffix: str | None = None) -> Path | None:
    """Find a file by basename under pack_root/search_roots (prefer non-_bbp_linked)."""
    name = _blend_basename(name) if (suffix and suffix.lower() == ".blend") else (Path(name).name if name else "")
    if not name:
        return None
    if suffix and not name.lower().endswith(suffix.lower()):
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
    # Prefer real Lib/ layout over _bbp_linked copies; demote materialized nested-relative ghosts (sibling of _bbp_linked).
    def _rank(p: Path) -> tuple:
        nested_ghost = False
        try:
            # Char/Rig/Lib/foo.blend next to Char/Rig/_bbp_linked/ is usually a //../Lib landmine slot.
            nested_ghost = (p.parent.parent / "_bbp_linked").is_dir()
        except (OSError, IndexError):
            nested_ghost = False
        return (
            1 if "_bbp_linked" in p.parts else 0,
            1 if nested_ghost else 0,
            -len(p.parts),
            str(p).lower(),
        )

    hits.sort(key=_rank)
    return hits[0]


def _find_lib_by_basename(name: str, roots: list[Path]) -> Path | None:
    """Find a .blend by basename under pack_root/search_roots."""
    return _find_file_by_basename(name, roots, suffix=".blend")


def _rebind_library(lib, new_abs: Path) -> None:
    """Point library at new_abs, prefer blend-relative. Do not reload here — reload can invalidate sibling Library RNA mid-loop."""
    lib.filepath = str(new_abs)
    try:
        lib.filepath = bpy.path.relpath(str(new_abs))
    except Exception:
        pass


def _materialize_missing_lib(lib, pack_root: Path, roots: list[Path]) -> str:
    """Recover a library whose filepath does not exist on disk.

    If the broken path resolves under pack_root (e.g. Char/Rig/Lib ghost from nested //../Lib), copy the real .blend into that exact slot so the relative path stays valid for nested packed parents. Otherwise rebind to a found file.
    Returns: 'materialized' | 'rebound' | 'missing'
    """
    try:
        abs_path = _lib_abs(lib)
        lib_fp = lib.filepath or ""
    except Exception:
        return "missing"
    if abs_path.is_file():
        return "rebound"
    found = _find_lib_by_basename(abs_path.name or Path(lib_fp).name or lib.name, roots)
    if found is None:
        return "missing"
    # Fill the expected pack-tree slot when the ghost path sits under pack_root.
    try:
        under_pack = _is_under(abs_path, pack_root) or _is_under(abs_path.parent, pack_root)
    except Exception:
        under_pack = False
    if under_pack and abs_path.suffix.lower() == ".blend":
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            if not abs_path.is_file():
                shutil.copy2(found, abs_path)
                print(f"  Materialized ghost lib path: {found} -> {abs_path}")
            return "materialized"
        except OSError as e:
            print(f"  Materialize failed ({e}); rebinding instead: {lib.name}")
    _rebind_library(lib, found)
    print(f"  Rebound missing lib {lib.name}: {lib_fp} -> {found}")
    return "rebound"


def _make_text_internal(txt) -> bool:
    """Embed a Text datablock in the blend (Blender 5.x: no Text.pack(); use text.make_internal)."""
    if getattr(txt, "is_in_memory", False) and not (getattr(txt, "filepath", None) or ""):
        return True
    win = bpy.context.window_manager.windows[0] if bpy.context.window_manager.windows else None
    if win is None:
        return False
    area = win.screen.areas[0]
    old_type = area.type
    try:
        area.type = "TEXT_EDITOR"
        space = area.spaces.active
        space.text = txt
        region = area.regions[-1] if area.regions else None
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.text.make_internal()
        return bool(getattr(txt, "is_in_memory", False) or not (txt.filepath or ""))
    except Exception as e:
        print(f"  make_internal failed for {txt.name}: {e}")
        return False
    finally:
        try:
            area.type = old_type
        except Exception:
            pass


def _pack_external_texts(roots: list[Path]) -> list[str]:
    """Embed external text scripts; recover by basename when filepath is stale. Returns still-external names."""
    unpacked: list[str] = []
    for txt in list(getattr(bpy.data, "texts", []) or []):
        if getattr(txt, "is_in_memory", False) and not (getattr(txt, "filepath", None) or ""):
            continue
        # Already packed via linked-packed / no external path
        if getattr(txt, "is_linked_packed", False):
            continue
        fp = getattr(txt, "filepath", None) or ""
        if not fp or fp in ("", "<builtin>", "<memory>"):
            continue
        abs_p = Path(bpy.path.abspath(fp))
        if not abs_p.is_file():
            found = _find_file_by_basename(abs_p.name or Path(fp).name, roots)
            if found is None:
                # Content may already be loaded in lines — still try make_internal.
                if not len(txt.lines):
                    print(f"UNPACKED_TEXT: {txt.name} | fp={fp}")
                    unpacked.append(txt.name)
                    continue
            else:
                print(f"  Recovered text path: {txt.name}: {abs_p} -> {found}")
                txt.filepath = str(found)
                try:
                    txt.filepath = bpy.path.relpath(str(found))
                except Exception:
                    pass
                try:
                    txt.reload()
                except Exception:
                    pass
        if _make_text_internal(txt):
            print(f"Packed text (make_internal): {txt.name}")
        else:
            print(f"UNPACKED_TEXT: {txt.name} | fp={txt.filepath}")
            unpacked.append(txt.name)
    return unpacked


def run_pack_linked(max_size_bytes: int, pack_root: Path | None = None, search_roots: list[Path] | None = None) -> None:
    """Localize libs with different path anchors (or escaping the blend dir), then pack_libraries + pack_all and save."""
    blend_path = Path(bpy.data.filepath)
    blend_dir = blend_path.parent
    pack_root = Path(pack_root) if pack_root else blend_dir
    roots = [pack_root] + [Path(r) for r in (search_roots or []) if r]
    # Dedupe roots
    seen_roots: set[str] = set()
    uniq_roots: list[Path] = []
    for r in roots:
        try:
            k = str(r.resolve()).lower()
        except OSError:
            k = str(r).lower()
        if k not in seen_roots:
            seen_roots.add(k)
            uniq_roots.append(r)

    print("=== Pack Linked Operation ===")
    print("Processing:", blend_path.name)
    print("Libraries found:", len(bpy.data.libraries))
    print("Pack root:", pack_root)

    missing_files: list[str] = []
    oversized_files: list[str] = []
    localized = 0
    recovered = 0

    # Snapshot names — filepath edits / duplicate localization can invalidate Library RNA handles.
    lib_names = [lib.name for lib in bpy.data.libraries]
    for lib_name in lib_names:
        lib = bpy.data.libraries.get(lib_name)
        if lib is None:
            print(f"  Library (gone during prepare): {lib_name}")
            continue
        if _lib_is_embedded(lib):
            # Still must be blend-relative — absolute archive-origin paths abort pack_libraries for *all* libs.
            estatus = _ensure_embedded_lib_packable_path(lib, blend_dir, pack_root, uniq_roots)
            print(f"  Library (embedded, path {estatus}): {lib_name}")
            continue
        try:
            abs_path = _lib_abs(lib)
            lib_fp = lib.filepath
        except ReferenceError:
            print(f"  Library (RNA removed during prepare): {lib_name}")
            continue
        except Exception as e:
            missing_files.append(str(getattr(lib, "filepath", lib_name)))
            print(f"  Library (bad path): {lib_name}, err: {e}")
            continue
        if not abs_path.is_file():
            lib = bpy.data.libraries.get(lib_name)
            if lib is None:
                continue
            mstat = _materialize_missing_lib(lib, pack_root, uniq_roots)
            if mstat == "missing":
                missing_files.append(str(abs_path))
                print(f"  Library (MISSING): {lib_name}, path: {lib_fp}")
                continue
            lib = bpy.data.libraries.get(lib_name)
            if lib is None:
                continue
            try:
                abs_path = _lib_abs(lib)
            except Exception:
                abs_path = Path()
            if not abs_path.is_file():
                missing_files.append(str(lib.filepath or lib_name))
                print(f"  Library (MISSING after materialize): {lib_name}")
                continue
            recovered += 1
            print(f"  Recovered missing library ({mstat}): {lib_name} -> {abs_path}")
        try:
            file_size = abs_path.stat().st_size
        except OSError as e:
            missing_files.append(str(abs_path))
            print(f"  Library (inaccessible): {lib_name}, path: {lib_fp}, err: {e}")
            continue
        size_gb = file_size / (1024 * 1024 * 1024)
        if file_size > max_size_bytes:
            oversized_files.append(str(abs_path))
            print(f"  Library (OVER limit, cannot pack): {lib_name}, path: {lib_fp}, size: {size_gb:.2f} GB")
            continue
        lib = bpy.data.libraries.get(lib_name)
        if lib is None:
            print(f"  Library (gone before localize): {lib_name}")
            continue
        status = _ensure_packable_lib_path(lib, blend_dir, pack_root)
        if status == "missing":
            missing_files.append(str(abs_path))
            print(f"  Library (MISSING after ensure): {lib_name}")
            continue
        if status == "localized":
            localized += 1
        lib = bpy.data.libraries.get(lib_name)
        fp_out = lib.filepath if lib else str(abs_path)
        print(f"  Library ({status}, {size_gb:.2f} GB): {lib_name}, path: {fp_out}")

    print(f"Recovered {recovered} missing library path(s) by basename under pack tree")
    print(f"Localized {localized} library path(s) with different path anchors (or outside blend dir)")

    # Do NOT remove missing libraries — Blender recreates ghost Library stubs from linked IDs on the next load/save (silent farm missings). Relocate or report instead.
    if oversized_files:
        print(f"WARNING: {len(oversized_files)} libraries over size limit remain external (not removed)")

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

    def _seal_unpacked_libraries() -> list[str]:
        """Recover/localize any still-unpacked libs and retry pack_libraries. Returns names still unpacked."""
        # Always sanitize absolute pack-linked origins first — one absolute path aborts pack_libraries entirely.
        for lib_name in [lib.name for lib in bpy.data.libraries]:
            lib = bpy.data.libraries.get(lib_name)
            if lib is None:
                continue
            if _lib_is_embedded(lib):
                _ensure_embedded_lib_packable_path(lib, blend_dir, pack_root, uniq_roots)
        still_names = [lib.name for lib in bpy.data.libraries if not _lib_is_embedded(lib)]
        if not still_names:
            # Embedded-only: still retry pack_libraries in case paths were just made relative.
            _do_pack_libraries()
            return [lib.name for lib in bpy.data.libraries if not _lib_is_embedded(lib)]
        print(f"Seal pass: {len(still_names)} unpacked libraries — recover/localize then retry pack_libraries")
        for lib_name in still_names:
            lib = bpy.data.libraries.get(lib_name)
            if lib is None or _lib_is_embedded(lib):
                continue
            try:
                abs_path = _lib_abs(lib)
                lib_fp = lib.filepath
            except (ReferenceError, Exception):
                abs_path = Path()
                lib_fp = ""
            if not abs_path.is_file():
                mstat = _materialize_missing_lib(lib, pack_root, uniq_roots)
                if mstat == "missing":
                    print(f"  Seal MISSING: {lib_name} fp={lib_fp}")
                    continue
                print(f"  Seal recovered ({mstat}): {lib_name}")
            lib = bpy.data.libraries.get(lib_name)
            if lib is None:
                continue
            _ensure_packable_lib_path(lib, blend_dir, pack_root)
        try:
            bpy.ops.file.make_paths_relative()
        except Exception:
            pass
        _do_pack_libraries()
        left = []
        for lib in bpy.data.libraries:
            if not _lib_is_embedded(lib):
                left.append(lib.name)
                print(f"UNPACKED_LIB: {lib.name} | fp={lib.filepath}")
        return left

    def _collect_missing_ids() -> list[str]:
        """Linked IDs with is_missing (e.g. SomeMat @ SomeLib.blend.004 ghost)."""
        hits: list[str] = []
        for coll_name in ("materials", "objects", "meshes", "node_groups", "images", "texts", "worlds"):
            coll = getattr(bpy.data, coll_name, None)
            if coll is None:
                continue
            for block in coll:
                if not getattr(block, "is_missing", False):
                    continue
                lib = getattr(block, "library", None)
                lib_name = lib.name if lib else "?"
                lib_fp = lib.filepath if lib else ""
                label = f"{block.name} @ {lib_name}"
                hits.append(label)
                print(f"MISSING_ID: {coll_name}/{block.name} | lib={lib_name} | fp={lib_fp}")
        return hits

    def _heal_missing_id_libraries() -> int:
        """Materialize/rebind libraries that own is_missing IDs (nested Lib path ghosts etc.)."""
        healed = 0
        seen: set[str] = set()
        for coll_name in ("materials", "objects", "meshes", "node_groups", "images", "texts", "worlds"):
            coll = getattr(bpy.data, coll_name, None)
            if coll is None:
                continue
            for block in coll:
                if not getattr(block, "is_missing", False):
                    continue
                lib = getattr(block, "library", None)
                if lib is None or lib.name in seen:
                    continue
                seen.add(lib.name)
                lib_name = lib.name
                lib_fp = lib.filepath or ""
                mstat = _materialize_missing_lib(lib, pack_root, uniq_roots)
                if mstat == "missing":
                    print(f"  Heal MISSING: {lib_name} fp={lib_fp}")
                    continue
                lib = bpy.data.libraries.get(lib_name)
                if lib is None:
                    continue
                _ensure_packable_lib_path(lib, blend_dir, pack_root)
                # Reload after path is packable so is_missing clears (safe: one lib at a time by name).
                lib = bpy.data.libraries.get(lib_name)
                if lib is not None and _lib_abs(lib).is_file():
                    try:
                        lib.reload()
                        print(f"  Reloaded healed library: {lib_name}")
                    except Exception as e:
                        print(f"  Reload after heal skipped for {lib_name}: {e}")
                print(f"  Healed missing-ID library ({mstat}): {lib_name}")
                healed += 1
        return healed

    def _remap_missing_ids_to_packed_twins() -> int:
        """Point is_missing IDs at same-named IDs on an already-packed library with the same .blend basename."""
        remapped = 0
        for coll_name in ("materials", "objects", "meshes", "node_groups", "images", "texts", "worlds"):
            coll = getattr(bpy.data, coll_name, None)
            if coll is None:
                continue
            # Index healthy packed/embedded twins: basename -> {id_name -> block}
            twins: dict[str, dict[str, object]] = {}
            for block in coll:
                lib = getattr(block, "library", None)
                if lib is None or not _lib_is_embedded(lib):
                    continue
                if getattr(block, "is_missing", False):
                    continue
                bname = _blend_basename(lib.filepath or lib.name)
                twins.setdefault(bname, {})[block.name] = block
            for block in list(coll):
                if not getattr(block, "is_missing", False):
                    continue
                lib = getattr(block, "library", None)
                if lib is None:
                    continue
                bname = _blend_basename(lib.filepath or lib.name)
                good = twins.get(bname, {}).get(block.name)
                if good is None:
                    continue
                try:
                    block.user_remap(good)
                    print(f"  Remapped missing {coll_name}/{block.name} -> packed twin on {bname}")
                    remapped += 1
                except Exception as e:
                    print(f"  Remap failed {coll_name}/{block.name}: {e}")
        return remapped

    if not _do_pack_libraries():
        print("Retry: ensuring packable paths into pack_root/_bbp_linked/")
        for lib_name in [lib.name for lib in bpy.data.libraries]:
            lib = bpy.data.libraries.get(lib_name)
            if lib is None:
                continue
            if _lib_is_embedded(lib):
                _ensure_embedded_lib_packable_path(lib, blend_dir, pack_root, uniq_roots)
                continue
            _ensure_packable_lib_path(lib, blend_dir, pack_root)
        try:
            bpy.ops.file.make_paths_relative()
        except Exception:
            pass
        _do_pack_libraries()

    unpacked_after = _seal_unpacked_libraries()
    if _heal_missing_id_libraries():
        _do_pack_libraries()
        unpacked_after = _seal_unpacked_libraries()
    if _remap_missing_ids_to_packed_twins():
        # Users moved off ghost stubs; seal again in case anything became packable.
        unpacked_after = _seal_unpacked_libraries()

    print("Libraries after pack:", len(bpy.data.libraries))
    for lib in bpy.data.libraries:
        print(f"  - {lib.name}: embedded={_lib_is_embedded(lib)} packed_file={bool(getattr(lib, 'packed_file', None))} archive={bool(getattr(lib, 'is_archive', False))} fp={lib.filepath}")

    try:
        print("Running pack_all() to ensure all data is packed...")
        bpy.ops.file.pack_all()
        print("pack_all() completed")
    except Exception as e:
        print("pack_all() warning:", e)

    unpacked_texts = _pack_external_texts(uniq_roots)

    _enable_autopack()
    print("Saving file...")
    bpy.ops.wm.save_mainfile(compress=True)

    # Final honesty check — ghost Lib.blend.004 stays unpacked / is_missing even when twins are packed.
    unpacked_after = []
    for lib in bpy.data.libraries:
        if _lib_is_embedded(lib):
            continue
        unpacked_after.append(lib.name)
        print(f"UNPACKED_LIB: {lib.name} | fp={lib.filepath}")
        # Also flag path-missing ghosts explicitly for the host parser.
        try:
            abs_p = _lib_abs(lib)
            if not abs_p.is_file():
                print(f"MISSING_FILE: {lib.name} | fp={lib.filepath} | abs={abs_p}")
        except Exception:
            print(f"MISSING_FILE: {lib.name} | fp={lib.filepath}")
    missing_ids = _collect_missing_ids()

    packed_n = sum(1 for lib in bpy.data.libraries if _lib_is_embedded(lib))
    print(
        f"=== Pack Linked Complete (packed_libs={packed_n}, "
        f"unpacked_left={len(unpacked_after)}, unpacked_texts={len(unpacked_texts)}, "
        f"missing_ids={len(missing_ids)}, missing={len(missing_files)}, oversized={len(oversized_files)}, "
        f"localized={localized}, recovered={recovered}) ==="
    )
    for mf in missing_files:
        print("MISSING_FILE:", mf)
    for of in oversized_files:
        print("OVERSIZED_FILE:", of)
    for err in pack_errors:
        print("PACK_ERROR:", err)
    for name in unpacked_after:
        print("MISSING_FILE:", name)
    for name in unpacked_texts:
        print("MISSING_FILE:", name)
    for label in missing_ids:
        print("MISSING_FILE:", label)
    if unpacked_after or unpacked_texts or missing_ids:
        # Non-zero exit so the host packer surfaces leftovers.
        sys.exit(3)


def main() -> None:
    """Parse optional config JSON from argv after --."""
    max_size_bytes = 2 * 1024 * 1024 * 1024
    pack_root = None
    search_roots: list[Path] = []
    argv = sys.argv
    if "--" in argv:
        args = argv[argv.index("--") + 1 :]
        if args:
            cfg_path = Path(args[0])
            if cfg_path.is_file():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    if payload.get("max_size_bytes") is not None:
                        max_size_bytes = int(payload["max_size_bytes"])
                    if payload.get("pack_root"):
                        pack_root = Path(payload["pack_root"])
                    search_roots = [Path(p) for p in (payload.get("search_roots") or [])]
    run_pack_linked(max_size_bytes, pack_root=pack_root, search_roots=search_roots)


if __name__ == "__main__":
    main()
