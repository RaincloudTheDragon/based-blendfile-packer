#!/usr/bin/env python
"""
To test:

Assume the opened blend file sits in the project root:

$ blender -b batter-tests/root/scene.blend -P pack.py -- target/directory

Explicitly provide a root path:

$ blender -b batter-tests/root/scene.blend -P pack.py -- -r /some/other/root target/directory

"""

import argparse
import dataclasses
import enum
import json
import os
import os.path
import sys
from pathlib import Path
from datetime import datetime
import builtins as _builtins
import re

import bpy
from bpy.types import Library

# Ensure Batter can be imported, even when it's not installed as package.
_my_dir = Path(__file__).resolve().parent
if str(_my_dir) not in sys.path:
    sys.path.append(str(_my_dir))

# Import not at top of file, but has to be below the modification sys.path.
from batter import asset_usage as au  # noqa: E402


# --- Logging helpers ---------------------------------------------------------
LOG_FILE: Path | None = None


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_log_file(line: str) -> None:
    global LOG_FILE
    try:
        if LOG_FILE is None:
            return
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8", errors="ignore") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _ts_print(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
    prefix = f"[{_timestamp()}] "
    msg = " ".join(str(a) for a in args)
    line = prefix + msg
    _builtins.print(line, **{k: v for k, v in kwargs.items() if k != "file"})
    _append_log_file(line)


def _log_lines(text: str) -> None:
    if not text:
        return
    for ln in text.splitlines():
        _ts_print(ln)


# Replace print in this module so all messages are timestamped and logged
print = _ts_print  # type: ignore[assignment]


def compute_target_relpath(abs_path: Path, base_root: Path) -> Path:
    """Return a stable relative path under the target, even if outside root.

    If `abs_path` is under `base_root`, return the normal relative path.
    Otherwise, prefix with a stable label so cross-drive/UNC files still copy.
    Labels:
      - DRIVE_X for Windows drive letters
      - UNC_server_share for UNC roots
      - ROOT otherwise
    """
    try:
        return abs_path.relative_to(base_root)
    except Exception:
        anchor = abs_path.anchor
        if os.name == "nt":
            if anchor.startswith("\\\\"):
                parts = anchor.strip("\\").split("\\")
                label = "UNC_" + "_".join(parts[:2]) if len(parts) >= 2 else "UNC"
            elif len(anchor) >= 2 and anchor[1] == ":":
                label = f"DRIVE_{anchor[0].upper()}"
            else:
                label = "ROOT"
        else:
            label = "ROOT"
        # Strip the anchor portion and any leading separators
        rel_after_anchor = str(abs_path)[len(anchor):].lstrip("\\/")
        return Path(label) / Path(rel_after_anchor)


def copy_blend_caches(src_blend: Path, dst_blend: Path, missing_on_copy: list[Path], caches_copied: list[Path] | None = None) -> int:
    """Copy common cache folders for a given .blend next to its target copy.

    - Copies sibling directory "blendcache_<blendname>" → same under dst parent
    - Copies sibling directory "bakes/<blendname>" → same under dst parent
    - Copies any sibling directory starting with "cache_" (including "cache_fluid_*")
    Returns number of cache directories copied.
    """
    copied = 0
    try:
        src_parent = src_blend.parent
        dst_parent = dst_blend.parent
        blendname = src_blend.stem

        candidates: list[tuple[Path, Path]] = []
        # Physics-style cache
        candidates.append((src_parent / f"blendcache_{blendname}", dst_parent / f"blendcache_{blendname}"))
        # Bake caches often stored under bakes/<blendname>
        candidates.append((src_parent / "bakes" / blendname, dst_parent / "bakes" / blendname))
        # Any cache_* folders (e.g., cache_fluid_*, cache_*) besides the two above
        try:
            for entry in src_parent.iterdir():
                try:
                    name = entry.name
                except Exception:
                    name = ""
                if not entry.is_dir():
                    continue
                if name.startswith("cache_"):
                    candidates.append((entry, dst_parent / name))
        except Exception:
            pass

        for src_dir, dst_dir in candidates:
            try:
                if src_dir.exists() and src_dir.is_dir():
                    dst_dir.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    try:
                        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                        print(f"✓ Copied cache dir: {src_dir} → {dst_dir}")
                        if caches_copied is not None:
                            caches_copied.append(src_dir)
                        copied += 1
                        continue
                    except PermissionError as e:
                        # Fallback to robocopy on Windows SMB perms
                        if os.name == "nt":
                            import subprocess as _sub
                            rc = _sub.run([
                                "robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1"
                            ], capture_output=True, text=True)
                            # robocopy returns <8 for success (including minor diffs)
                            if rc.returncode < 8:
                                print(f"✓ Robocopy cache dir: {src_dir} → {dst_dir}")
                                if caches_copied is not None:
                                    caches_copied.append(src_dir)
                                copied += 1
                                continue
                            else:
                                print(f"✗ Robocopy failed for {src_dir}: code {rc.returncode}")
                                _log_lines(rc.stdout)
                                _log_lines(rc.stderr)
                        raise e
            except Exception as e:
                print(f"✗ Failed to copy cache dir {src_dir}: {e}")
                try:
                    missing_on_copy.append(src_dir)
                except Exception:
                    pass
    except Exception:
        pass
    return copied


