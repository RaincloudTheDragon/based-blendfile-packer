"""
Packing operations for SheepIt Project Submitter.
Based on pack.py from Dr. Sybren's Batter project.
"""

import os
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

import bpy
from bpy.types import Operator
from bpy.props import EnumProperty

# Import batter asset usage module
import sys
from pathlib import Path as PathLib

# Get the addon directory
_my_dir = PathLib(__file__).resolve().parent.parent
if str(_my_dir) not in sys.path:
    sys.path.insert(0, str(_my_dir))

try:
    from batter import asset_usage as au
except ImportError:
    # Fallback: try importing directly
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "asset_usage",
        _my_dir / "batter" / "asset_usage.py"
    )
    if spec and spec.loader:
        au = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(au)
    else:
        raise ImportError("Could not import batter.asset_usage module")


class WorkflowMode:
    """Workflow mode constants."""
    COPY_ONLY = "copy-only"
    PACK_AND_SAVE = "pack-and-save"


def compute_target_relpath(abs_path: Path, base_root: Path) -> Path:
    """Return a stable relative path under the target, even if outside root."""
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
        rel_after_anchor = str(abs_path)[len(anchor):].lstrip("\\/")
        return Path(label) / Path(rel_after_anchor)


def copy_blend_caches(src_blend: Path, dst_blend: Path, missing_on_copy: list) -> int:
    """Copy common cache folders for a given .blend next to its target copy."""
    copied = 0
    try:
        src_parent = src_blend.parent
        dst_parent = dst_blend.parent
        blendname = src_blend.stem

        candidates = []
        candidates.append((src_parent / f"blendcache_{blendname}", dst_parent / f"blendcache_{blendname}"))
        candidates.append((src_parent / "bakes" / blendname, dst_parent / "bakes" / blendname))
        
        try:
            for entry in src_parent.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name.startswith("cache_"):
                    candidates.append((entry, dst_parent / entry.name))
        except Exception:
            pass

        for src_dir, dst_dir in candidates:
            try:
                if src_dir.exists() and src_dir.is_dir():
                    dst_dir.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                        copied += 1
                        continue
                    except PermissionError as e:
                        if os.name == "nt":
                            import subprocess as _sub
                            rc = _sub.run([
                                "robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1"
                            ], capture_output=True, text=True)
                            if rc.returncode < 8:
                                copied += 1
                                continue
                        raise e
            except Exception as e:
                missing_on_copy.append(src_dir)
    except Exception:
        pass
    return copied


def _run_blender_script(script: str, blend_path: Path) -> tuple[str, str, int]:
    """Run a Python script in a Blender subprocess."""
    import subprocess
    import time
    print(f"[SheepIt Pack] Running Blender script on: {blend_path.name}")
    start_time = time.time()
    result = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)
    elapsed = time.time() - start_time
    print(f"[SheepIt Pack]   Script completed in {elapsed:.2f}s, return code: {result.returncode}")
    if result.stdout:
        print(f"[SheepIt Pack]   stdout: {result.stdout[:200]}...")  # First 200 chars
    if result.stderr:
        print(f"[SheepIt Pack]   stderr: {result.stderr[:200]}...")  # First 200 chars
    return result.stdout, result.stderr, result.returncode


