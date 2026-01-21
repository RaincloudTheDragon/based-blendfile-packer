"""
Packing operations for SheepIt Project Submitter.
Based on pack.py from Dr. Sybren's Batter project.
"""

import os
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

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
    result = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)
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


def pack_project(workflow: str, target_path: Optional[Path] = None, enable_nla: bool = True) -> Path:
    """
    Main packing function.
    
    Args:
        workflow: Either 'copy-only' or 'pack-and-save'
        target_path: Target directory (if None, uses temp directory)
        enable_nla: Whether to enable NLA tracks
    
    Returns:
        Path to the packed output directory
    """
    if target_path is None:
        target_path = Path(tempfile.mkdtemp(prefix="sheepit_pack_"))
    
    copy_only_mode = workflow == WorkflowMode.COPY_ONLY
    autopack_on_save = not copy_only_mode
    run_pack_linked = not copy_only_mode
    
    # Find asset usages
    asset_usages = au.find()
    top_level_blend_abs = au.library_abspath(None).resolve()
    
    # Collect all file paths
    all_filepaths = []
    all_filepaths.extend(au.library_abspath(lib) for lib in asset_usages.keys())
    all_filepaths.extend(
        asset_usage.abspath
        for asset_usages in asset_usages.values()
        for asset_usage in asset_usages
    )
    
    # Determine common root
    try:
        common_root_str = os.path.commonpath(all_filepaths)
    except ValueError:
        blend_file_drive = Path(bpy.data.filepath).drive if hasattr(Path(bpy.data.filepath), 'drive') else ""
        project_filepaths = [p for p in all_filepaths if getattr(p, "drive", "") == blend_file_drive]
        if project_filepaths:
            common_root_str = os.path.commonpath(project_filepaths)
        else:
            common_root_str = str(Path(bpy.data.filepath).parent)
    
    if not common_root_str:
        raise ValueError("Could not find a common root directory for these assets.")
    
    common_root = Path(common_root_str)
    
    # Copy files
    copied_paths = set()
    copy_map = {}
    missing_on_copy = []
    
    # Copy top-level blend
    current_blend_abspath = top_level_blend_abs
    try:
        current_relpath = current_blend_abspath.relative_to(common_root)
    except ValueError:
        current_relpath = compute_target_relpath(current_blend_abspath, common_root)
    
    top_level_target_blend = None
    if current_blend_abspath not in copied_paths:
        target_path_file = target_path / current_relpath
        try:
            target_path_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current_blend_abspath, target_path_file)
            copied_paths.add(current_blend_abspath)
            if current_blend_abspath.suffix.lower() == ".blend":
                copy_map[str(current_blend_abspath.resolve())] = str(target_path_file.resolve())
            top_level_target_blend = target_path_file.resolve()
            # Copy caches
            copy_blend_caches(current_blend_abspath, target_path_file, missing_on_copy)
        except Exception as e:
            missing_on_copy.append(current_blend_abspath)
    
    # Copy other assets
    for lib, links_to in asset_usages.items():
        for asset_usage in links_to:
            if asset_usage.abspath in copied_paths:
                continue
            
            try:
                asset_relpath = asset_usage.abspath.relative_to(common_root)
            except ValueError:
                asset_relpath = compute_target_relpath(asset_usage.abspath, common_root)
            
            if not asset_usage.abspath.exists():
                missing_on_copy.append(asset_usage.abspath)
                continue
            
            target_asset_path = target_path / asset_relpath
            try:
                target_asset_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(asset_usage.abspath, target_asset_path)
                copied_paths.add(asset_usage.abspath)
                if asset_usage.is_blendfile and asset_usage.abspath.suffix.lower() == ".blend":
                    copy_map[str(asset_usage.abspath.resolve())] = str(target_asset_path.resolve())
            except Exception as e:
                missing_on_copy.append(asset_usage.abspath)
    
    # Remap library paths
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
    
    # Enable NLA before packing
    if enable_nla:
        for blend_to_fix in to_remap:
            if blend_to_fix.exists():
                enable_nla_in_blend(blend_to_fix, autopack_on_save=autopack_on_save)
    
    # Remap library paths
    for blend_to_fix in to_remap:
        if blend_to_fix.exists():
            remap_library_paths(
                blend_to_fix,
                copy_map,
                common_root,
                target_path,
                ensure_autopack=autopack_on_save,
            )
    
    # Pack files if not copy-only
    if not copy_only_mode:
        for blend_to_fix in to_remap:
            if blend_to_fix.exists():
                pack_all_in_blend(blend_to_fix)
        
        if run_pack_linked:
            for blend_to_fix in to_remap:
                if blend_to_fix.exists():
                    pack_linked_in_blend(blend_to_fix)
    
    return target_path


class SHEEPIT_OT_pack_copy_only(Operator):
    """Pack project as copy-only (for scenes with caches) - creates ZIP-ready structure."""
    bl_idname = "sheepit.pack_copy_only"
    bl_label = "Pack Copy Only"
    bl_description = "Copy assets without packing (for scenes with caches). Outputs to temporary directory."
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        try:
            target_path = pack_project(WorkflowMode.COPY_ONLY, enable_nla=True)
            self.report({'INFO'}, f"Packed to: {target_path}")
            context.scene.sheepit_submit.pack_output_path = str(target_path)
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Packing failed: {str(e)}")
            return {'CANCELLED'}


class SHEEPIT_OT_pack_and_save(Operator):
    """Pack project and save (pack all assets into blend files)."""
    bl_idname = "sheepit.pack_and_save"
    bl_label = "Pack and Save"
    bl_description = "Pack all assets into blend files. Outputs to temporary directory."
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        try:
            target_path = pack_project(WorkflowMode.PACK_AND_SAVE, enable_nla=True)
            self.report({'INFO'}, f"Packed to: {target_path}")
            context.scene.sheepit_submit.pack_output_path = str(target_path)
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Packing failed: {str(e)}")
            return {'CANCELLED'}


def register():
    """Register operators."""
    bpy.utils.register_class(SHEEPIT_OT_pack_copy_only)
    bpy.utils.register_class(SHEEPIT_OT_pack_and_save)


def unregister():
    """Unregister operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_pack_and_save)
    bpy.utils.unregister_class(SHEEPIT_OT_pack_copy_only)
