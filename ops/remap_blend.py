"""Headless Blender entrypoint: remap library/image/text/cache paths after pack copy.

Invoked by pack_ops as: blender -b <blend> --python remap_blend.py -- <config.json>
Config JSON keys: copy_map, search_roots, common_root, target_path, ensure_autopack.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import bpy


def abort_dead_unc(path, kind="", name="", err="") -> None:
    """Print fatal markers for the host packer and exit."""
    print("BBP_FATAL_DEAD_UNC:" + str(path))
    print("BBP_FATAL_DEAD_UNC_KIND:" + str(kind))
    print("BBP_FATAL_DEAD_UNC_NAME:" + str(name))
    print("BBP_FATAL_DEAD_UNC_ERR:" + str(err))
    print("Pack aborted: inaccessible network path (dead UNC):", path)
    sys.exit(2)


def safe_resolve(p, kind="", name="") -> Path:
    """Resolve path or abort on dead UNC / OSError."""
    try:
        return Path(p).resolve()
    except OSError as e:
        abort_dead_unc(p, kind, name, e)
        raise  # unreachable; keeps type checkers happy


def soft_resolve(p) -> Path:
    """Resolve path; on OSError return Path as-is."""
    try:
        return Path(p).resolve()
    except OSError:
        return Path(p)


def norm_key(p) -> str:
    """Normalize path for copy_map lookup (must match pack_ops._norm_copy_map_key)."""
    s = str(soft_resolve(p))
    if os.name == "nt":
        return s.lower().replace("\\", "/")
    return s


def recover_stale_light(stale: Path, search_roots: list[Path]) -> Path | None:
    """Remap-time recovery: datafiles + suffix graft + small bounded walk (libs/texts only)."""
    stale_p = Path(stale)
    try:
        if stale_p.is_file():
            return stale_p.resolve()
    except OSError:
        pass

    name = stale_p.name
    # Stale Blender-install datafiles/assets path → running Blender's DATAFILES tree.
    parts_l = [p.lower() for p in stale_p.parts]
    if name.lower().endswith(".blend") and "datafiles" in parts_l and "assets" in parts_l:
        try:
            df = Path(bpy.utils.system_resource("DATAFILES"))
            try:
                idx = parts_l.index("assets")
                cand = df / "assets" / Path(*stale_p.parts[idx + 1 :])
                if cand.is_file():
                    print("    Recovered stale path:", stale_p, "->", cand)
                    return cand.resolve()
            except (ValueError, OSError):
                pass
            assets = df / "assets"
            if assets.is_dir():
                for sub in assets.iterdir():
                    if sub.is_dir():
                        cand = sub / name
                        if cand.is_file():
                            print("    Recovered stale path:", stale_p, "->", cand)
                            return cand.resolve()
        except Exception:
            pass

    # Graft trailing suffixes onto session search roots (no walk).
    parts = stale_p.parts
    for i in range(len(parts)):
        suffix = parts[i:]
        if not suffix:
            continue
        for root in search_roots:
            try:
                cand = root.joinpath(*suffix)
                if cand.is_file():
                    print("    Recovered stale path:", stale_p, "->", cand)
                    return cand.resolve()
            except OSError:
                continue

    if not name:
        return None

    # Bounded basename walk (caps keep remap under timeout).
    hits: list[Path] = []
    seen: set[str] = set()
    for root in search_roots:
        try:
            if not root.is_dir():
                continue
            root_depth = len(root.parts)
            dirs_seen = 0
            for dirpath, dirnames, filenames in os.walk(root):
                dirs_seen += 1
                if dirs_seen > 800:
                    dirnames.clear()
                    break
                if len(Path(dirpath).parts) - root_depth >= 5:
                    dirnames.clear()
                    continue
                if name in filenames:
                    hit = Path(dirpath) / name
                    try:
                        if hit.is_file():
                            k = norm_key(hit)
                            if k not in seen:
                                seen.add(k)
                                hits.append(hit.resolve())
                                if len(hits) >= 12:
                                    break
                    except OSError:
                        continue
            if len(hits) >= 12:
                break
        except OSError:
            continue

    if len(hits) == 1:
        print("    Recovered stale path:", stale_p, "->", hits[0])
        return hits[0]
    if hits:
        def suffix_score(hit: Path) -> int:
            sp = [x.lower() for x in stale_p.parts]
            hp = [x.lower() for x in Path(hit).parts]
            score = 0
            for a, b in zip(reversed(sp), reversed(hp)):
                if a != b:
                    break
                score += 1
            return score

        scored = sorted(((suffix_score(h), h) for h in hits), key=lambda t: (-t[0], str(t[1]).lower()))
        if scored[0][0] >= 2:
            tied = [h for s, h in scored if s == scored[0][0]]
            if len(tied) == 1:
                print("    Recovered stale path:", stale_p, "->", tied[0])
                return tied[0]
    return None


def resolve_new_abs(
    abs_src: Path,
    copy_map_norm: dict,
    common_root: Path,
    target_path: Path,
    search_roots: list[Path],
    allow_recover: bool = False,
) -> Path | None:
    """Map a source absolute path to its packed-tree (or recovered) location."""
    key = norm_key(abs_src)
    new_abs = None
    try:
        if abs_src.relative_to(target_path):
            new_abs = abs_src
    except Exception:
        pass
    if new_abs is None and key in copy_map_norm:
        new_abs = Path(copy_map_norm[key])
    if new_abs is None:
        for _cm_k, cm_v in copy_map_norm.items():
            if soft_resolve(cm_v) == soft_resolve(abs_src):
                new_abs = soft_resolve(abs_src)
                break
    if new_abs is None:
        try:
            rel_to_root = abs_src.relative_to(common_root)
            new_abs = soft_resolve(target_path / rel_to_root)
        except Exception:
            pass
    if new_abs is not None and not Path(new_abs).exists():
        new_abs = None
    if new_abs is None and allow_recover:
        recovered = recover_stale_light(abs_src, search_roots)
        if recovered is not None:
            rk = norm_key(recovered)
            if rk in copy_map_norm:
                new_abs = Path(copy_map_norm[rk])
            else:
                new_abs = recovered
    return new_abs


def _enable_autopack() -> None:
    """Turn on whatever autopack preference flag this Blender build exposes."""
    try:
        fp = bpy.context.preferences.filepaths
        for k in ("use_autopack", "use_autopack_files", "use_auto_pack"):
            if hasattr(fp, k):
                try:
                    setattr(fp, k, True)
                except Exception:
                    pass
    except Exception:
        pass


def run_remap(config_path: Path) -> None:
    """Load config JSON and remap all relevant filepaths in the open blend."""
    with open(config_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, dict) and "copy_map" in payload:
        copy_map = payload.get("copy_map") or {}
        search_roots = [Path(p) for p in (payload.get("search_roots") or [])]
        common_root = Path(payload["common_root"])
        target_path = Path(payload["target_path"])
        ensure_autopack = bool(payload.get("ensure_autopack", True))
    else:
        # Legacy: payload was bare copy_map only (should not happen with current pack_ops).
        copy_map = payload if isinstance(payload, dict) else {}
        search_roots = []
        common_root = Path(".")
        target_path = Path(".")
        ensure_autopack = True

    blend_dir = Path(bpy.data.filepath).parent
    bpy.context.preferences.filepaths.use_relative_paths = True
    copy_map_norm = {norm_key(k): v for k, v in copy_map.items()}

    remapped = 0
    unresolved: list[str] = []
    print("Remapping library paths in:", bpy.path.basename(bpy.data.filepath))
    print("Found", len(bpy.data.libraries), "libraries")

    for lib in bpy.data.libraries:
        if getattr(lib, "packed_file", None):
            print("  Skipping packed library:", lib.name)
            continue
        src = lib.filepath
        print("  Processing library:", lib.name, ", current path:", src)
        if src.startswith("//"):
            abs_src = safe_resolve(blend_dir / src[2:], "Library", lib.name)
        else:
            abs_src = safe_resolve(src, "Library", lib.name)
        new_abs = resolve_new_abs(abs_src, copy_map_norm, common_root, target_path, search_roots, allow_recover=True)
        if new_abs is not None:
            if Path(new_abs).exists():
                lib.filepath = str(new_abs)
                try:
                    rel_path = bpy.path.relpath(str(new_abs))
                    lib.filepath = rel_path
                    print("    Remapped to relative:", rel_path)
                    remapped += 1
                except Exception as e:
                    print("    WARNING: Could not make relative:", e, ", keeping absolute")
                    remapped += 1
            else:
                print("    WARNING: Target file does not exist:", new_abs)
                unresolved.append(str(new_abs))
        else:
            print("    WARNING: Could not determine new path for:", abs_src)
            unresolved.append(str(abs_src))

    print("Remapped", remapped, "libraries,", len(unresolved), "unresolved")
    if unresolved:
        print("Unresolved paths:", unresolved)

    # Remap image/texture paths (copy_map only — no per-image filesystem recovery).
    images_remapped = 0
    for img in bpy.data.images:
        if getattr(img, "packed_file", None):
            continue  # packed data is self-contained; ignore dead filepath
        if img.filepath and img.filepath not in ("", "<builtin>", "<memory>"):
            src = img.filepath
            if src.startswith("//"):
                abs_src = safe_resolve(blend_dir / src[2:], "Image", img.name)
            else:
                abs_src = safe_resolve(src, "Image", img.name)
            new_abs = resolve_new_abs(abs_src, copy_map_norm, common_root, target_path, search_roots)
            if new_abs is not None and Path(new_abs).exists():
                img.filepath = str(new_abs)
                try:
                    rel_path = bpy.path.relpath(str(new_abs))
                    img.filepath = rel_path
                    images_remapped += 1
                except Exception:
                    images_remapped += 1
    print("Remapped", images_remapped, "image/texture paths")

    # Remap external text scripts (e.g. handles.py) after stale-path recovery.
    texts_remapped = 0
    for txt in getattr(bpy.data, "texts", []):
        if getattr(txt, "packed_file", None):
            continue
        src = getattr(txt, "filepath", None) or ""
        if not src or src in ("", "<builtin>", "<memory>"):
            continue
        if src.startswith("//"):
            abs_src = soft_resolve(blend_dir / src[2:])
        else:
            abs_src = soft_resolve(src)
        new_abs = resolve_new_abs(abs_src, copy_map_norm, common_root, target_path, search_roots, allow_recover=True)
        if new_abs is not None and Path(new_abs).exists():
            txt.filepath = str(new_abs)
            try:
                txt.filepath = bpy.path.relpath(str(new_abs))
            except Exception:
                pass
            texts_remapped += 1
    print("Remapped", texts_remapped, "text script paths")

    # Remap physics/point cache paths (particle systems, cloth, soft body, etc.).
    caches_remapped = 0

    def remap_abs_to_rel(abs_src: Path):
        new_abs = resolve_new_abs(abs_src, copy_map_norm, common_root, target_path, search_roots)
        if new_abs is None:
            for src_prefix in sorted(copy_map_norm.keys(), key=lambda x: -len(x)):
                try:
                    rel = abs_src.relative_to(Path(src_prefix))
                    candidate = soft_resolve(Path(copy_map_norm[src_prefix]) / rel)
                    if candidate.exists():
                        new_abs = candidate
                        break
                except (ValueError, KeyError):
                    pass
        if new_abs is not None and Path(new_abs).exists():
            try:
                return bpy.path.relpath(str(new_abs))
            except Exception:
                return str(new_abs)
        return None

    def do_remap_path(src, kind="Cache", name=""):
        if not src or src in ("", "<builtin>", "<memory>"):
            return None
        if src.startswith("//"):
            abs_src = safe_resolve(blend_dir / src[2:], kind, name)
        else:
            abs_src = safe_resolve(src, kind, name)
        return remap_abs_to_rel(abs_src)

    for obj in bpy.data.objects:
        for mod in getattr(obj, "modifiers", []):
            ps = getattr(mod, "particle_system", None)
            if ps and getattr(ps, "point_cache", None):
                pc = ps.point_cache
                if getattr(pc, "filepath", None):
                    new_path = do_remap_path(pc.filepath, "PointCache", obj.name)
                    if new_path is not None:
                        pc.filepath = new_path
                        caches_remapped += 1
            pc = getattr(mod, "point_cache", None)
            if pc and getattr(pc, "filepath", None):
                new_path = do_remap_path(pc.filepath, "PointCache", obj.name)
                if new_path is not None:
                    pc.filepath = new_path
                    caches_remapped += 1
    print("Remapped", caches_remapped, "physics/point cache paths")

    # Remap cache file paths (USD, etc.).
    cache_files_remapped = 0
    for cf in getattr(bpy.data, "cache_files", []):
        if getattr(cf, "filepath", None):
            new_path = do_remap_path(cf.filepath, "CacheFile", getattr(cf, "name", ""))
            if new_path is not None:
                cf.filepath = new_path
                cache_files_remapped += 1
    print("Remapped", cache_files_remapped, "cache file (USD) paths")

    bpy.ops.wm.save_as_mainfile(filepath=str(Path(bpy.data.filepath)), compress=True)
    try:
        bpy.ops.file.make_paths_relative(basedir=str(blend_dir))
        print("Made all paths relative")
    except Exception as e:
        print("Warning: make_paths_relative failed:", e)

    if ensure_autopack:
        _enable_autopack()

    bpy.ops.wm.save_as_mainfile(filepath=str(Path(bpy.data.filepath)), compress=True)
    print("Remapping complete")


def _config_path_from_argv() -> Path:
    """Read config path from args after Blender's `--` separator."""
    if "--" in sys.argv:
        args = sys.argv[sys.argv.index("--") + 1 :]
        if args:
            return Path(args[0])
    raise SystemExit("remap_blend.py: missing config JSON after --")


# Blender executes this file with --python; run immediately.
run_remap(_config_path_from_argv())