def expand_sequence_files(path: Path) -> tuple[list[Path], str | None]:
    """If `path` looks like part of an image sequence, return all frames.

    Returns (files, base_pattern). base_pattern is a human-friendly string
    to display in the summary (e.g., "paintmap####.png"). If not a sequence,
    returns ([path], None).
    """
    try:
        suffix = path.suffix.lower()
        name_lower = path.name.lower()
        if suffix == ".gz" and name_lower.endswith(".vdb.gz"):
            suffix = ".vdb.gz"
        if suffix not in {".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".bmp", ".vdb", ".vdb.gz"}:
            return [path], None
        m = re.match(r"^(.*?)(\d+)$", path.stem)
        if not m:
            return [path], None
        base, digits = m.group(1), m.group(2)
        width = len(digits)
        # Collect siblings that match base + digits of same width (common pattern)
        files: list[Path] = []
        pattern = re.compile(rf"^{re.escape(base)}\d+{re.escape(suffix)}$", re.IGNORECASE)
        try:
            for p in sorted(path.parent.iterdir()):
                if p.is_file() and p.suffix.lower() == suffix and pattern.match(p.name):
                    files.append(p)
        except Exception:
            return [path], None
        if not files:
            return [path], None
        base_pattern = f"{base}{'#'*width}{suffix}"
        return files, base_pattern
    except Exception:
        return [path], None


def discover_used_cache_dirs(blend_path: Path) -> list[Path]:
    """Open the given blend and return cache directories that are actually referenced.

    We look for:
      - Any modifier/physics `point_cache.filepath` (fallback to blendcache_<name>)
      - Fluid domain cache directories (best-effort across Blender versions)
      - Dynamic Paint surfaces output paths
      - RigidBody world/Particle systems point caches
    """
    import subprocess, json as _json

    script = (
        "import bpy, json\n"
        "from pathlib import Path\n"
        "blend_path = Path(bpy.data.filepath)\n"
        "blend_dir = blend_path.parent\n"
        "blend_name = blend_path.stem\n"
        "dirs = set()\n"
        "def norm(p):\n"
        "    try:\n"
        "        if not p:\n"
        "            return None\n"
        "        ap = bpy.path.abspath(str(p))\n"
        "        if not ap:\n"
        "            return None\n"
        "        rp = Path(ap).resolve()\n"
        "        # If path points to a file (e.g., 'meta'), use its parent\n"
        "        try:\n"
        "            if rp.exists() and rp.is_file():\n"
        "                rp = rp.parent\n"
        "        except Exception:\n"
        "            pass\n"
        "        # If terminal folder is a cache leaf like 'meta' or 'data', go up one\n"
        "        try:\n"
        "            if rp.name.lower() in {'meta','data'}:\n"
        "                rp = rp.parent\n"
        "        except Exception:\n"
        "            pass\n"
        "        return str(rp)\n"
        "    except Exception:\n"
        "        return None\n"
        "# Point caches on modifiers and physics\n"
        "for obj in bpy.data.objects:\n"
        "    for mod in getattr(obj, 'modifiers', []):\n"
        "        pc = getattr(mod, 'point_cache', None)\n"
        "        if pc is not None:\n"
        "            p = getattr(pc, 'filepath', '') or str(blend_dir / f'blendcache_{blend_name}')\n"
        "            np = norm(p)\n"
        "            if np: dirs.add(np)\n"
        "        # Fluid domain cache paths (best-effort)\n"
        "        try:\n"
        "            ds = getattr(mod, 'domain_settings', None)\n"
        "        except Exception:\n"
        "            ds = None\n"
        "        if ds is not None:\n"
        "            for attr in ('cache_directory', 'cache_directory_render', 'cache_directory_final'):\n"
        "                if hasattr(ds, attr):\n"
        "                    np = norm(getattr(ds, attr))\n"
        "                    if np: dirs.add(np)\n"
        "        # Dynamic paint output\n"
        "        if getattr(mod, 'type', '') == 'DYNAMIC_PAINT':\n"
        "            cs = getattr(mod, 'canvas_settings', None)\n"
        "            if cs:\n"
        "                for surf in getattr(cs, 'canvas_surfaces', []) or []:\n"
        "                    for attr in ('image_output_path', 'output_path', 'filepath', 'path'):\n"
        "                        if hasattr(surf, attr):\n"
        "                            np = norm(getattr(surf, attr))\n"
        "                            if np: dirs.add(np)\n"
        "    # Rigid body world / particle systems\n"
        "    for ps in getattr(obj, 'particle_systems', []) or []:\n"
        "        try:\n"
        "            pc = ps.point_cache\n"
        "            p = getattr(pc, 'filepath', '') or str(blend_dir / f'blendcache_{blend_name}')\n"
        "            np = norm(p)\n"
        "            if np: dirs.add(np)\n"
        "        except Exception:\n"
        "            pass\n"
        "rbw = getattr(getattr(obj, 'rigid_body_constraint', None), 'point_cache', None)\n"
        "    # Scene-level rigidbody world\n"
        "for sc in bpy.data.scenes:\n"
        "    rbw = getattr(sc, 'rigidbody_world', None)\n"
        "    if rbw and hasattr(rbw, 'point_cache') and rbw.point_cache:\n"
        "        p = getattr(rbw.point_cache, 'filepath', '') or str(blend_dir / f'blendcache_{blend_name}')\n"
        "        np = norm(p)\n"
        "        if np: dirs.add(np)\n"
        "# Only keep existing directories\n"
        "# Always include bakes/<blendname> if present next to the blend\n"
        "try:\n"
        "    bakes_dir = (blend_dir / 'bakes' / blend_name)\n"
        "    if bakes_dir.exists():\n"
        "        dirs.add(str(bakes_dir.resolve()))\n"
        "except Exception:\n"
        "    pass\n"
        "dirs = [d for d in dirs if d and Path(d).exists() and Path(d).is_dir()]\n"
        "print(json.dumps(dirs))\n"
    )

    res = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)

    # stdout should be the JSON list in the last line; parse robustly
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    last_json = None
    for ln in reversed(text.splitlines()):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            last_json = s
            break
    try:
        if last_json:
            data = _json.loads(last_json)
            return [Path(p) for p in data if isinstance(p, str)]
    except Exception:
        pass
    return []


def collect_nonblend_assets_in_blend(blend_path: Path) -> list[Path]:
    """Return absolute paths of non-blend assets used by the given .blend.

    Runs Blender in a subprocess and queries file_path_map(include_libraries=False)
    and also checks bpy.data.images directly to catch PNG/EXR files that may be missed.
    """
    import subprocess, json as _json
    script = (
        "import bpy, json\n"
        "from pathlib import Path\n"
        "fps = set()\n"
        "m = bpy.data.file_path_map(include_libraries=False)\n"
        "for user, files in m.items():\n"
        "    if not files: continue\n"
        "    # Skip library users entirely; Blender returns libraries even with include_libraries=False sometimes\n"
        "    if hasattr(user, 'library') and user.library is not None:\n"
        "        continue\n"
        "    for f in files:\n"
        "        try:\n"
        "            ap = bpy.path.abspath(f)\n"
        "        except Exception:\n"
        "            ap = f\n"
        "        fps.add(str(Path(ap).resolve()))\n"
        "# Also check bpy.data.images directly, as file_path_map may miss some image references\n"
        "# (e.g., PNG, EXR files in certain node setups)\n"
        "for img in bpy.data.images:\n"
        "    if img.packed_file: continue\n"
        "    if not img.filepath: continue\n"
        "    # Skip generated images and sequences/movies\n"
        "    if getattr(img, 'source', 'FILE') not in {'FILE', 'TILED'}: continue\n"
        "    try:\n"
        "        ap = bpy.path.abspath(img.filepath)\n"
        "        fps.add(str(Path(ap).resolve()))\n"
        "    except Exception:\n"
        "        pass\n"
        "# Also check sounds and movie clips for completeness\n"
        "for snd in getattr(bpy.data, 'sounds', []):\n"
        "    if snd.packed_file: continue\n"
        "    if not snd.filepath: continue\n"
        "    try:\n"
        "        ap = bpy.path.abspath(snd.filepath)\n"
        "        fps.add(str(Path(ap).resolve()))\n"
        "    except Exception:\n"
        "        pass\n"
        "for mc in getattr(bpy.data, 'movieclips', []):\n"
        "    if not mc.filepath: continue\n"
        "    try:\n"
        "        ap = bpy.path.abspath(mc.filepath)\n"
        "        fps.add(str(Path(ap).resolve()))\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(sorted(fps)))\n"
    )
    res = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)
    txt = (res.stdout or "") + "\n" + (res.stderr or "")
    payload = None
    for ln in reversed(txt.splitlines()):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            payload = s
            break
    try:
        if payload:
            arr = _json.loads(payload)
            return [Path(p) for p in arr if isinstance(p, str)]
    except Exception:
        pass
    return []