def remap_library_paths(blend_path: Path, copy_map: dict[str, str], common_root: Path, target_path: Path, ensure_autopack: bool = True) -> list[Path]:
    """Open a blend file and remap all library paths to be relative to the copied tree."""
    import json
    
    copy_map_json = json.dumps(copy_map)
    
    autopack_block = ""
    if ensure_autopack:
        autopack_block = (
            "try:\n"
            "    fp = bpy.context.preferences.filepaths\n"
            "    for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
            "        if hasattr(fp, k):\n"
            "            try:\n"
            "                setattr(fp, k, True)\n"
            "            except Exception:\n"
            "                pass\n"
            "except Exception:\n"
            "    pass\n"
        )
    
    remap_script = (
        "import bpy, json\n"
        "from pathlib import Path\n"
        f"copy_map = json.loads(r'''{copy_map_json}''')\n"
        f"common_root = Path(r'{str(common_root)}')\n"
        f"target_path = Path(r'{str(target_path)}')\n"
        "bpy.context.preferences.filepaths.use_relative_paths = True\n"
        "remapped = 0\n"
        "for lib in bpy.data.libraries:\n"
        "    src = lib.filepath\n"
        "    if src.startswith('//'):\n"
        "        abs_src = (Path(bpy.data.filepath).parent / src[2:]).resolve()\n"
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
        "        try:\n"
        "            rel_to_root = abs_src.relative_to(common_root)\n"
        "            new_abs = (target_path / rel_to_root).resolve()\n"
        "        except Exception:\n"
        "            pass\n"
        "    if new_abs is None:\n"
        "        continue\n"
        "    lib.filepath = str(new_abs)\n"
        "    try:\n"
        "        lib.filepath = bpy.path.relpath(str(new_abs))\n"
        "    except Exception:\n"
        "        pass\n"
        "    remapped += 1\n"
        "bpy.ops.wm.save_as_mainfile(filepath=str(Path(bpy.data.filepath)))\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative(basedir=str(Path(bpy.data.filepath).parent))\n"
        "except Exception:\n"
        "    pass\n"
        f"{autopack_block}"
        "bpy.ops.wm.save_as_mainfile(filepath=str(Path(bpy.data.filepath)))\n"
    )
    
    stdout, stderr, returncode = _run_blender_script(remap_script, blend_path)
    unresolved = []
    
    if returncode != 0:
        return unresolved
    
    return unresolved


def pack_all_in_blend(blend_path: Path) -> list[Path]:
    """Open a blend and pack all external files into it."""
    script = (
        "import bpy\n"
        "from pathlib import Path\n"
        "blend_dir = Path(bpy.data.filepath).parent\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative(basedir=str(blend_dir))\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    bpy.ops.file.pack_all()\n"
        "    try:\n"
        "        fp = bpy.context.preferences.filepaths\n"
        "        for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
        "            if hasattr(fp, k):\n"
        "                try:\n"
        "                    setattr(fp, k, True)\n"
        "                except Exception:\n"
        "                    pass\n"
        "    except Exception:\n"
        "        pass\n"
        "    bpy.ops.wm.save_mainfile()\n"
        "except Exception as e:\n"
        "    print('Pack all failed:', e)\n"
    )
    
    stdout, stderr, returncode = _run_blender_script(script, blend_path)
    missing = []
    # Parse missing files from output if needed
    return missing


def pack_linked_in_blend(blend_path: Path) -> None:
    """Open a blend and run Pack Linked (pack libraries), then save with autopack on."""
    script = (
        "import bpy\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative()\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    bpy.ops.file.pack_libraries()\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    fp = bpy.context.preferences.filepaths\n"
        "    for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
        "        if hasattr(fp, k):\n"
        "            try:\n"
        "                setattr(fp, k, True)\n"
        "            except Exception:\n"
        "                pass\n"
        "except Exception:\n"
        "    pass\n"
        "bpy.ops.wm.save_mainfile()\n"
    )
    
    _run_blender_script(script, blend_path)


def enable_nla_in_blend(blend_path: Path, autopack_on_save: bool = True) -> None:
    """Open a blend and ensure NLA tracks/strips are enabled and unmuted."""
    autopack_block = ""
    if autopack_on_save:
        autopack_block = (
            "try:\n"
            "    fp = bpy.context.preferences.filepaths\n"
            "    for k in ('use_autopack', 'use_autopack_files', 'use_auto_pack'):\n"
            "        if hasattr(fp, k):\n"
            "            try:\n"
            "                setattr(fp, k, True)\n"
            "            except Exception:\n"
            "                pass\n"
            "except Exception:\n"
            "    pass\n"
        )
    
    script = (
        "import bpy\n"
        "for obj in bpy.data.objects:\n"
        "    ad = getattr(obj, 'animation_data', None)\n"
        "    if not ad:\n"
        "        continue\n"
        "    if hasattr(ad, 'use_nla') and not getattr(ad, 'use_nla', True):\n"
        "        try:\n"
        "            ad.use_nla = True\n"
        "        except Exception:\n"
        "            pass\n"
        "    tracks = getattr(ad, 'nla_tracks', None)\n"
        "    if not tracks:\n"
        "        continue\n"
        "    for tr in tracks:\n"
        "        try:\n"
        "            if hasattr(tr, 'lock') and tr.lock:\n"
        "                tr.lock = False\n"
        "            tr.mute = False\n"
        "            if hasattr(tr, 'is_solo') and tr.is_solo:\n"
        "                tr.is_solo = False\n"
        "            for st in getattr(tr, 'strips', []):\n"
        "                try:\n"
        "                    if hasattr(st, 'mute') and st.mute:\n"
        "                        st.mute = False\n"
        "                    if hasattr(st, 'use_animated_influence') and hasattr(st, 'influence'):\n"
        "                        if (not getattr(st, 'use_animated_influence')) and float(getattr(st, 'influence', 1.0)) == 0.0:\n"
        "                            st.influence = 1.0\n"
        "                except Exception:\n"
        "                    pass\n"
        "        except Exception:\n"
        "            pass\n"
        f"{autopack_block}"
        "bpy.ops.wm.save_mainfile()\n"
    )
    
    _run_blender_script(script, blend_path)