def remap_nonblend_paths_in_blend(
    blend_path: Path, copy_map: dict[str, str], autopack_on_save: bool = True
) -> None:
    """In-place remap of non-blend asset paths inside a .blend using provided mapping.

    copy_map keys and values are absolute paths.
    """
    import subprocess, json as _json
    mapping_json = _json.dumps(copy_map)
    autopack_block = ""
    if autopack_on_save:
        autopack_block = (
            "try:\n"
            "    fp = bpy.context.preferences.filepaths\n"
            "    ensured = False\n"
            "    for k in ('use_autopack','use_autopack_files','use_auto_pack'):\n"
            "        if hasattr(fp,k):\n"
            "            try:\n"
            "                if not bool(getattr(fp,k)):\n"
            "                    setattr(fp,k,True)\n"
            "                ensured = bool(getattr(fp,k))\n"
            "                break\n"
            "            except Exception:\n"
            "                pass\n"
            "    if not ensured:\n"
            "        try:\n"
            "            bpy.ops.file.autopack_toggle()\n"
            "        except Exception:\n"
            "            pass\n"
            "except Exception:\n"
            "    pass\n"
        )
    script = (
        "import bpy, json\n"
        "from pathlib import Path\n"
        f"mapping = json.loads(r'''{mapping_json}''')\n"
        "def remap_path(p):\n"
        "    if not p: return None\n"
        "    ap = str(Path(bpy.path.abspath(p)).resolve())\n"
        "    na = mapping.get(ap)\n"
        "    return na\n"
        "count = 0\n"
        "# Images\n"
        "for img in bpy.data.images:\n"
        "    if img.packed_file: continue\n"
        "    if getattr(img, 'source', 'FILE') in {'SEQUENCE','MOVIE'}:\n"
        "        continue\n"
        "    np = remap_path(img.filepath)\n"
        "    if np:\n"
        "        try:\n"
        "            img.filepath = bpy.path.relpath(np)\n"
        "            count += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "# Sounds\n"
        "for snd in bpy.data.sounds:\n"
        "    if snd.packed_file: continue\n"
        "    np = remap_path(snd.filepath)\n"
        "    if np:\n"
        "        try:\n"
        "            snd.filepath = bpy.path.relpath(np)\n"
        "            count += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "# Movie clips\n"
        "for mc in getattr(bpy.data, 'movieclips', []):\n"
        "    np = remap_path(mc.filepath)\n"
        "    if np:\n"
        "        try:\n"
        "            mc.filepath = bpy.path.relpath(np)\n"
        "            count += 1\n"
        "        except Exception:\n"
        "            pass\n"
        f"{autopack_block}"
        "bpy.ops.wm.save_mainfile()\n"
        "print('Remapped nonblend paths:', count)\n"
    )
    subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)


class WorkflowMode(enum.Enum):
    COPY_ONLY = "copy-only"
    PACK_AND_SAVE = "pack-and-save"


@dataclasses.dataclass
class RenderSettingsSnapshot:
    engine: str | None = None
    cycles_device: str | None = None
    compute_device_type: str | None = None


@dataclasses.dataclass
class CLIArgs:
    root_path: Path
    target_path: Path
    enable_nla: bool = False
    pack_linked: bool = True
    workflow: WorkflowMode = WorkflowMode.PACK_AND_SAVE
    mirror_render_settings: bool = False


# Will be updated by actually parsing CLI arguments.
cli_args = CLIArgs(
    root_path=Path(".").resolve(),
    target_path=Path(".").resolve(),
    enable_nla=False,
    pack_linked=True,
    workflow=WorkflowMode.PACK_AND_SAVE,
    mirror_render_settings=False,
)


class JSONEncoder(json.JSONEncoder):
    def default(self, o: object) -> object:
        # `dataclasses.is_dataclass(o)` also returns True if `o` is a dataclass class.
        # This code only supports class instances, though.
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if isinstance(o, set):
            return tuple(o)
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, Library):
            return f"<library {o.filepath}>"
        return super().default(o)


def main():
    global cli_args
    cli_args = parse_cli_args()
    copy_only_mode = cli_args.workflow == WorkflowMode.COPY_ONLY
    autopack_on_save = not copy_only_mode
    run_pack_linked = cli_args.pack_linked and not copy_only_mode
    render_profile: RenderSettingsSnapshot | None = None

    # Initialize log file in target directory
    global LOG_FILE
    log_name = f"batter_pack_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    LOG_FILE = cli_args.target_path / log_name
    print(f"Logging to: {LOG_FILE}")

    asset_usages = au.find()
    top_level_blend_abs = au.library_abspath(None).resolve()
    if cli_args.mirror_render_settings:
        try:
            render_profile = capture_render_settings(top_level_blend_abs)
            if render_profile is None:
                print("Warning: Unable to capture render settings from current blend.")
        except Exception as exc:
            print(f"✗ Failed to capture render settings: {exc}")
            render_profile = None

    # Collect all absolute file paths, and determine the common root.
    all_filepaths: list[Path] = []
    all_filepaths.extend(au.library_abspath(lib) for lib in asset_usages.keys())
    all_filepaths.extend(
        asset_usage.abspath
        for asset_usages in asset_usages.values()
        for asset_usage in asset_usages
    )

    # Determine common root; handle cross-drive scenarios on Windows.
    try:
        common_root_str: str = os.path.commonpath(all_filepaths)
    except ValueError:
        blend_file_drive = Path(bpy.data.filepath).drive
        project_filepaths = [p for p in all_filepaths if getattr(p, "drive", "") == blend_file_drive]
        if project_filepaths:
            common_root_str = os.path.commonpath(project_filepaths)
            print(f"Warning: Cross-drive assets detected. Using project drive ({blend_file_drive}) as root.")
            skipped = [str(p) for p in all_filepaths if getattr(p, "drive", "") != blend_file_drive]
            if skipped:
                print(f"Skipping assets on other drives: {skipped}")
        else:
            # Fallback to the current blend's directory
            common_root_str = str(Path(bpy.data.filepath).parent)
            print("Warning: No assets found on project drive. Using blend file directory as root.")
    if not common_root_str:
        raise ValueError(
            "could not find a common root directory for these assets, this is not supported right now."
        )
    common_root = Path(common_root_str)
    del common_root_str

    # Tighten root to the current project's folder (avoid pulling sibling projects)
    try:
        project_candidate: Path | None = None
        parts = top_level_blend_abs.parts
        if parts:
            first_segment = Path(parts[0])
            if str(first_segment).startswith("\\\\"):
                if len(parts) >= 2:
                    project_candidate = first_segment / parts[1]
            else:
                if len(parts) >= 2:
                    project_candidate = first_segment / parts[1]
        if project_candidate:
            try:
                top_level_blend_abs.relative_to(project_candidate)
            except ValueError:
                project_candidate = None
        if project_candidate and project_candidate != common_root:
            try:
                common_root.relative_to(project_candidate)
            except ValueError:
                print(f"Adjusted root to project folder: {project_candidate} (was {common_root})")
                common_root = project_candidate
    except Exception:
        pass

    print()
    header = f"Packing, relative to \033[38;5;214m{common_root}\033[0m → {cli_args.target_path}"
    separator = (len(header) - 15) * "-"  #  remove ANSI control codes
    print(separator)
    print(header)
    print(separator)

    # Construct a plan of what to copy, and where.
    copied_paths: set[Path] = set()
    copy_map: dict[Path, Path] = {}
    # Tracking for final summary
    missing_on_copy: list[Path] = []
    unresolved_libs_after_remap: list[Path] = []
    pack_missing_sources: list[Path] = []
    outside_root_assets: list[Path] = []
    caches_copied: list[Path] = []
    sequences_copied: dict[str, int] = {}

    # First, copy the current (top-level) blend file itself.
    top_level_target_blend: Path | None = None
    current_relpath: Path | None = None
    current_blend_abspath = top_level_blend_abs
    try:
        current_relpath = current_blend_abspath.relative_to(common_root)
    except ValueError:
        current_relpath = compute_target_relpath(current_blend_abspath, common_root)
        print(f"Note: current blend is outside root; using {current_relpath}")
    if current_blend_abspath not in copied_paths:
        target_path = cli_args.target_path / current_relpath
        print(
            f"Plan: \033[96mCOPY\033[0m {colourise(current_relpath, width=55)} → {target_path}"
        )
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(current_blend_abspath, target_path)
            copied_paths.add(current_blend_abspath)
            if current_blend_abspath.suffix.lower() == ".blend":
                copy_map[current_blend_abspath.resolve()] = target_path.resolve()
            print(f"✓ Copied: {current_relpath}")
        except Exception as e:
            print(f"✗ Failed to copy {current_relpath}: {e}")
            try:
                missing_on_copy.append(current_blend_abspath)
            except Exception:
                pass
        else:
            top_level_target_blend = target_path.resolve()
            # Copy only caches that are actually used by the top-level blend
            try:
                used_dirs = discover_used_cache_dirs(current_blend_abspath)
                if used_dirs:
                    print(f"Detected {len(used_dirs)} used cache dir(s) in top-level blend")
                for src_dir in used_dirs:
                    try:
                        src_dir.relative_to(common_root)
                    except ValueError:
                        print(f"Note: cache dir outside project root; still copying {src_dir}")
                        outside_root_assets.append(src_dir)
                    # Preserve the relative path under the source blend directory
                    try:
                        rel_cache = src_dir.relative_to(current_blend_abspath.parent)
                    except Exception:
                        rel_cache = Path(src_dir.name)
                    dst_dir = top_level_target_blend.parent / rel_cache
                    try:
                        import shutil
                        dst_dir.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                            print(f"✓ Copied cache dir: {src_dir} → {dst_dir}")
                            caches_copied.append(src_dir)
                        except PermissionError as e:
                            if os.name == "nt":
                                import subprocess as _sub
                                rc = _sub.run(["robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1"], capture_output=True, text=True)
                                if rc.returncode < 8:
                                    print(f"✓ Robocopy cache dir: {src_dir} → {dst_dir}")
                                    caches_copied.append(src_dir)
                                else:
                                    print(f"✗ Robocopy failed for {src_dir}: code {rc.returncode}")
                                    _log_lines(rc.stdout)
                                    _log_lines(rc.stderr)
                            else:
                                raise e
                    except Exception as e:
                        print(f"✗ Failed to copy cache dir {src_dir}: {e}")
                        missing_on_copy.append(src_dir)
            except Exception:
                pass

    for lib, links_to in asset_usages.items():
        for asset_usage in links_to:
            try:
                asset_relpath = asset_usage.abspath.relative_to(common_root)
            except ValueError:
                asset_relpath = compute_target_relpath(asset_usage.abspath, common_root)

            # TODO: if this blend file was linked with an absolute path, 'lib'
            # should be remapped to use relative paths.

            if asset_usage.abspath in copied_paths:
                continue

            # Decide which files to copy: expand sequences for non-blend images
            files_to_copy: list[Path] = [asset_usage.abspath]
            if not asset_usage.is_blendfile:
                expanded, base_pat = expand_sequence_files(asset_usage.abspath)
                if expanded and len(expanded) > 1 and base_pat:
                    files_to_copy = expanded
                    sequences_copied[base_pat] = sequences_copied.get(base_pat, 0) + len(expanded)

            for file_path in files_to_copy:
                if not file_path.exists():
                    print(f"✗ Missing source (skip copy): {file_path}")
                    try:
                        missing_on_copy.append(file_path)
                    except Exception:
                        pass
                    continue

                try:
                    rel = file_path.relative_to(common_root)
                except ValueError:
                    rel = compute_target_relpath(file_path, common_root)
                    print(f"Note: asset outside project root; storing under {rel}")
                    outside_root_assets.append(file_path)

                target_path = cli_args.target_path / rel
                print(
                    f"Plan: \033[96mCOPY\033[0m {colourise(rel, width=55)} → {target_path}"
                )

                # Actually copy the file
                try:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(file_path, target_path)
                    copied_paths.add(file_path)
                    if asset_usage.is_blendfile and file_path.suffix.lower() == ".blend":
                        copy_map[file_path.resolve()] = target_path.resolve()
                    print(f"✓ Copied: {rel}")
                except Exception as e:
                    print(f"✗ Failed to copy {rel}: {e}")
                    try:
                        missing_on_copy.append(file_path)
                    except Exception:
                        pass
                else:
                    # For library blends: collect and copy their non-blend assets, then remap them
                    if asset_usage.is_blendfile and file_path.suffix.lower() == ".blend":
                        try:
                            lib_nonblends = collect_nonblend_assets_in_blend(file_path)
                        except Exception:
                            lib_nonblends = []
                        if lib_nonblends:
                            lib_map: dict[str, str] = {}
                            for nb in lib_nonblends:
                                try:
                                    if not nb.exists():
                                        print(f"✗ Missing source (skip copy): {nb}")
                                        missing_on_copy.append(nb)
                                        continue
                                    try:
                                        nb_rel = nb.relative_to(common_root)
                                    except ValueError:
                                        nb_rel = compute_target_relpath(nb, common_root)
                                    nb_dst = cli_args.target_path / nb_rel
                                    nb_dst.parent.mkdir(parents=True, exist_ok=True)
                                    import shutil as _sh
                                    _sh.copy2(nb, nb_dst)
                                    print(f"✓ Copied: {nb_rel}")
                                    lib_map[str(nb.resolve())] = str(nb_dst.resolve())
                                except Exception as ce:
                                    print(f"✗ Failed to copy {nb}: {ce}")
                                    missing_on_copy.append(nb)
                            # Remap non-blend paths inside the library to the copied targets
                            try:
                                remap_nonblend_paths_in_blend(
                                    target_path.resolve(),
                                    lib_map,
                                    autopack_on_save=autopack_on_save,
                                )
                            except Exception:
                                pass

    print(separator)

    # Remap library paths in all copied blend files to be relative to the copied tree.
    print()
    print(separator)
    print("Remapping library paths to relative...")
    print(separator)

    # Build dependency graph of blend -> blend dependencies
    blend_deps = au.find_blend_asset_usage()

    def lib_abs(lib: Library | None) -> Path:
        return au.library_abspath(lib)

    adjacency: dict[Path, set[Path]] = {}
    nodes: set[Path] = set()

    for lib, uses in blend_deps.items():
        src = lib_abs(lib)
        nodes.add(src)
        adjacency.setdefault(src, set())
        for use in uses:
            if not use.is_blendfile:
                continue
            nodes.add(use.abspath)
            adjacency[src].add(use.abspath)

    # Topologically order nodes: dependencies first, root last
    visited: set[Path] = set()
    order: list[Path] = []

    def visit(node: Path) -> None:
        if node in visited:
            return
        visited.add(node)
        for dep in adjacency.get(node, set()):
            visit(dep)
        order.append(node)

    for node in nodes:
        visit(node)

    # Map to target blend paths and filter to those we copied
    to_remap: list[Path] = []
    for abs_path in order:
        if abs_path.suffix.lower() != ".blend":
            continue
        try:
            rel = abs_path.relative_to(common_root)
        except ValueError:
            rel = compute_target_relpath(abs_path, common_root)
        # Only remap if we copied this file (or it's the current .blend)
        if abs_path not in copied_paths and abs_path != current_blend_abspath:
            continue
        to_remap.append(cli_args.target_path / rel)

    # Execute remapping in dependency order (leaves first, root last)
    for blend_to_fix in to_remap:
        unresolved = remap_library_paths(
            blend_path=blend_to_fix,
            copy_map={str(k): str(v) for k, v in copy_map.items()},
            common_root=common_root,
            target_path=cli_args.target_path,
            ensure_autopack_on_save=autopack_on_save,
        )
        unresolved_libs_after_remap.extend(unresolved)

    print(separator)

    if copy_only_mode and cli_args.mirror_render_settings and render_profile:
        print()
        print(separator)
        print("Mirroring render settings to copied blends...")
        print(separator)
        for blend_to_fix in to_remap:
            apply_render_settings_to_blend(blend_to_fix, render_profile)
        print(separator)

    # Optional: Enable NLA before any packing so evaluation matches viewport behavior
    if cli_args.enable_nla:
        print()
        print(separator)
        print("Enabling NLA tracks and strips (safe) in all copied blends before packing...")
        print(separator)
        for blend_to_fix in to_remap:
            enable_nla_in_blend(blend_to_fix, autopack_on_save=autopack_on_save)

    # Pass 1: Autopack external files (images, etc.) bottom-up so leaves are self-contained.
    if not copy_only_mode:
        print()
        print(separator)
        print("Packing resources (pack all) bottom-up...")
        print(separator)
        for blend_to_fix in to_remap:
            missing_from_pack = pack_all_in_blend(blend_to_fix)
            pack_missing_sources.extend(missing_from_pack)
    else:
        print()
        print(separator)
        print("Skipping pack-all phase (copy-only workflow).")
        print(separator)

    # Pass 2: Pack Linked libraries bottom-up so parents include children.
    if run_pack_linked:
        print()
        print(separator)
        print("Pack Linked libraries bottom-up...")
        print(separator)
        for blend_to_fix in to_remap:
            pack_linked_in_blend(blend_to_fix)
    elif cli_args.pack_linked and copy_only_mode:
        print()
        print(separator)
        print("Pack Linked disabled because copy-only workflow selected.")
        print(separator)

    print(separator)

    # Optional: Run a second NLA enable pass after Pack Linked
    if cli_args.enable_nla and run_pack_linked:
        print()
        print(separator)
        print("Re-enabling NLA (safe) in all copied blends after Pack Linked...")
        print(separator)
        for blend_to_fix in to_remap:
            enable_nla_in_blend(blend_to_fix, autopack_on_save=autopack_on_save)

    if cli_args.workflow == WorkflowMode.PACK_AND_SAVE:
        print()
        print(separator)
        print("Saving packed blends to Downloads...")
        print(separator)
        downloads_dir = resolve_downloads_dir()
        downloads_targets: list[Path] = []
        if top_level_target_blend:
            downloads_targets.append(top_level_target_blend)
        exported_paths = export_blends_to_downloads(downloads_targets, cli_args.target_path, downloads_dir)
        if exported_paths:
            print(f"Saved {len(exported_paths)} blend file(s) to {downloads_dir}")
        else:
            print("No blend files were exported to Downloads.")
        print(separator)

    # Cleanup backup files (.blend1 .. .blend32) created during saves.
    print()
    print(separator)
    print("Cleaning up .blend backup files (.blend1-.blend32)...")
    print(separator)
    removed = cleanup_blend_backups(cli_args.target_path)
    print(f"Removed {removed} backup file(s)")
    print(separator)

    # Final summary at absolute end
    print()
    print(separator)
    print("Missing/Unresolved report")
    print(separator)
    if missing_on_copy:
        print("Missing during copy:")
        for p in sorted(set(missing_on_copy)):
            print(f"  - {p}")
    if unresolved_libs_after_remap:
        print("Unresolved libraries after remap:")
        for p in sorted(set(unresolved_libs_after_remap)):
            print(f"  - {p}")
    if pack_missing_sources:
        print("Missing sources during pack-all:")
        for p in sorted(set(pack_missing_sources)):
            print(f"  - {p}")
    if outside_root_assets:
        print("Outside project root (copied with prefixed paths):")
        for p in sorted(set(outside_root_assets)):
            print(f"  - {p}")
    if caches_copied:
        print("Caches copied:")
        for p in sorted(set(caches_copied)):
            print(f"  - {p}")
    if sequences_copied:
        print("Sequences copied:")
        for base_pat, cnt in sorted(sequences_copied.items()):
            print(f"  - {base_pat} × {cnt}")
    if not (missing_on_copy or unresolved_libs_after_remap or pack_missing_sources):
        print("Nothing missing. All good.")
    print(separator)