def pack_project(workflow: str, target_path: Optional[Path] = None, enable_nla: bool = True) -> Tuple[Path, Optional[Path]]:
    """
    Main packing function.
    
    Args:
        workflow: Either 'copy-only' or 'pack-and-save'
        target_path: Target directory (if None, uses temp directory)
        enable_nla: Whether to enable NLA tracks
    
    Returns:
        Tuple of (target_path: Path, file_path: Optional[Path])
        - target_path: Path to the packed output directory
        - file_path: Path to the file to submit (ZIP for copy-only, blend for pack-and-save)
    """
    print(f"[SheepIt Pack] Starting pack process: workflow={workflow}, enable_nla={enable_nla}")
    
    if target_path is None:
        target_path = Path(tempfile.mkdtemp(prefix="sheepit_pack_"))
        print(f"[SheepIt Pack] Created temporary directory: {target_path}")
    else:
        print(f"[SheepIt Pack] Using provided target path: {target_path}")
    
    copy_only_mode = workflow == WorkflowMode.COPY_ONLY
    autopack_on_save = not copy_only_mode
    run_pack_linked = not copy_only_mode
    
    print(f"[SheepIt Pack] Mode: {'COPY_ONLY' if copy_only_mode else 'PACK_AND_SAVE'}")
    print(f"[SheepIt Pack] Autopack on save: {autopack_on_save}, Pack linked: {run_pack_linked}")
    
    # Find asset usages
    print(f"[SheepIt Pack] Finding asset usages...")
    asset_usages = au.find()
    top_level_blend_abs = au.library_abspath(None).resolve()
    print(f"[SheepIt Pack] Found {len(asset_usages)} libraries with assets")
    print(f"[SheepIt Pack] Top-level blend: {top_level_blend_abs}")
    
    # Collect all file paths
    print(f"[SheepIt Pack] Collecting all file paths...")
    all_filepaths = []
    all_filepaths.extend(au.library_abspath(lib) for lib in asset_usages.keys())
    all_filepaths.extend(
        asset_usage.abspath
        for asset_usages in asset_usages.values()
        for asset_usage in asset_usages
    )
    print(f"[SheepIt Pack] Collected {len(all_filepaths)} total file paths")
    
    # Determine common root
    print(f"[SheepIt Pack] Determining common root directory...")
    try:
        common_root_str = os.path.commonpath(all_filepaths)
        print(f"[SheepIt Pack] Common root (method 1): {common_root_str}")
    except ValueError:
        print(f"[SheepIt Pack] Method 1 failed, trying drive-based approach...")
        blend_file_drive = Path(bpy.data.filepath).drive if hasattr(Path(bpy.data.filepath), 'drive') else ""
        project_filepaths = [p for p in all_filepaths if getattr(p, "drive", "") == blend_file_drive]
        if project_filepaths:
            common_root_str = os.path.commonpath(project_filepaths)
            print(f"[SheepIt Pack] Common root (method 2): {common_root_str}")
        else:
            common_root_str = str(Path(bpy.data.filepath).parent)
            print(f"[SheepIt Pack] Common root (fallback): {common_root_str}")
    
    if not common_root_str:
        raise ValueError("Could not find a common root directory for these assets.")
    
    common_root = Path(common_root_str)
    print(f"[SheepIt Pack] Using common root: {common_root}")
    
    # Copy files
    print(f"[SheepIt Pack] Starting file copy process...")
    copied_paths = set()
    copy_map = {}
    missing_on_copy = []
    
    # Copy top-level blend
    print(f"[SheepIt Pack] Copying top-level blend file...")
    current_blend_abspath = top_level_blend_abs
    try:
        current_relpath = current_blend_abspath.relative_to(common_root)
        print(f"[SheepIt Pack]   Relative path: {current_relpath}")
    except ValueError:
        current_relpath = compute_target_relpath(current_blend_abspath, common_root)
        print(f"[SheepIt Pack]   Computed relative path: {current_relpath}")
    
    top_level_target_blend = None
    if current_blend_abspath not in copied_paths:
        target_path_file = target_path / current_relpath
        print(f"[SheepIt Pack]   Copying: {current_blend_abspath} -> {target_path_file}")
        try:
            target_path_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current_blend_abspath, target_path_file)
            copied_paths.add(current_blend_abspath)
            if current_blend_abspath.suffix.lower() == ".blend":
                copy_map[str(current_blend_abspath.resolve())] = str(target_path_file.resolve())
            top_level_target_blend = target_path_file.resolve()
            print(f"[SheepIt Pack]   Copied successfully, size: {target_path_file.stat().st_size} bytes")
            # Copy caches
            print(f"[SheepIt Pack]   Copying blend caches...")
            cache_count = copy_blend_caches(current_blend_abspath, target_path_file, missing_on_copy)
            print(f"[SheepIt Pack]   Copied {cache_count} cache directories")
        except Exception as e:
            print(f"[SheepIt Pack]   ERROR copying top-level blend: {type(e).__name__}: {str(e)}")
            missing_on_copy.append(current_blend_abspath)
    
    # Copy other assets
    print(f"[SheepIt Pack] Copying {sum(len(links) for links in asset_usages.values())} asset files...")
    asset_count = 0
    for lib, links_to in asset_usages.items():
        for asset_usage in links_to:
            if asset_usage.abspath in copied_paths:
                continue
            
            asset_count += 1
            if asset_count % 10 == 0:
                print(f"[SheepIt Pack]   Copied {asset_count} assets so far...")
            
            try:
                asset_relpath = asset_usage.abspath.relative_to(common_root)
            except ValueError:
                asset_relpath = compute_target_relpath(asset_usage.abspath, common_root)
            
            if not asset_usage.abspath.exists():
                print(f"[SheepIt Pack]   WARNING: Asset does not exist: {asset_usage.abspath}")
                missing_on_copy.append(asset_usage.abspath)
                continue
            
            target_asset_path = target_path / asset_relpath
            try:
                target_asset_path.parent.mkdir(parents=True, exist_ok=True)
                file_size = asset_usage.abspath.stat().st_size
                shutil.copy2(asset_usage.abspath, target_asset_path)
                copied_paths.add(asset_usage.abspath)
                if asset_usage.is_blendfile and asset_usage.abspath.suffix.lower() == ".blend":
                    copy_map[str(asset_usage.abspath.resolve())] = str(target_asset_path.resolve())
                if asset_count <= 5 or asset_count % 50 == 0:  # Log first 5 and every 50th
                    print(f"[SheepIt Pack]   Copied: {asset_usage.abspath.name} ({file_size} bytes)")
            except Exception as e:
                print(f"[SheepIt Pack]   ERROR copying asset {asset_usage.abspath.name}: {type(e).__name__}: {str(e)}")
                missing_on_copy.append(asset_usage.abspath)
    
    print(f"[SheepIt Pack] Finished copying assets. Total copied: {len(copied_paths)}, Missing: {len(missing_on_copy)}")
    if missing_on_copy:
        print(f"[SheepIt Pack]   Missing files: {[str(p) for p in missing_on_copy[:5]]}...")  # First 5
    
    # Remap library paths
    print(f"[SheepIt Pack] Finding blend dependencies...")
    blend_deps = au.find_blend_asset_usage()
    to_remap = []
    for abs_path in [top_level_blend_abs] + [au.library_abspath(lib) for lib in blend_deps.keys()]:
        if abs_path.suffix.lower() != ".blend":
            continue
        try:
            rel = abs_path.relative_to(common_root)
        except ValueError:
            rel = compute_target_relpath(abs_path, common_root)
        to_remap.append(target_path / rel)
    
    print(f"[SheepIt Pack] Found {len(to_remap)} blend files to process")
    
    # Enable NLA before packing
    if enable_nla:
        print(f"[SheepIt Pack] Enabling NLA tracks in blend files...")
        for i, blend_to_fix in enumerate(to_remap, 1):
            if blend_to_fix.exists():
                print(f"[SheepIt Pack]   [{i}/{len(to_remap)}] Enabling NLA in: {blend_to_fix.name}")
                enable_nla_in_blend(blend_to_fix, autopack_on_save=autopack_on_save)
        print(f"[SheepIt Pack] Finished enabling NLA")
    
    # Remap library paths
    print(f"[SheepIt Pack] Remapping library paths in blend files...")
    for i, blend_to_fix in enumerate(to_remap, 1):
        if blend_to_fix.exists():
            print(f"[SheepIt Pack]   [{i}/{len(to_remap)}] Remapping paths in: {blend_to_fix.name}")
            remap_library_paths(
                blend_to_fix,
                copy_map,
                common_root,
                target_path,
                ensure_autopack=autopack_on_save,
            )
    print(f"[SheepIt Pack] Finished remapping library paths")
    
    # Pack files if not copy-only
    if not copy_only_mode:
        print(f"[SheepIt Pack] Packing all assets into blend files...")
        for i, blend_to_fix in enumerate(to_remap, 1):
            if blend_to_fix.exists():
                print(f"[SheepIt Pack]   [{i}/{len(to_remap)}] Packing all in: {blend_to_fix.name}")
                pack_all_in_blend(blend_to_fix)
        print(f"[SheepIt Pack] Finished packing all assets")
        
        if run_pack_linked:
            print(f"[SheepIt Pack] Packing linked libraries...")
            for i, blend_to_fix in enumerate(to_remap, 1):
                if blend_to_fix.exists():
                    print(f"[SheepIt Pack]   [{i}/{len(to_remap)}] Packing linked in: {blend_to_fix.name}")
                    pack_linked_in_blend(blend_to_fix)
            print(f"[SheepIt Pack] Finished packing linked libraries")
    
    print(f"[SheepIt Pack] Pack process completed successfully!")
    print(f"[SheepIt Pack] Output directory: {target_path}")
    
    # Determine file path for submission
    file_path = None
    if copy_only_mode:
        # For copy-only, we'll create ZIP later in the operator
        # Return None here, ZIP will be created in operator
        pass
    else:
        # For pack-and-save, return the main target blend file
        if top_level_target_blend and top_level_target_blend.exists():
            file_path = top_level_target_blend
            print(f"[SheepIt Pack] Target blend file for submission: {file_path}")
        else:
            # Fallback: find the first .blend file in target_path
            blend_files = list(target_path.rglob("*.blend"))
            if blend_files:
                file_path = blend_files[0]
                print(f"[SheepIt Pack] Found blend file for submission: {file_path}")
    
    return target_path, file_path