def colourise(asset_path: Path, width=0) -> str:
    if width:
        as_string = f"{asset_path!s:.<{width}}"
    else:
        as_string = str(asset_path)

    if asset_path.suffix.lower() == ".blend":
        return f"\033[96m{as_string}\033[0m"
    return f"\033[95m{as_string}\033[0m"


def remap_library_paths(
    blend_path: Path,
    copy_map: dict[str, str],
    common_root: Path,
    target_path: Path,
    ensure_autopack_on_save: bool = True,
) -> list[Path]:
    """Open a blend file and remap all library paths to be relative to the copied tree."""
    import subprocess, json

    copy_map_json = json.dumps(copy_map)

    autopack_block = ""
    if ensure_autopack_on_save:
        autopack_block = (
            "try:\n"
            "    fp = bpy.context.preferences.filepaths\n"
            "    for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
            "        if hasattr(fp, k):\n"
            "            try:\n"
            "                setattr(fp, k, True)\n"
            "            except Exception:\n"
            "                pass\n"
            "    print('Autopack preference set ON (if available)')\n"
            "except Exception:\n"
            "    pass\n"
        )
    remap_script = (
        "import bpy, os, json\n"
        "from pathlib import Path\n"
        "blend_path = Path(bpy.data.filepath)\n"
        f"copy_map = json.loads(r'''{copy_map_json}''')\n"
        f"common_root = Path(r'{str(common_root)}')\n"
        f"target_path = Path(r'{str(target_path)}')\n"
        "bpy.context.preferences.filepaths.use_relative_paths = True\n"
        "remapped = 0\n"
        "for lib in bpy.data.libraries:\n"
        "    src = lib.filepath\n"
        "    # Resolve absolute current path\n"
        "    if src.startswith('//'):\n"
        "        abs_src = (blend_path.parent / src[2:]).resolve()\n"
        "    else:\n"
        "        abs_src = Path(src).resolve()\n"
        "    key = str(abs_src)\n"
        "    new_abs = None\n"
        "    try:\n"
        "        abs_src.relative_to(target_path)\n"
        "        new_abs = abs_src\n"
        "    except Exception:\n"
        "        new_abs = None\n"
        "    if key in copy_map:\n"
        "        new_abs = Path(copy_map[key])\n"
        "    elif new_abs is None:\n"
        "        # Fallback via common_root relative mapping\n"
        "        try:\n"
        "            rel_to_root = abs_src.relative_to(common_root)\n"
        "            new_abs = (target_path / rel_to_root).resolve()\n"
        "        except Exception:\n"
        "            pass\n"
        "    if new_abs is None:\n"
        "        # Last resort: suffix/filename match\n"
        "        src_posix = abs_src.as_posix().lower()\n"
        "        for _, v in copy_map.items():\n"
        "            if src_posix.endswith(Path(v).as_posix().lower()):\n"
        "                new_abs = Path(v)\n"
        "                break\n"
        "    if new_abs is None:\n"
        "        print('  Skip ' + lib.name + ' -> unresolved ' + str(abs_src))\n"
        "        continue\n"
        "    # Assign absolute for relocate then explicitly set Blender-style relative path\n"
        "    lib.filepath = str(new_abs)\n"
        "    try:\n"
        "        lib.filepath = bpy.path.relpath(str(new_abs))\n"
        "    except Exception:\n"
        "        pass\n"
        "    remapped += 1\n"
        "    print('  Remap ' + lib.name + ' FROM: ' + str(src) + ' TO: ' + lib.filepath)\n"
        "# First save absolute relocations/relative assignments\n"
        "bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))\n"
        "# Then convert residuals to relative and save again\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative(basedir=str(blend_path.parent))\n"
        "except Exception:\n"
        "    pass\n"
        f"{autopack_block}"
        "bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))\n"
        "print('Remapped ' + str(remapped) + ' libraries in ' + blend_path.name)\n"
    )

    result = subprocess.run(
        [
            "blender",
            "--factory-startup",
            "-b",
            str(blend_path),
            "--python-expr",
            remap_script,
        ],
        capture_output=True,
        text=True,
    )

    # Parse unresolved lines from stdout
    unresolved: list[Path] = []
    try:
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        for line in out.splitlines():
            ls = line.strip()
            if ls.startswith("Skip ") or ls.startswith("  Skip "):
                if "unresolved" in ls:
                    p = ls.split("unresolved", 1)[1].strip()
                    unresolved.append(Path(p))
    except Exception:
        pass

    if result.returncode != 0:
        print(f"✗ Failed to remap paths in {blend_path.name}")
        _log_lines(result.stderr)
        _log_lines(result.stdout)
    else:
        print(f"✓ Remapped paths in {blend_path.name}")
        _log_lines(result.stdout)

    return unresolved

def pack_all_in_blend(blend_path: Path) -> list[Path]:
    """Open a blend and pack all external files into it."""
    import subprocess

    script = (
        "import bpy\n"
        "import sys\n"
        "from pathlib import Path\n"
        "blend_dir = Path(bpy.data.filepath).parent\n"
        "# Before packing, ensure relative file paths are re-evaluated from current location\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative(basedir=str(blend_dir))\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    bpy.ops.file.pack_all()\n"
        "    # Ensure autopack ON at final save (set prefs if available; only toggle if not)\n"
        "    try:\n"
        "        fp = bpy.context.preferences.filepaths\n"
        "        ensured = False\n"
        "        for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
        "            if hasattr(fp, k):\n"
        "                try:\n"
        "                    if not bool(getattr(fp, k)):\n"
        "                        setattr(fp, k, True)\n"
        "                    ensured = bool(getattr(fp, k))\n"
        "                    break\n"
        "                except Exception:\n"
        "                    pass\n"
        "        if not ensured:\n"
        "            try:\n"
        "                bpy.ops.file.autopack_toggle()\n"
        "            except Exception:\n"
        "                pass\n"
        "    except Exception:\n"
        "        pass\n"
        "    bpy.ops.wm.save_mainfile()\n"
        "    print('Packed all in', bpy.path.basename(bpy.data.filepath))\n"
        "except Exception as e:\n"
        "    print('Pack all failed:', e)\n"
    )

    result = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)

    missing: list[Path] = []
    try:
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        for line in out.splitlines():
            if "Unable to pack file, source path" in line and "not found" in line:
                start = line.find("source path '")
                end = line.rfind("'")
                if start != -1 and end != -1 and end > start + len("source path '"):
                    p = line[start + len("source path '"):end]
                    missing.append(Path(p))
    except Exception:
        pass

    _log_lines(result.stdout)
    _log_lines(result.stderr)
    return missing


def pack_linked_in_blend(blend_path: Path) -> None:
    """Open a blend and run Pack Linked (pack libraries), then save with autopack on."""
    import subprocess

    script = (
        "import bpy\n"
        "count = 0\n"
        "# Ensure relative paths are set before packing libraries\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative()\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    bpy.ops.file.pack_libraries()\n"
        "    count += 1\n"
        "except Exception:\n"
        "    pass\n"
        "# Ensure autopack ON at final save (set prefs if available; only toggle if not)\n"
        "try:\n"
        "    fp = bpy.context.preferences.filepaths\n"
        "    ensured = False\n"
        "    for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
        "        if hasattr(fp, k):\n"
        "            try:\n"
        "                if not bool(getattr(fp, k)):\n"
        "                    setattr(fp, k, True)\n"
        "                ensured = bool(getattr(fp, k))\n"
        "                break\n"
        "            except Exception:\n"
        "                pass\n"
        "    if not ensured:\n"
        "        try:\n"
        "            bpy.ops.file.autopack_toggle()\n"
        "        except Exception:\n"
        "            pass\n"
        "except Exception:\n"
        "    pass\n"
        "bpy.ops.wm.save_mainfile()\n"
        "print('Pack Linked operations:', count, 'in', bpy.path.basename(bpy.data.filepath))\n"
    )

    result = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)
    _log_lines(result.stdout)
    _log_lines(result.stderr)