class SHEEPIT_OT_pack_zip(Operator):
    """Pack project as ZIP (for scenes with caches) - creates ZIP and submits to SheepIt."""
    bl_idname = "sheepit.pack_zip"
    bl_label = "Submit as ZIP"
    bl_description = "Copy assets without packing (for scenes with caches), create ZIP, and submit to SheepIt"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        submit_settings = context.scene.sheepit_submit
        original_filepath = bpy.data.filepath
        temp_blend_path = None
        temp_dir = None
        
        try:
            # Save current blend state to temp file with frame range applied
            from .submit_ops import save_current_blend_with_frame_range, apply_frame_range_to_blend
            print(f"[SheepIt Pack] Saving current blend state with frame range...")
            temp_blend_path, frame_start, frame_end, frame_step = save_current_blend_with_frame_range(submit_settings)
            temp_dir = temp_blend_path.parent
            print(f"[SheepIt Pack] Saved to temp file: {temp_blend_path}")
            print(f"[SheepIt Pack] Frame range: {frame_start} - {frame_end} (step: {frame_step})")
            
            # Temporarily set bpy.data.filepath so pack_project uses the temp file
            bpy.data.filepath = str(temp_blend_path)
            print(f"[SheepIt Pack] Temporarily set bpy.data.filepath to: {temp_blend_path}")
            
            # Pack project (will use temp file as source)
            target_path, _ = pack_project(WorkflowMode.COPY_ONLY, enable_nla=True)
            print(f"[SheepIt Pack] Packed to: {target_path}")
            context.scene.sheepit_submit.pack_output_path = str(target_path)
            
            # Apply frame range to all blend files in the packed directory
            print(f"[SheepIt Pack] Applying frame range to blend files in packed directory...")
            blend_files = list(target_path.rglob("*.blend"))
            for blend_file in blend_files:
                if blend_file.exists():
                    print(f"[SheepIt Pack]   Applying frame range to: {blend_file.name}")
                    apply_frame_range_to_blend(blend_file, frame_start, frame_end, frame_step)
            
            # Restore original filepath
            bpy.data.filepath = original_filepath
            print(f"[SheepIt Pack] Restored original bpy.data.filepath")
            
            # Create ZIP
            from .submit_ops import create_zip_from_directory
            zip_path = target_path.parent / f"{target_path.name}.zip"
            print(f"[SheepIt Pack] Creating ZIP: {zip_path}")
            create_zip_from_directory(target_path, zip_path)
            
            # Submit via API
            from .api_submit import submit_file_to_sheepit
            from ..utils.compat import get_addon_prefs
            from ..utils.auth import load_auth_cookies
            
            sheepit_prefs = get_addon_prefs()
            if not sheepit_prefs:
                self.report({'ERROR'}, "Addon preferences not found. Please configure SheepIt credentials in preferences.")
                return {'CANCELLED'}
            
            # Get authentication
            auth_cookies = None
            username = None
            password = None
            
            if sheepit_prefs.use_browser_login:
                auth_cookies = load_auth_cookies()
                if not auth_cookies:
                    self.report({'ERROR'}, "No browser login session found. Please login via browser in preferences.")
                    return {'CANCELLED'}
            else:
                if not sheepit_prefs.sheepit_username or not sheepit_prefs.sheepit_password:
                    self.report({'ERROR'}, "Please configure SheepIt username and password in preferences.")
                    return {'CANCELLED'}
                username = sheepit_prefs.sheepit_username
                password = sheepit_prefs.sheepit_password
            
            # Submit
            success, message = submit_file_to_sheepit(
                zip_path,
                context.scene.sheepit_submit,
                auth_cookies=auth_cookies,
                username=username,
                password=password
            )
            
            if success:
                # Clean up temp file on success
                if temp_blend_path and temp_blend_path.exists():
                    try:
                        temp_blend_path.unlink()
                        if temp_dir and temp_dir.exists():
                            try:
                                temp_dir.rmdir()
                            except Exception:
                                pass  # Directory may not be empty
                        print(f"[SheepIt Pack] Cleaned up temp file: {temp_blend_path}")
                    except Exception as e:
                        print(f"[SheepIt Pack] WARNING: Could not clean up temp file: {e}")
                self.report({'INFO'}, message)
                return {'FINISHED'}
            else:
                # Leave temp file for debugging on failure
                if temp_blend_path:
                    print(f"[SheepIt Pack] Temp file left for debugging: {temp_blend_path}")
                self.report({'ERROR'}, message)
                return {'CANCELLED'}
                
        except Exception as e:
            # Restore original filepath on error
            bpy.data.filepath = original_filepath
            print(f"[SheepIt Pack] ERROR: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            # Leave temp file for debugging
            if temp_blend_path:
                print(f"[SheepIt Pack] Temp file left for debugging: {temp_blend_path}")
            self.report({'ERROR'}, f"Packing failed: {str(e)}")
            return {'CANCELLED'}


class SHEEPIT_OT_pack_blend(Operator):
    """Pack project and save (pack all assets into blend files) - submits blend to SheepIt."""
    bl_idname = "sheepit.pack_blend"
    bl_label = "Submit as Blend"
    bl_description = "Pack all assets into blend files and submit to SheepIt"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        submit_settings = context.scene.sheepit_submit
        original_filepath = bpy.data.filepath
        temp_blend_path = None
        temp_dir = None
        
        try:
            # Save current blend state to temp file with frame range applied
            from .submit_ops import save_current_blend_with_frame_range, apply_frame_range_to_blend
            print(f"[SheepIt Pack] Saving current blend state with frame range...")
            temp_blend_path, frame_start, frame_end, frame_step = save_current_blend_with_frame_range(submit_settings)
            temp_dir = temp_blend_path.parent
            print(f"[SheepIt Pack] Saved to temp file: {temp_blend_path}")
            print(f"[SheepIt Pack] Frame range: {frame_start} - {frame_end} (step: {frame_step})")
            
            # Temporarily set bpy.data.filepath so pack_project uses the temp file
            bpy.data.filepath = str(temp_blend_path)
            print(f"[SheepIt Pack] Temporarily set bpy.data.filepath to: {temp_blend_path}")
            
            # Pack project (will use temp file as source)
            target_path, blend_path = pack_project(WorkflowMode.PACK_AND_SAVE, enable_nla=True)
            print(f"[SheepIt Pack] Packed to: {target_path}")
            context.scene.sheepit_submit.pack_output_path = str(target_path)
            
            if not blend_path or not blend_path.exists():
                # Restore original filepath before returning
                bpy.data.filepath = original_filepath
                self.report({'ERROR'}, "Could not find target blend file for submission.")
                return {'CANCELLED'}
            
            # Apply frame range to the target blend file before submission
            print(f"[SheepIt Pack] Applying frame range to target blend file: {blend_path.name}")
            apply_frame_range_to_blend(blend_path, frame_start, frame_end, frame_step)
            
            # Restore original filepath
            bpy.data.filepath = original_filepath
            print(f"[SheepIt Pack] Restored original bpy.data.filepath")
            
            # Submit via API
            from .api_submit import submit_file_to_sheepit
            from ..utils.compat import get_addon_prefs
            from ..utils.auth import load_auth_cookies
            
            sheepit_prefs = get_addon_prefs()
            if not sheepit_prefs:
                self.report({'ERROR'}, "Addon preferences not found. Please configure SheepIt credentials in preferences.")
                return {'CANCELLED'}
            
            # Get authentication
            auth_cookies = None
            username = None
            password = None
            
            if sheepit_prefs.use_browser_login:
                auth_cookies = load_auth_cookies()
                if not auth_cookies:
                    self.report({'ERROR'}, "No browser login session found. Please login via browser in preferences.")
                    return {'CANCELLED'}
            else:
                if not sheepit_prefs.sheepit_username or not sheepit_prefs.sheepit_password:
                    self.report({'ERROR'}, "Please configure SheepIt username and password in preferences.")
                    return {'CANCELLED'}
                username = sheepit_prefs.sheepit_username
                password = sheepit_prefs.sheepit_password
            
            # Submit
            success, message = submit_file_to_sheepit(
                blend_path,
                context.scene.sheepit_submit,
                auth_cookies=auth_cookies,
                username=username,
                password=password
            )
            
            if success:
                # Clean up temp file on success
                if temp_blend_path and temp_blend_path.exists():
                    try:
                        temp_blend_path.unlink()
                        if temp_dir and temp_dir.exists():
                            try:
                                temp_dir.rmdir()
                            except Exception:
                                pass  # Directory may not be empty
                        print(f"[SheepIt Pack] Cleaned up temp file: {temp_blend_path}")
                    except Exception as e:
                        print(f"[SheepIt Pack] WARNING: Could not clean up temp file: {e}")
                self.report({'INFO'}, message)
                return {'FINISHED'}
            else:
                # Leave temp file for debugging on failure
                if temp_blend_path:
                    print(f"[SheepIt Pack] Temp file left for debugging: {temp_blend_path}")
                self.report({'ERROR'}, message)
                return {'CANCELLED'}
                
        except Exception as e:
            # Restore original filepath on error
            bpy.data.filepath = original_filepath
            print(f"[SheepIt Pack] ERROR: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            # Leave temp file for debugging
            if temp_blend_path:
                print(f"[SheepIt Pack] Temp file left for debugging: {temp_blend_path}")
            self.report({'ERROR'}, f"Packing failed: {str(e)}")
            return {'CANCELLED'}


def register():
    """Register operators."""
    bpy.utils.register_class(SHEEPIT_OT_pack_zip)
    bpy.utils.register_class(SHEEPIT_OT_pack_blend)


def unregister():
    """Unregister operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_pack_blend)
    bpy.utils.unregister_class(SHEEPIT_OT_pack_zip)