def capture_render_settings(blend_path: Path) -> RenderSettingsSnapshot | None:
    """Capture basic render settings from the given blend file."""
    import subprocess, json as _json

    script = (
        "import bpy, json\n"
        "payload = {'engine': None, 'cycles_device': None, 'compute_device_type': None}\n"
        "scene = getattr(bpy.context, 'scene', None)\n"
        "if scene is not None:\n"
        "    try:\n"
        "        payload['engine'] = getattr(scene.render, 'engine', None)\n"
        "    except Exception:\n"
        "        pass\n"
        "    if payload.get('engine') == 'CYCLES':\n"
        "        try:\n"
        "            payload['cycles_device'] = getattr(scene.cycles, 'device', None)\n"
        "        except Exception:\n"
        "            pass\n"
        "try:\n"
        "    prefs = bpy.context.preferences.addons.get('cycles')\n"
        "    if prefs and hasattr(prefs, 'preferences'):\n"
        "        payload['compute_device_type'] = getattr(prefs.preferences, 'compute_device_type', None)\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps(payload))\n"
    )

    res = subprocess.run(
        ["blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    payload_line = None
    for line in reversed(out.splitlines()):
        ls = line.strip()
        if ls.startswith("{") and ls.endswith("}"):
            payload_line = ls
            break
    if not payload_line:
        return None
    try:
        data = _json.loads(payload_line)
    except Exception:
        return None
    return RenderSettingsSnapshot(
        engine=data.get("engine"),
        cycles_device=data.get("cycles_device"),
        compute_device_type=data.get("compute_device_type"),
    )


def apply_render_settings_to_blend(
    blend_path: Path, settings: RenderSettingsSnapshot
) -> None:
    """Apply stored render settings to the target blend file."""
    import subprocess, json as _json

    payload = dataclasses.asdict(settings)
    payload_json = _json.dumps(payload)
    script = (
        "import bpy, json\n"
        f"payload = json.loads(r'''{payload_json}''')\n"
        "engine = payload.get('engine')\n"
        "target_device = payload.get('cycles_device')\n"
        "if engine == 'CYCLES' and not target_device:\n"
        "    target_device = 'GPU'\n"
        "for sc in bpy.data.scenes:\n"
        "    if engine:\n"
        "        try:\n"
        "            sc.render.engine = engine\n"
        "        except Exception:\n"
        "            pass\n"
        "    if engine == 'CYCLES' and target_device:\n"
        "        try:\n"
        "            sc.cycles.device = target_device\n"
        "        except Exception:\n"
        "            pass\n"
        "if engine == 'CYCLES':\n"
        "    pref_type = payload.get('compute_device_type')\n"
        "    if pref_type:\n"
        "        try:\n"
        "            prefs = bpy.context.preferences.addons.get('cycles')\n"
        "            if prefs and hasattr(prefs, 'preferences'):\n"
        "                prefs.preferences.compute_device_type = pref_type\n"
        "        except Exception:\n"
        "            pass\n"
        "bpy.ops.wm.save_mainfile()\n"
        "print('Applied render settings to', bpy.path.basename(bpy.data.filepath))\n"
    )

    result = subprocess.run(
        ["blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script],
        capture_output=True,
        text=True,
        check=False,
    )
    _log_lines(result.stdout)
    _log_lines(result.stderr)


def save_blend_copy_as(blend_path: Path, destination: Path) -> bool:
    """Save a copy of blend_path as destination via Blender to keep metadata intact."""
    import subprocess, json as _json

    dest_json = _json.dumps(str(destination))
    script = (
        "import bpy\n"
        f"dest = {dest_json}\n"
        "from pathlib import Path\n"
        "Path(dest).parent.mkdir(parents=True, exist_ok=True)\n"
        "bpy.ops.wm.save_as_mainfile(filepath=dest, copy=True)\n"
        "print('Saved copy to', dest)\n"
    )

    result = subprocess.run(
        ["blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script],
        capture_output=True,
        text=True,
        check=False,
    )
    _log_lines(result.stdout)
    _log_lines(result.stderr)
    if result.returncode != 0:
        print(f"✗ Failed to save copy of {blend_path.name} to {destination}")
        return False
    print(f"✓ Saved copy of {blend_path.name} to {destination}")
    return True


def _flatten_relative_blend_name(blend_path: Path, root: Path) -> str:
    # Always prefer the source filename itself; flattening path segments made
    # download names noisy and harder to map back to the original file.
    return blend_path.name


def _unique_destination(base_dir: Path, filename: str) -> Path:
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        alt = base_dir / f"{stem}-{counter}{suffix}"
        if not alt.exists():
            return alt
        counter += 1


def resolve_downloads_dir() -> Path:
    hints = [os.environ.get("USERPROFILE"), os.environ.get("HOME")]
    for hint in hints:
        if hint:
            return Path(hint).expanduser() / "Downloads"
    return Path.home() / "Downloads"


def export_blends_to_downloads(
    blend_paths: list[Path], root: Path, downloads_dir: Path
) -> list[Path]:
    """Save copies of blend files into the Downloads directory for easy access."""
    downloaded: list[Path] = []
    try:
        downloads_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"✗ Unable to create Downloads directory {downloads_dir}: {exc}")
        return downloaded
    for blend_path in blend_paths:
        if blend_path.suffix.lower() != ".blend":
            continue
        flattened = _flatten_relative_blend_name(blend_path, root)
        destination = _unique_destination(downloads_dir, flattened)
        if save_blend_copy_as(blend_path, destination):
            downloaded.append(destination)
    return downloaded


def enable_nla_in_blend(blend_path: Path, autopack_on_save: bool = True) -> None:
    """Open a blend and ensure NLA tracks/strips are enabled and unmuted."""
    import subprocess

    autopack_block = ""
    if autopack_on_save:
        autopack_block = (
            "try:\n"
            "    fp = bpy.context.preferences.filepaths\n"
            "    ensured = None\n"
            "    for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
            "        if hasattr(fp, k):\n"
            "            try:\n"
            "                if not bool(getattr(fp, k)):\n"
            "                    setattr(fp, k, True)\n"
            "                ensured = bool(getattr(fp, k))\n"
            "                break\n"
            "            except Exception:\n"
            "                pass\n"
            "    if ensured is not True:\n"
            "        try:\n"
            "            bpy.ops.file.autopack_toggle()\n"
            "            ensured = True\n"
            "        except Exception:\n"
            "            pass\n"
            "    print('Autopack ensured:', ensured)\n"
            "except Exception:\n"
            "    pass\n"
        )
    script = (
        "import bpy\n"
        "obj_count = 0\n"
        "track_edits = 0\n"
        "strip_edits = 0\n"
        "nla_enabled_objects = 0\n"
        "tweak_disabled = 0\n"
        "scene_tweak_off = 0\n"
        "tweak_op_exits = 0\n"
        "influence_fixes = 0\n"
        "cleared_actions = 0\n"
        "targeted_forced = 0\n"
        "remaining = []\n"
        "# Ensure scene-level NLA tweak mode is off (can block stack evaluation)\n"
        "try:\n"
        "    if hasattr(bpy.context, 'scene') and hasattr(bpy.context.scene, 'is_nla_tweakmode') and bpy.context.scene.is_nla_tweakmode:\n"
        "        bpy.context.scene.is_nla_tweakmode = False\n"
        "        scene_tweak_off += 1\n"
        "except Exception:\n"
        "    pass\n"
        "for obj in bpy.data.objects:\n"
        "    ad = getattr(obj, 'animation_data', None)\n"
        "    if not ad:\n"
        "        continue\n"
        "    tracks = getattr(ad, 'nla_tracks', None)\n"
        "    # Enable NLA stack evaluation whenever disabled (regardless of track presence)\n"
        "    if hasattr(ad, 'use_nla') and not getattr(ad, 'use_nla', True):\n"
        "        try:\n"
        "            ad.use_nla = True\n"
        "            nla_enabled_objects += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "    # Try to exit/toggle off any tweak mode flags if present\n"
        "    for prop in ('is_tweakmode', 'use_tweak_mode', 'is_tweak_mode'):\n"
        "        try:\n"
        "            val = getattr(ad, prop) if hasattr(ad, prop) else None\n"
        "        except Exception:\n"
        "            val = None\n"
        "        if isinstance(val, bool) and val:\n"
        "            try:\n"
        "                setattr(ad, prop, False)\n"
        "                tweak_disabled += 1\n"
        "            except Exception:\n"
        "                pass\n"
        "    # Rig-specific hard enable: for armatures named 'rig' or 'rig.xxx', force stack\n"
        "    if (getattr(obj, 'type', '') == 'ARMATURE') and obj.name.lower().startswith('rig'):\n"
        "        try:\n"
        "            if hasattr(ad, 'use_nla'):\n"
        "                ad.use_nla = True\n"
        "            if tracks and getattr(ad, 'action', None) is not None:\n"
        "                ad.action = None\n"
        "                cleared_actions += 1\n"
        "            for tr in (tracks or []):\n"
        "                if hasattr(tr, 'lock') and tr.lock:\n"
        "                    tr.lock = False\n"
        "                tr.mute = False\n"
        "                if hasattr(tr, 'is_solo') and tr.is_solo:\n"
        "                    tr.is_solo = False\n"
        "                for st in getattr(tr, 'strips', []):\n"
        "                    if hasattr(st, 'mute') and st.mute:\n"
        "                        st.mute = False\n"
        "                    if hasattr(st, 'use_animated_influence') and hasattr(st, 'influence') and (not st.use_animated_influence) and float(getattr(st, 'influence', 1.0)) == 0.0:\n"
        "                        st.influence = 1.0\n"
        "                        influence_fixes += 1\n"
        "            targeted_forced += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "    # Apply same enabling to Armature data-block animation (if any)\n"
        "    try:\n"
        "        data_ad = getattr(getattr(obj, 'data', None), 'animation_data', None)\n"
        "    except Exception:\n"
        "        data_ad = None\n"
        "    if data_ad:\n"
        "        try:\n"
        "            if hasattr(data_ad, 'use_nla') and not getattr(data_ad, 'use_nla', True):\n"
        "                data_ad.use_nla = True\n"
        "                nla_enabled_objects += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "        dtracks = getattr(data_ad, 'nla_tracks', None)\n"
        "        try:\n"
        "            if dtracks and getattr(data_ad, 'action', None) is not None:\n"
        "                data_ad.action = None\n"
        "                cleared_actions += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "        for dtr in (dtracks or []):\n"
        "            try:\n"
        "                if hasattr(dtr, 'lock') and dtr.lock:\n"
        "                    dtr.lock = False\n"
        "                dtr.mute = False\n"
        "                if hasattr(dtr, 'is_solo') and dtr.is_solo:\n"
        "                    dtr.is_solo = False\n"
        "                for dst in getattr(dtr, 'strips', []):\n"
        "                    if hasattr(dst, 'mute') and dst.mute:\n"
        "                        dst.mute = False\n"
        "                    if hasattr(dst, 'use_animated_influence') and hasattr(dst, 'influence') and (not dst.use_animated_influence) and float(getattr(dst, 'influence', 1.0)) == 0.0:\n"
        "                        dst.influence = 1.0\n"
        "                        influence_fixes += 1\n"
        "            except Exception:\n"
        "                pass\n"
        "    # Avoid operators that require view-layer context; operate data-only\n"
        "    if not tracks:\n"
        "        continue\n"
        "    # If an active action exists alongside NLA tracks, clear it to ensure stack evaluation\n"
        "    try:\n"
        "        if getattr(ad, 'action', None) is not None:\n"
        "            ad.action = None\n"
        "            cleared_actions += 1\n"
        "    except Exception:\n"
        "        pass\n"
        "    for tr in tracks:\n"
        "        try:\n"
        "            if hasattr(tr, 'lock') and tr.lock:\n"
        "                tr.lock = False\n"
        "            tr.mute = False\n"
        "            if hasattr(tr, 'is_solo') and tr.is_solo:\n"
        "                tr.is_solo = False\n"
        "            track_edits += 1\n"
        "            for st in getattr(tr, 'strips', []):\n"
        "                try:\n"
        "                    if hasattr(st, 'mute') and st.mute:\n"
        "                        st.mute = False\n"
        "                    # If influence is zero and not animated, set to 1.0\n"
        "                    if hasattr(st, 'use_animated_influence') and hasattr(st, 'influence'):\n"
        "                        if (not getattr(st, 'use_animated_influence')) and float(getattr(st, 'influence', 1.0)) == 0.0:\n"
        "                            st.influence = 1.0\n"
        "                            influence_fixes += 1\n"
        "                    strip_edits += 1\n"
        "                except Exception:\n"
        "                    pass\n"
        "        except Exception:\n"
        "            pass\n"
        "    # Collect diagnostics if still disabled\n"
        "    any_muted = False\n"
        "    for tr in tracks:\n"
        "        if getattr(tr, 'mute', False):\n"
        "            any_muted = True\n"
        "            break\n"
        "        for st in getattr(tr, 'strips', []):\n"
        "            if getattr(st, 'mute', False) or getattr(st, 'influence', 1.0) == 0.0:\n"
        "                any_muted = True\n"
        "                break\n"
        "    if hasattr(ad, 'use_nla') and not ad.use_nla:\n"
        "        any_muted = True\n"
        "    if any_muted:\n"
        "        remaining.append(obj.name)\n"
        "    obj_count += 1\n"
        "# Nudge the frame to refresh dependency graph evaluation\n"
        "try:\n"
        "    sc = bpy.context.scene\n"
        "    cur = sc.frame_current\n"
        "    sc.frame_set(cur + 1)\n"
        "    sc.frame_set(cur)\n"
        "except Exception:\n"
        "    pass\n"
        f"{autopack_block}"
        "bpy.ops.wm.save_mainfile()\n"
        "print('Enabled NLA in', obj_count, 'objects; tracks', track_edits, 'strips', strip_edits, 'nla_enabled_objects', nla_enabled_objects, 'tweak_disabled', tweak_disabled, 'tweak_op_exits', tweak_op_exits, 'influence_fixes', influence_fixes, 'cleared_actions', cleared_actions, 'targeted_forced', targeted_forced, 'scene_tweak_off', scene_tweak_off, 'in', bpy.path.basename(bpy.data.filepath))\n"
        "if remaining:\n"
        "    print('NLA still muted/disabled for objects:', ', '.join(remaining[:10]))\n"
    )

    result = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)
    _log_lines(result.stdout)
    _log_lines(result.stderr)


def cleanup_blend_backups(root: Path) -> int:
    """Delete .blendN backup files (N in 1..32) under the given root."""
    removed = 0
    try:
        for path in root.rglob("*.blend?"):
            # Match .blend1 - .blend32 only
            suffix = path.suffix.lower()
            if not suffix.startswith(".blend"):
                continue
            try:
                n = int(suffix.replace(".blend", ""))
            except ValueError:
                continue
            if 1 <= n <= 32:
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


def parse_cli_args() -> CLIArgs:
    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1 :]
    else:
        argv = []

    current_blendfile_dir = Path(bpy.data.filepath).resolve().parent

    my_name = Path(__file__).name
    parser = argparse.ArgumentParser(my_name)
    parser.add_argument("-r", "--root", type=Path, default=current_blendfile_dir)
    parser.add_argument("--enable-nla", action="store_true", help="Enable and unmute NLA tracks/strips before packing")
    # Pack Linked control (default on)
    parser.add_argument("--pack-linked", dest="pack_linked", action="store_true", default=True, help="Run Pack Linked (pack libraries) bottom-up [default]")
    parser.add_argument("--no-pack-linked", dest="pack_linked", action="store_false", help="Disable Pack Linked step")
    workflow_choices = [mode.value for mode in WorkflowMode]
    parser.add_argument(
        "--workflow",
        choices=workflow_choices,
        default=WorkflowMode.PACK_AND_SAVE.value,
        help="Select workflow: copy-only (no packing) or pack-and-save (default)",
    )
    parser.add_argument(
        "--mirror-render-settings",
        action="store_true",
        help="Mirror render settings from the current blend to all linked blends (copy-only workflow helper)",
    )
    parser.add_argument("target", type=Path)
    args = parser.parse_args(argv)

    return CLIArgs(
        root_path=args.root.resolve(),
        target_path=args.target,
        enable_nla=bool(getattr(args, "enable_nla", False)),
        pack_linked=bool(getattr(args, "pack_linked", True)),
        workflow=WorkflowMode(args.workflow),
        mirror_render_settings=bool(getattr(args, "mirror_render_settings", False)),
    )


if __name__ == "__main__":
    main()
