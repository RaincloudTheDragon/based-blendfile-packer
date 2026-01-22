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


def copy_blend_caches(src_blend: Path, dst_blend: Path, missing_on_copy: list) -> list[Path]:
    """Copy common cache folders for a given .blend next to its target copy.
    
    Returns:
        List of copied cache directory paths (for truncation)
    """
    copied = []
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
                        copied.append(dst_dir)
                        continue
                    except PermissionError as e:
                        if os.name == "nt":
                            import subprocess as _sub
                            rc = _sub.run([
                                "robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1"
                            ], capture_output=True, text=True)
                            if rc.returncode < 8:
                                copied.append(dst_dir)
                                continue
                        raise e
            except Exception as e:
                missing_on_copy.append(src_dir)
    except Exception:
        pass
    return copied


def truncate_caches_to_frame_range(cache_dir: Path, frame_start: int, frame_end: int, frame_step: int) -> int:
    """
    Remove cache files outside the specified frame range.
    
    This helps reduce ZIP size by only including cache files for frames that will be rendered.
    
    Handles common cache naming patterns:
    - Numbered sequences: frame_0001.vdb, frame_0002.vdb, cache_fluid_0042.bphys.gz, etc.
    - Extract frame numbers using regex: r'(\d+)' or r'frame_(\d+)', r'cache.*?(\d+)', etc.
    - Only keep files where extracted frame number is within [frame_start, frame_end] and matches frame_step
    
    Returns number of files removed.
    """
    import re
    files_removed = 0
    valid_frames = set(range(frame_start, frame_end + 1, frame_step))
    
    for cache_file in cache_dir.rglob("*"):
        if not cache_file.is_file():
            continue
        
        # Try to extract frame number from filename
        frame_num = None
        # Pattern 1: frame_####.ext or cache_####.ext
        match = re.search(r'(?:frame_|cache[^_]*_)(\d+)', cache_file.stem, re.IGNORECASE)
        if match:
            frame_num = int(match.group(1))
        else:
            # Pattern 2: Just numbers at end of filename
            match = re.search(r'(\d+)$', cache_file.stem)
            if match:
                frame_num = int(match.group(1))
        
        if frame_num is not None and frame_num not in valid_frames:
            try:
                cache_file.unlink()
                files_removed += 1
            except Exception as e:
                print(f"[SheepIt Pack] WARNING: Could not remove {cache_file.name}: {e}")
    
    return files_removed


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


class IncrementalPacker:
    """Stateful incremental packer that processes files in batches across multiple timer events."""
    
    def __init__(self, workflow: str, target_path: Optional[Path], enable_nla: bool, 
                 progress_callback=None, cancel_check=None,
                 frame_start=None, frame_end=None, frame_step=None):
        self.workflow = workflow
        self.target_path = target_path
        self.enable_nla = enable_nla
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check
        self.frame_start = frame_start  # For cache truncation
        self.frame_end = frame_end
        self.frame_step = frame_step
        
        # State tracking
        self.phase = 'INIT'
        self.copy_only_mode = workflow == WorkflowMode.COPY_ONLY
        self.autopack_on_save = not self.copy_only_mode
        self.run_pack_linked = not self.copy_only_mode
        
        # Asset finding state
        self.asset_usages = None
        self.top_level_blend_abs = None
        self.all_filepaths = []
        self.common_root = None
        
        # File copying state
        self.copied_paths = set()
        self.copy_map = {}
        self.missing_on_copy = []
        self.assets_to_copy = []  # List of (asset_usage, target_path, common_root) tuples
        self.assets_copied = 0
        self.top_level_target_blend = None
        self.cache_dirs = []  # List of cache directories to truncate
        
        # Blend processing state
        self.blend_deps = None
        self.to_remap = []
        self.nla_index = 0
        self.remap_index = 0
        self.pack_all_index = 0
        self.pack_linked_index = 0
        
        # Cache truncation state
        self.cache_truncate_index = 0
        
        # Results
        self.file_path = None
        self.error = None
    
    def process_batch(self, batch_size: int = 20) -> Tuple[str, bool]:
        """
        Process one batch of work.
        
        Returns:
            Tuple of (next_phase, is_complete)
            - next_phase: Next phase name to continue with
            - is_complete: True if packing is fully complete
        """
        if self.cancel_check and self.cancel_check():
            raise InterruptedError("Packing cancelled by user")
        
        if self.phase == 'INIT':
            if self.target_path is None:
                self.target_path = Path(tempfile.mkdtemp(prefix="sheepit_pack_"))
                print(f"[SheepIt Pack] Created temporary directory: {self.target_path}")
            else:
                print(f"[SheepIt Pack] Using provided target path: {self.target_path}")
            
            print(f"[SheepIt Pack] Mode: {'COPY_ONLY' if self.copy_only_mode else 'PACK_AND_SAVE'}")
            self.phase = 'FIND_ASSETS'
            return ('FIND_ASSETS', False)
        
        elif self.phase == 'FIND_ASSETS':
            print(f"[SheepIt Pack] Finding asset usages...")
            if self.progress_callback:
                self.progress_callback(5.0, "Finding asset usages...")
            self.asset_usages = au.find()
            self.top_level_blend_abs = au.library_abspath(None).resolve()
            print(f"[SheepIt Pack] Found {len(self.asset_usages)} libraries with assets")
            print(f"[SheepIt Pack] Top-level blend: {self.top_level_blend_abs}")
            self.phase = 'COLLECT_PATHS'
            return ('COLLECT_PATHS', False)
        
        elif self.phase == 'COLLECT_PATHS':
            print(f"[SheepIt Pack] Collecting all file paths...")
            if self.progress_callback:
                self.progress_callback(10.0, "Collecting file paths...")
            self.all_filepaths = []
            self.all_filepaths.extend(au.library_abspath(lib) for lib in self.asset_usages.keys())
            self.all_filepaths.extend(
                asset_usage.abspath
                for asset_usages in self.asset_usages.values()
                for asset_usage in asset_usages
            )
            print(f"[SheepIt Pack] Collected {len(self.all_filepaths)} total file paths")
            self.phase = 'FIND_COMMON_ROOT'
            return ('FIND_COMMON_ROOT', False)
        
        elif self.phase == 'FIND_COMMON_ROOT':
            print(f"[SheepIt Pack] Determining common root directory...")
            try:
                common_root_str = os.path.commonpath(self.all_filepaths)
                print(f"[SheepIt Pack] Common root (method 1): {common_root_str}")
            except ValueError:
                print(f"[SheepIt Pack] Method 1 failed, trying drive-based approach...")
                blend_file_drive = Path(bpy.data.filepath).drive if hasattr(Path(bpy.data.filepath), 'drive') else ""
                project_filepaths = [p for p in self.all_filepaths if getattr(p, "drive", "") == blend_file_drive]
                if project_filepaths:
                    common_root_str = os.path.commonpath(project_filepaths)
                    print(f"[SheepIt Pack] Common root (method 2): {common_root_str}")
                else:
                    common_root_str = str(Path(bpy.data.filepath).parent)
                    print(f"[SheepIt Pack] Common root (fallback): {common_root_str}")
            
            if not common_root_str:
                raise ValueError("Could not find a common root directory for these assets.")
            
            self.common_root = Path(common_root_str)
            print(f"[SheepIt Pack] Using common root: {self.common_root}")
            self.phase = 'PREPARE_COPY_TOP_BLEND'
            return ('PREPARE_COPY_TOP_BLEND', False)
        
        elif self.phase == 'PREPARE_COPY_TOP_BLEND':
            print(f"[SheepIt Pack] Copying top-level blend file...")
            if self.progress_callback:
                self.progress_callback(15.0, "Copying top-level blend file...")
            
            current_blend_abspath = self.top_level_blend_abs
            try:
                current_relpath = current_blend_abspath.relative_to(self.common_root)
                print(f"[SheepIt Pack]   Relative path: {current_relpath}")
            except ValueError:
                current_relpath = compute_target_relpath(current_blend_abspath, self.common_root)
                print(f"[SheepIt Pack]   Computed relative path: {current_relpath}")
            
            if current_blend_abspath not in self.copied_paths:
                target_path_file = self.target_path / current_relpath
                print(f"[SheepIt Pack]   Copying: {current_blend_abspath} -> {target_path_file}")
                try:
                    target_path_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(current_blend_abspath, target_path_file)
                    self.copied_paths.add(current_blend_abspath)
                    if current_blend_abspath.suffix.lower() == ".blend":
                        self.copy_map[str(current_blend_abspath.resolve())] = str(target_path_file.resolve())
                    self.top_level_target_blend = target_path_file.resolve()
                    print(f"[SheepIt Pack]   Copied successfully, size: {target_path_file.stat().st_size} bytes")
                    # Copy caches
                    print(f"[SheepIt Pack]   Copying blend caches...")
                    copied_cache_dirs = copy_blend_caches(current_blend_abspath, target_path_file, self.missing_on_copy)
                    self.cache_dirs.extend(copied_cache_dirs)
                    print(f"[SheepIt Pack]   Copied {len(copied_cache_dirs)} cache directories")
                except Exception as e:
                    print(f"[SheepIt Pack]   ERROR copying top-level blend: {type(e).__name__}: {str(e)}")
                    self.missing_on_copy.append(current_blend_abspath)
            
            # Prepare asset copy list
            total_assets = sum(len(links) for links in self.asset_usages.values())
            print(f"[SheepIt Pack] Preparing to copy {total_assets} asset files...")
            for lib, links_to in self.asset_usages.items():
                for asset_usage in links_to:
                    if asset_usage.abspath in self.copied_paths:
                        continue
                    try:
                        asset_relpath = asset_usage.abspath.relative_to(self.common_root)
                    except ValueError:
                        asset_relpath = compute_target_relpath(asset_usage.abspath, self.common_root)
                    self.assets_to_copy.append((asset_usage, asset_relpath))
            
            self.assets_copied = 0
            self.phase = 'COPY_ASSETS'
            return ('COPY_ASSETS', False)
        
        elif self.phase == 'COPY_ASSETS':
            # Copy batch_size assets
            total_assets = len(self.assets_to_copy)
            batch_end = min(self.assets_copied + batch_size, total_assets)
            
            for i in range(self.assets_copied, batch_end):
                asset_usage, asset_relpath = self.assets_to_copy[i]
                
                if not asset_usage.abspath.exists():
                    print(f"[SheepIt Pack]   WARNING: Asset does not exist: {asset_usage.abspath}")
                    self.missing_on_copy.append(asset_usage.abspath)
                    continue
                
                target_asset_path = self.target_path / asset_relpath
                try:
                    target_asset_path.parent.mkdir(parents=True, exist_ok=True)
                    file_size = asset_usage.abspath.stat().st_size
                    shutil.copy2(asset_usage.abspath, target_asset_path)
                    self.copied_paths.add(asset_usage.abspath)
                    if asset_usage.is_blendfile and asset_usage.abspath.suffix.lower() == ".blend":
                        self.copy_map[str(asset_usage.abspath.resolve())] = str(target_asset_path.resolve())
                    if (i < 5) or (i % 50 == 0):
                        print(f"[SheepIt Pack]   Copied: {asset_usage.abspath.name} ({file_size} bytes)")
                except Exception as e:
                    print(f"[SheepIt Pack]   ERROR copying asset {asset_usage.abspath.name}: {type(e).__name__}: {str(e)}")
                    self.missing_on_copy.append(asset_usage.abspath)
            
            self.assets_copied = batch_end
            
            # Update progress
            progress_pct = 15.0 + (self.assets_copied / total_assets * 30.0) if total_assets > 0 else 15.0
            if self.progress_callback:
                self.progress_callback(progress_pct, f"Copying assets... ({self.assets_copied}/{total_assets})")
            if self.assets_copied % 10 == 0 or self.assets_copied == total_assets:
                print(f"[SheepIt Pack]   Copied {self.assets_copied}/{total_assets} assets ({progress_pct:.1f}%)...")
            
            if self.assets_copied >= total_assets:
                print(f"[SheepIt Pack] Finished copying assets. Total copied: {len(self.copied_paths)}, Missing: {len(self.missing_on_copy)}")
                if self.missing_on_copy:
                    print(f"[SheepIt Pack]   Missing files: {[str(p) for p in self.missing_on_copy[:5]]}...")
                # Check if we need to truncate caches
                if self.frame_start is not None and self.frame_end is not None and self.frame_step is not None and self.cache_dirs:
                    self.cache_truncate_index = 0
                    self.phase = 'TRUNCATING_CACHES'
                    return ('TRUNCATING_CACHES', False)
                else:
                    self.phase = 'FIND_DEPENDENCIES'
                    return ('FIND_DEPENDENCIES', False)
            else:
                return ('COPY_ASSETS', False)  # More batches needed
        
        elif self.phase == 'TRUNCATING_CACHES':
            if self.cache_truncate_index == 0:
                print(f"[SheepIt Pack] Truncating caches to frame range {self.frame_start}-{self.frame_end} (step: {self.frame_step})...")
                if self.progress_callback:
                    self.progress_callback(45.0, "Truncating caches to frame range...")
            
            # Process one cache directory per batch
            if self.cache_truncate_index < len(self.cache_dirs):
                cache_dir = self.cache_dirs[self.cache_truncate_index]
                if cache_dir.exists() and cache_dir.is_dir():
                    progress_pct = 45.0 + ((self.cache_truncate_index + 1) / len(self.cache_dirs) * 0.5) if self.cache_dirs else 45.0
                    if self.progress_callback:
                        self.progress_callback(progress_pct, f"Truncating caches... ({self.cache_truncate_index + 1}/{len(self.cache_dirs)} cache directories)")
                    print(f"[SheepIt Pack]   [{self.cache_truncate_index + 1}/{len(self.cache_dirs)}] Truncating cache: {cache_dir.name}")
                    files_removed = truncate_caches_to_frame_range(cache_dir, self.frame_start, self.frame_end, self.frame_step)
                    print(f"[SheepIt Pack]   Removed {files_removed} cache files outside frame range")
                self.cache_truncate_index += 1
                return ('TRUNCATING_CACHES', False)
            else:
                print(f"[SheepIt Pack] Finished truncating caches to frame range {self.frame_start}-{self.frame_end}")
                self.phase = 'FIND_DEPENDENCIES'
                return ('FIND_DEPENDENCIES', False)
        
        elif self.phase == 'FIND_DEPENDENCIES':
            print(f"[SheepIt Pack] Finding blend dependencies...")
            if self.progress_callback:
                self.progress_callback(45.0, "Finding blend dependencies...")
            self.blend_deps = au.find_blend_asset_usage()
            self.to_remap = []
            for abs_path in [self.top_level_blend_abs] + [au.library_abspath(lib) for lib in self.blend_deps.keys()]:
                if abs_path.suffix.lower() != ".blend":
                    continue
                try:
                    rel = abs_path.relative_to(self.common_root)
                except ValueError:
                    rel = compute_target_relpath(abs_path, self.common_root)
                self.to_remap.append(self.target_path / rel)
            
            print(f"[SheepIt Pack] Found {len(self.to_remap)} blend files to process")
            self.nla_index = 0
            self.phase = 'ENABLE_NLA' if self.enable_nla else 'REMAP_PATHS'
            return (self.phase, False)
        
        elif self.phase == 'ENABLE_NLA':
            if self.nla_index == 0:
                print(f"[SheepIt Pack] Enabling NLA tracks in blend files...")
                if self.progress_callback:
                    self.progress_callback(50.0, "Enabling NLA tracks...")
            
            # Process one blend file per batch
            if self.nla_index < len(self.to_remap):
                blend_to_fix = self.to_remap[self.nla_index]
                if blend_to_fix.exists():
                    progress_pct = 50.0 + ((self.nla_index + 1) / len(self.to_remap) * 5.0) if self.to_remap else 50.0
                    if self.progress_callback:
                        self.progress_callback(progress_pct, f"Enabling NLA in blend files... ({self.nla_index + 1}/{len(self.to_remap)})")
                    print(f"[SheepIt Pack]   [{self.nla_index + 1}/{len(self.to_remap)}] Enabling NLA in: {blend_to_fix.name}")
                    enable_nla_in_blend(blend_to_fix, autopack_on_save=self.autopack_on_save)
                self.nla_index += 1
                return ('ENABLE_NLA', False)
            else:
                print(f"[SheepIt Pack] Finished enabling NLA")
                self.remap_index = 0
                self.phase = 'REMAP_PATHS'
                return ('REMAP_PATHS', False)
        
        elif self.phase == 'REMAP_PATHS':
            if self.remap_index == 0:
                print(f"[SheepIt Pack] Remapping library paths in blend files...")
                if self.progress_callback:
                    self.progress_callback(55.0, "Remapping library paths...")
            
            # Process one blend file per batch
            if self.remap_index < len(self.to_remap):
                blend_to_fix = self.to_remap[self.remap_index]
                if blend_to_fix.exists():
                    progress_pct = 55.0 + ((self.remap_index + 1) / len(self.to_remap) * 10.0) if self.to_remap else 55.0
                    if self.progress_callback:
                        self.progress_callback(progress_pct, f"Remapping paths... ({self.remap_index + 1}/{len(self.to_remap)})")
                    print(f"[SheepIt Pack]   [{self.remap_index + 1}/{len(self.to_remap)}] Remapping paths in: {blend_to_fix.name}")
                    remap_library_paths(
                        blend_to_fix,
                        self.copy_map,
                        self.common_root,
                        self.target_path,
                        ensure_autopack=self.autopack_on_save,
                    )
                self.remap_index += 1
                return ('REMAP_PATHS', False)
            else:
                print(f"[SheepIt Pack] Finished remapping library paths")
                if not self.copy_only_mode:
                    self.pack_all_index = 0
                    self.phase = 'PACK_ALL'
                    return ('PACK_ALL', False)
                else:
                    self.phase = 'COMPLETE'
                    return ('COMPLETE', False)
        
        elif self.phase == 'PACK_ALL':
            if self.pack_all_index == 0:
                print(f"[SheepIt Pack] Packing all assets into blend files...")
                if self.progress_callback:
                    self.progress_callback(65.0, "Packing assets into blend files...")
            
            # Process one blend file per batch
            if self.pack_all_index < len(self.to_remap):
                blend_to_fix = self.to_remap[self.pack_all_index]
                if blend_to_fix.exists():
                    progress_pct = 65.0 + ((self.pack_all_index + 1) / len(self.to_remap) * 15.0) if self.to_remap else 65.0
                    if self.progress_callback:
                        self.progress_callback(progress_pct, f"Packing assets... ({self.pack_all_index + 1}/{len(self.to_remap)})")
                    print(f"[SheepIt Pack]   [{self.pack_all_index + 1}/{len(self.to_remap)}] Packing all in: {blend_to_fix.name}")
                    pack_all_in_blend(blend_to_fix)
                self.pack_all_index += 1
                return ('PACK_ALL', False)
            else:
                print(f"[SheepIt Pack] Finished packing all assets")
                if self.run_pack_linked:
                    self.pack_linked_index = 0
                    self.phase = 'PACK_LINKED'
                    return ('PACK_LINKED', False)
                else:
                    self.phase = 'COMPLETE'
                    return ('COMPLETE', False)
        
        elif self.phase == 'PACK_LINKED':
            if self.pack_linked_index == 0:
                print(f"[SheepIt Pack] Packing linked libraries...")
                if self.progress_callback:
                    self.progress_callback(80.0, "Packing linked libraries...")
            
            # Process one blend file per batch
            if self.pack_linked_index < len(self.to_remap):
                blend_to_fix = self.to_remap[self.pack_linked_index]
                if blend_to_fix.exists():
                    progress_pct = 80.0 + ((self.pack_linked_index + 1) / len(self.to_remap) * 15.0) if self.to_remap else 80.0
                    if self.progress_callback:
                        self.progress_callback(progress_pct, f"Packing linked... ({self.pack_linked_index + 1}/{len(self.to_remap)})")
                    print(f"[SheepIt Pack]   [{self.pack_linked_index + 1}/{len(self.to_remap)}] Packing linked in: {blend_to_fix.name}")
                    pack_linked_in_blend(blend_to_fix)
                self.pack_linked_index += 1
                return ('PACK_LINKED', False)
            else:
                print(f"[SheepIt Pack] Finished packing linked libraries")
                self.phase = 'COMPLETE'
                return ('COMPLETE', False)
        
        elif self.phase == 'COMPLETE':
            print(f"[SheepIt Pack] Pack process completed successfully!")
            print(f"[SheepIt Pack] Output directory: {self.target_path}")
            
            # Determine file path for submission
            if self.copy_only_mode:
                # For copy-only, we'll create ZIP later in the operator
                self.file_path = None
            else:
                # For pack-and-save, return the main target blend file
                if self.top_level_target_blend and self.top_level_target_blend.exists():
                    self.file_path = self.top_level_target_blend
                    print(f"[SheepIt Pack] Target blend file for submission: {self.file_path}")
                else:
                    # Fallback: find the first .blend file in target_path
                    blend_files = list(self.target_path.rglob("*.blend"))
                    if blend_files:
                        self.file_path = blend_files[0]
                        print(f"[SheepIt Pack] Found blend file for submission: {self.file_path}")
            
            return ('COMPLETE', True)
        
        return (self.phase, False)


def pack_project(workflow: str, target_path: Optional[Path] = None, enable_nla: bool = True, 
                 progress_callback=None, cancel_check=None) -> Tuple[Path, Optional[Path]]:
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
    if progress_callback:
        progress_callback(5.0, "Finding asset usages...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
    asset_usages = au.find()
    top_level_blend_abs = au.library_abspath(None).resolve()
    print(f"[SheepIt Pack] Found {len(asset_usages)} libraries with assets")
    print(f"[SheepIt Pack] Top-level blend: {top_level_blend_abs}")
    
    # Collect all file paths
    print(f"[SheepIt Pack] Collecting all file paths...")
    if progress_callback:
        progress_callback(10.0, "Collecting file paths...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
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
    if progress_callback:
        progress_callback(15.0, "Starting file copy process...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
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
    total_assets = sum(len(links) for links in asset_usages.values())
    print(f"[SheepIt Pack] Copying {total_assets} asset files...")
    asset_count = 0
    for lib, links_to in asset_usages.items():
        for asset_usage in links_to:
            if asset_usage.abspath in copied_paths:
                continue
            
            asset_count += 1
            # Update progress every 10 files or every 1% of total
            if asset_count % 10 == 0 or (total_assets > 0 and asset_count % max(1, total_assets // 100) == 0):
                progress_pct = 15.0 + (asset_count / total_assets * 30.0) if total_assets > 0 else 15.0
                if progress_callback:
                    progress_callback(progress_pct, f"Copying assets... ({asset_count}/{total_assets})")
                print(f"[SheepIt Pack]   Copied {asset_count}/{total_assets} assets ({progress_pct:.1f}%)...")
            if cancel_check and cancel_check():
                raise InterruptedError("Packing cancelled by user")
            
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
    if progress_callback:
        progress_callback(45.0, "Finding blend dependencies...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
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
        if progress_callback:
            progress_callback(50.0, "Enabling NLA tracks...")
        for i, blend_to_fix in enumerate(to_remap, 1):
            if cancel_check and cancel_check():
                raise InterruptedError("Packing cancelled by user")
            if blend_to_fix.exists():
                progress_pct = 50.0 + (i / len(to_remap) * 5.0) if to_remap else 50.0
                if progress_callback:
                    progress_callback(progress_pct, f"Enabling NLA in blend files... ({i}/{len(to_remap)})")
                print(f"[SheepIt Pack]   [{i}/{len(to_remap)}] Enabling NLA in: {blend_to_fix.name}")
                enable_nla_in_blend(blend_to_fix, autopack_on_save=autopack_on_save)
        print(f"[SheepIt Pack] Finished enabling NLA")
    
    # Remap library paths
    print(f"[SheepIt Pack] Remapping library paths in blend files...")
    if progress_callback:
        progress_callback(55.0, "Remapping library paths...")
    for i, blend_to_fix in enumerate(to_remap, 1):
        if cancel_check and cancel_check():
            raise InterruptedError("Packing cancelled by user")
        if blend_to_fix.exists():
            progress_pct = 55.0 + (i / len(to_remap) * 10.0) if to_remap else 55.0
            if progress_callback:
                progress_callback(progress_pct, f"Remapping paths... ({i}/{len(to_remap)})")
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
        if progress_callback:
            progress_callback(65.0, "Packing assets into blend files...")
        for i, blend_to_fix in enumerate(to_remap, 1):
            if cancel_check and cancel_check():
                raise InterruptedError("Packing cancelled by user")
            if blend_to_fix.exists():
                progress_pct = 65.0 + (i / len(to_remap) * 15.0) if to_remap else 65.0
                if progress_callback:
                    progress_callback(progress_pct, f"Packing assets... ({i}/{len(to_remap)})")
                print(f"[SheepIt Pack]   [{i}/{len(to_remap)}] Packing all in: {blend_to_fix.name}")
                pack_all_in_blend(blend_to_fix)
        print(f"[SheepIt Pack] Finished packing all assets")
        
        if run_pack_linked:
            print(f"[SheepIt Pack] Packing linked libraries...")
            if progress_callback:
                progress_callback(80.0, "Packing linked libraries...")
            for i, blend_to_fix in enumerate(to_remap, 1):
                if cancel_check and cancel_check():
                    raise InterruptedError("Packing cancelled by user")
                if blend_to_fix.exists():
                    progress_pct = 80.0 + (i / len(to_remap) * 15.0) if to_remap else 80.0
                    if progress_callback:
                        progress_callback(progress_pct, f"Packing linked... ({i}/{len(to_remap)})")
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
    
    def invoke(self, context, event):
        """Initialize modal operator with timer."""
        submit_settings = context.scene.sheepit_submit
        
        print(f"[SheepIt Pack] DEBUG: SHEEPIT_OT_pack_zip.invoke called")
        
        # Check if already submitting
        if submit_settings.is_submitting:
            print(f"[SheepIt Pack] DEBUG: Already submitting, cancelling")
            self.report({'WARNING'}, "A submission is already in progress.")
            return {'CANCELLED'}
        
        # Initialize progress properties
        submit_settings.is_submitting = True
        submit_settings.submit_progress = 0.0
        submit_settings.submit_status_message = "Initializing..."
        
        # Initialize phase tracking
        self._phase = 'INIT'
        self._original_filepath = bpy.data.filepath
        self._temp_blend_path = None
        self._temp_dir = None
        self._target_path = None
        self._zip_path = None
        self._frame_start = None
        self._frame_end = None
        self._frame_step = None
        self._auth_cookies = None
        self._username = None
        self._password = None
        self._success = False
        self._message = ""
        self._error = None
        self._packer = None  # IncrementalPacker instance
        
        print(f"[SheepIt Pack] DEBUG: Initialized with original_filepath: {self._original_filepath}")
        
        # Create timer for modal updates
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        print(f"[SheepIt Pack] DEBUG: Timer created: {self._timer}")
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
        
        # Start modal operation
        context.window_manager.modal_handler_add(self)
        print(f"[SheepIt Pack] DEBUG: Modal handler added, returning RUNNING_MODAL")
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        """Handle modal events and update progress."""
        submit_settings = context.scene.sheepit_submit
        
        # Debug: Log all events (but filter out noisy ones)
        if event.type not in ('TIMER', 'MOUSEMOVE', 'WINDOW_DEACTIVATE'):
            print(f"[SheepIt Pack] DEBUG: Modal event received: type={event.type}, value={getattr(event, 'value', 'N/A')}")
        
        # Handle ESC key to cancel
        if event.type == 'ESC':
            print(f"[SheepIt Pack] DEBUG: ESC key pressed, cancelling")
            self._cleanup(context, cancelled=True)
            self.report({'INFO'}, "Submission cancelled.")
            return {'CANCELLED'}
        
        # Handle timer events
        if event.type == 'TIMER':
            try:
                print(f"[SheepIt Pack] DEBUG: Modal timer event, current phase: {self._phase}")
                
                if self._phase == 'INIT':
                    print(f"[SheepIt Pack] DEBUG: Entering INIT phase")
                    submit_settings.submit_progress = 0.0
                    submit_settings.submit_status_message = "Initializing..."
                    self._phase = 'SAVING_BLEND'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to SAVING_BLEND phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_BLEND':
                    print(f"[SheepIt Pack] DEBUG: Entering SAVING_BLEND phase")
                    submit_settings.submit_progress = 5.0
                    submit_settings.submit_status_message = "Saving current blend state..."
                    
                    from .submit_ops import save_current_blend_with_frame_range, apply_frame_range_to_blend
                    
                    print(f"[SheepIt Pack] DEBUG: About to call save_current_blend_with_frame_range")
                    try:
                        self._temp_blend_path, self._frame_start, self._frame_end, self._frame_step = save_current_blend_with_frame_range(submit_settings)
                        self._temp_dir = self._temp_blend_path.parent
                        print(f"[SheepIt Pack] DEBUG: save_current_blend_with_frame_range completed")
                        print(f"[SheepIt Pack] Saved to temp file: {self._temp_blend_path}")
                        print(f"[SheepIt Pack] DEBUG: Frame range: {self._frame_start}-{self._frame_end} (step: {self._frame_step})")
                    except Exception as e:
                        print(f"[SheepIt Pack] DEBUG: ERROR in SAVING_BLEND: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = f"Failed to save current blend state: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'APPLYING_FRAME_RANGE'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to APPLYING_FRAME_RANGE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE':
                    print(f"[SheepIt Pack] DEBUG: Entering APPLYING_FRAME_RANGE phase")
                    submit_settings.submit_progress = 10.0
                    submit_settings.submit_status_message = "Frame range applied."
                    # Frame range is already applied in save_current_blend_with_frame_range
                    self._phase = 'OVERRIDING_FILEPATH'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to OVERRIDING_FILEPATH phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'OVERRIDING_FILEPATH':
                    print(f"[SheepIt Pack] DEBUG: Entering OVERRIDING_FILEPATH phase")
                    submit_settings.submit_progress = 12.0
                    submit_settings.submit_status_message = "Preparing for packing..."
                    
                    print(f"[SheepIt Pack] DEBUG: Temp file exists: {self._temp_blend_path.exists() if self._temp_blend_path else 'N/A'}")
                    print(f"[SheepIt Pack] DEBUG: Current bpy.data.filepath: {bpy.data.filepath}")
                    
                    # Temporarily override library_abspath to use temp file instead of opening it
                    # This avoids invalidating the operator instance
                    from batter import asset_usage as au
                    import functools
                    
                    # Store original function
                    self._original_library_abspath = au.library_abspath
                    
                    # Create override function
                    temp_file_path = self._temp_blend_path.resolve()
                    def override_library_abspath(lib):
                        if lib is None:
                            return temp_file_path
                        else:
                            return self._original_library_abspath(lib)
                    
                    # Replace the function (clear cache first)
                    au.library_abspath.cache_clear()
                    au.library_abspath = override_library_abspath
                    # Re-apply lru_cache decorator behavior by wrapping
                    au.library_abspath = functools.lru_cache(maxsize=None)(override_library_abspath)
                    
                    print(f"[SheepIt Pack] DEBUG: Overrode library_abspath to use temp file: {temp_file_path}")
                    
                    # Initialize IncrementalPacker
                    def progress_callback(progress_pct, message):
                        """Update progress during packing."""
                        # Map packer progress (0-100%) to operator progress (15-61%)
                        submit_settings.submit_progress = 15.0 + (progress_pct * 0.46)
                        submit_settings.submit_status_message = message
                        print(f"[SheepIt Pack] DEBUG: Progress update: {submit_settings.submit_progress:.1f}% - {message}")
                        # Force UI redraw on every update
                        for area in context.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
                    
                    def cancel_check():
                        """Check if user wants to cancel."""
                        return not submit_settings.is_submitting
                    
                    self._packer = IncrementalPacker(
                        WorkflowMode.COPY_ONLY,
                        target_path=None,
                        enable_nla=True,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                        frame_start=self._frame_start,
                        frame_end=self._frame_end,
                        frame_step=self._frame_step
                    )
                    
                    self._phase = 'PACKING_INIT'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to PACKING_INIT phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'PACKING_INIT' or self._phase.startswith('PACKING_'):
                    # Handle all packing sub-phases using IncrementalPacker
                    try:
                        # Process one batch
                        next_phase, is_complete = self._packer.process_batch(batch_size=20)
                        
                        if is_complete:
                            # Packing is complete
                            self._target_path = self._packer.target_path
                            print(f"[SheepIt Pack] DEBUG: Incremental packing completed")
                            print(f"[SheepIt Pack] Packed to: {self._target_path}")
                            context.scene.sheepit_submit.pack_output_path = str(self._target_path)
                            self._phase = 'APPLYING_FRAME_RANGE_TO_PACKED'
                            print(f"[SheepIt Pack] DEBUG: Transitioning to APPLYING_FRAME_RANGE_TO_PACKED phase")
                        else:
                            # Continue with next phase from packer (prepend PACKING_ prefix)
                            self._phase = f'PACKING_{next_phase}'
                            print(f"[SheepIt Pack] DEBUG: Packing phase: {self._phase}, continuing...")
                        
                        return {'RUNNING_MODAL'}
                    except InterruptedError as e:
                        print(f"[SheepIt Pack] DEBUG: Packing cancelled by user")
                        self._cleanup(context, cancelled=True)
                        self.report({'INFO'}, "Packing cancelled.")
                        return {'CANCELLED'}
                    except Exception as e:
                        print(f"[SheepIt Pack] DEBUG: ERROR in PACKING: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = f"Packing failed: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE_TO_PACKED':
                    print(f"[SheepIt Pack] DEBUG: Entering APPLYING_FRAME_RANGE_TO_PACKED phase")
                    submit_settings.submit_progress = 60.0
                    submit_settings.submit_status_message = "Applying frame range to packed files..."
                    
                    from .submit_ops import apply_frame_range_to_blend
                    
                    print(f"[SheepIt Pack] DEBUG: Searching for blend files in: {self._target_path}")
                    # Apply frame range to all blend files in the packed directory
                    blend_files = list(self._target_path.rglob("*.blend"))
                    print(f"[SheepIt Pack] DEBUG: Found {len(blend_files)} blend files")
                    
                    for i, blend_file in enumerate(blend_files, 1):
                        # Check for cancellation
                        if not submit_settings.is_submitting:
                            print(f"[SheepIt Pack] DEBUG: Cancellation detected in APPLYING_FRAME_RANGE_TO_PACKED")
                            raise InterruptedError("Packing cancelled by user")
                        if blend_file.exists():
                            progress_pct = 60.0 + (i / len(blend_files) * 2.0) if blend_files else 60.0
                            submit_settings.submit_progress = progress_pct
                            submit_settings.submit_status_message = f"Applying frame range... ({i}/{len(blend_files)})"
                            print(f"[SheepIt Pack] DEBUG: [{i}/{len(blend_files)}] Applying frame range to: {blend_file.name} ({progress_pct:.1f}%)")
                            apply_frame_range_to_blend(blend_file, self._frame_start, self._frame_end, self._frame_step)
                            # Force UI redraw after each file
                            for area in context.screen.areas:
                                if area.type == 'PROPERTIES':
                                    area.tag_redraw()
                    
                    self._phase = 'RESTORING_LIBRARY_ABSPATH'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to RESTORING_LIBRARY_ABSPATH phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'RESTORING_LIBRARY_ABSPATH':
                    print(f"[SheepIt Pack] DEBUG: Entering RESTORING_LIBRARY_ABSPATH phase")
                    submit_settings.submit_progress = 62.0
                    submit_settings.submit_status_message = "Restoring file paths..."
                    
                    # Restore original library_abspath function
                    if hasattr(self, '_original_library_abspath'):
                        from batter import asset_usage as au
                        au.library_abspath.cache_clear()
                        au.library_abspath = self._original_library_abspath
                        print(f"[SheepIt Pack] DEBUG: Restored original library_abspath function")
                    
                    self._phase = 'VALIDATING_FILE_SIZE'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to VALIDATING_FILE_SIZE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_FILE_SIZE':
                    print(f"[SheepIt Pack] DEBUG: Entering VALIDATING_FILE_SIZE phase (before ZIP)")
                    submit_settings.submit_progress = 64.0
                    submit_settings.submit_status_message = "Validating file size..."
                    
                    # Estimate packed directory size
                    total_size = 0
                    file_count = 0
                    for root, dirs, files in os.walk(self._target_path):
                        for file in files:
                            file_path = Path(root) / file
                            if file_path.exists():
                                total_size += file_path.stat().st_size
                                file_count += 1
                    
                    total_size_gb = total_size / (1024 * 1024 * 1024)
                    MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
                    
                    print(f"[SheepIt Pack] Estimated packed directory size: {total_size_gb:.2f} GB ({file_count} files)")
                    
                    if total_size > MAX_FILE_SIZE:
                        error_msg = (
                            f"Estimated packed size ({total_size_gb:.2f} GB) exceeds 2GB limit. Cannot create ZIP.\n\n"
                            "To reduce file size, consider:\n"
                            "- Optimizing the scene (reduce geometry, simplify materials)\n"
                            "- Optimizing asset files (compress textures, reduce resolution)\n"
                            "- Splitting the frame range (render in smaller chunks)\n"
                            "- Truncating caches to match your selected frame range\n"
                            "  (Note: Caches are automatically truncated to your selected frame range during packing)"
                        )
                        print(f"[SheepIt Pack] ERROR: {error_msg}")
                        self._error = error_msg
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'CREATING_ZIP'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to CREATING_ZIP phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'CREATING_ZIP':
                    print(f"[SheepIt Pack] DEBUG: Entering CREATING_ZIP phase")
                    submit_settings.submit_progress = 65.0
                    submit_settings.submit_status_message = "Creating ZIP archive..."
                    
                    from .submit_ops import create_zip_from_directory
                    
                    self._zip_path = self._target_path.parent / f"{self._target_path.name}.zip"
                    print(f"[SheepIt Pack] DEBUG: Creating ZIP: {self._zip_path}")
                    print(f"[SheepIt Pack] DEBUG: Source directory: {self._target_path}")
                    
                    # Create progress callback for ZIP creation
                    def zip_progress_callback(progress_pct, message):
                        """Update progress during ZIP creation."""
                        # Map 0-100% to 65-80% range
                        submit_settings.submit_progress = 65.0 + (progress_pct * 0.15)
                        submit_settings.submit_status_message = message
                        print(f"[SheepIt Pack] DEBUG: ZIP progress: {submit_settings.submit_progress:.1f}% - {message}")
                        # Force UI redraw on every update
                        for area in context.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
                    
                    def zip_cancel_check():
                        """Check if user wants to cancel."""
                        return not submit_settings.is_submitting
                    
                    try:
                        create_zip_from_directory(
                            self._target_path, 
                            self._zip_path,
                            progress_callback=zip_progress_callback,
                            cancel_check=zip_cancel_check
                        )
                        submit_settings.submit_progress = 80.0
                        submit_settings.submit_status_message = "ZIP archive created"
                        print(f"[SheepIt Pack] DEBUG: ZIP creation completed")
                        print(f"[SheepIt Pack] Creating ZIP: {self._zip_path}")
                    except InterruptedError as e:
                        print(f"[SheepIt Pack] DEBUG: ZIP creation cancelled by user")
                        self._cleanup(context, cancelled=True)
                        self.report({'INFO'}, "ZIP creation cancelled.")
                        return {'CANCELLED'}
                    except Exception as e:
                        print(f"[SheepIt Pack] DEBUG: ERROR creating ZIP: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = f"ZIP creation failed: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'VALIDATING_ZIP_SIZE'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to VALIDATING_ZIP_SIZE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_ZIP_SIZE':
                    print(f"[SheepIt Pack] DEBUG: Entering VALIDATING_ZIP_SIZE phase")
                    submit_settings.submit_progress = 80.5
                    submit_settings.submit_status_message = "Validating ZIP size..."
                    
                    # Check final ZIP size
                    if self._zip_path and self._zip_path.exists():
                        zip_size = self._zip_path.stat().st_size
                        zip_size_gb = zip_size / (1024 * 1024 * 1024)
                        MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
                        
                        print(f"[SheepIt Pack] Final ZIP size: {zip_size_gb:.2f} GB")
                        
                        if zip_size > MAX_FILE_SIZE:
                            error_msg = (
                                f"ZIP size ({zip_size_gb:.2f} GB) exceeds 2GB limit. Cannot submit.\n\n"
                                "To reduce file size, consider:\n"
                                "- Optimizing the scene (reduce geometry, simplify materials)\n"
                                "- Optimizing asset files (compress textures, reduce resolution)\n"
                                "- Splitting the frame range (render in smaller chunks)\n"
                                "- Truncating caches to match your selected frame range\n"
                                "  (Note: Caches are automatically truncated to your selected frame range during packing)"
                            )
                            print(f"[SheepIt Pack] ERROR: {error_msg}")
                            self._error = error_msg
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    
                    self._phase = 'AUTHENTICATING'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to AUTHENTICATING phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'AUTHENTICATING':
                    submit_settings.submit_progress = 85.0
                    submit_settings.submit_status_message = "Authenticating..."
                    
                    from ..utils.compat import get_addon_prefs
                    from ..utils.auth import load_auth_cookies
                    
                    sheepit_prefs = get_addon_prefs()
                    if not sheepit_prefs:
                        self._error = "Addon preferences not found. Please configure SheepIt credentials in preferences."
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    # Get authentication
                    if sheepit_prefs.use_browser_login:
                        self._auth_cookies = load_auth_cookies()
                        if not self._auth_cookies:
                            self._error = "No browser login session found. Please login via browser in preferences."
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    else:
                        if not sheepit_prefs.sheepit_username or not sheepit_prefs.sheepit_password:
                            self._error = "Please configure SheepIt username and password in preferences."
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                        self._username = sheepit_prefs.sheepit_username
                        self._password = sheepit_prefs.sheepit_password
                    
                    self._phase = 'VALIDATING_FILE_SIZE_UPLOAD'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_FILE_SIZE_UPLOAD':
                    submit_settings.submit_progress = 81.0
                    submit_settings.submit_status_message = "Validating file size before upload..."
                    
                    # File size validation is also done in submit_file_to_sheepit, but we check here for better UX
                    if self._zip_path and self._zip_path.exists():
                        zip_size = self._zip_path.stat().st_size
                        zip_size_gb = zip_size / (1024 * 1024 * 1024)
                        MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
                        
                        if zip_size > MAX_FILE_SIZE:
                            error_msg = (
                                f"ZIP size ({zip_size_gb:.2f} GB) exceeds 2GB limit. Cannot submit.\n\n"
                                "To reduce file size, consider:\n"
                                "- Optimizing the scene (reduce geometry, simplify materials)\n"
                                "- Optimizing asset files (compress textures, reduce resolution)\n"
                                "- Splitting the frame range (render in smaller chunks)\n"
                                "- Truncating caches to match your selected frame range\n"
                                "  (Note: Caches are automatically truncated to your selected frame range during packing)"
                            )
                            print(f"[SheepIt Pack] ERROR: {error_msg}")
                            self._error = error_msg
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    
                    self._phase = 'UPLOADING'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'UPLOADING':
                    submit_settings.submit_progress = 87.0
                    submit_settings.submit_status_message = "Uploading to SheepIt..."
                    
                    from .api_submit import submit_file_to_sheepit
                    
                    self._success, self._message = submit_file_to_sheepit(
                        self._zip_path,
                        context.scene.sheepit_submit,
                        auth_cookies=self._auth_cookies,
                        username=self._username,
                        password=self._password
                    )
                    
                    if self._success:
                        submit_settings.submit_progress = 95.0
                        submit_settings.submit_status_message = "Upload complete!"
                        self._phase = 'OPENING_BROWSER'
                    else:
                        self._error = self._message
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'OPENING_BROWSER':
                    submit_settings.submit_progress = 97.0
                    submit_settings.submit_status_message = "Opening browser..."
                    # Browser is already opened by submit_file_to_sheepit
                    self._phase = 'CLEANUP'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'CLEANUP':
                    submit_settings.submit_progress = 98.0
                    submit_settings.submit_status_message = "Cleaning up..."
                    
                    # Clean up temp file on success
                    if self._temp_blend_path and self._temp_blend_path.exists():
                        try:
                            self._temp_blend_path.unlink()
                            if self._temp_dir and self._temp_dir.exists():
                                try:
                                    self._temp_dir.rmdir()
                                except Exception:
                                    pass  # Directory may not be empty
                            print(f"[SheepIt Pack] Cleaned up temp file: {self._temp_blend_path}")
                        except Exception as e:
                            print(f"[SheepIt Pack] WARNING: Could not clean up temp file: {e}")
                    
                    self._phase = 'COMPLETE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'COMPLETE':
                    submit_settings.submit_progress = 100.0
                    submit_settings.submit_status_message = "Submission complete!"
                    
                    # Small delay to show completion
                    import time
                    time.sleep(0.2)
                    
                    self._cleanup(context, cancelled=False)
                    self.report({'INFO'}, self._message)
                    return {'FINISHED'}
                
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._error = f"Packing failed: {type(e).__name__}: {str(e)}"
                self._cleanup(context, cancelled=True)
                self.report({'ERROR'}, self._error)
                return {'CANCELLED'}
        
        return {'RUNNING_MODAL'}
    
    def _cleanup(self, context, cancelled=False):
        """Clean up progress properties and timer."""
        submit_settings = context.scene.sheepit_submit
        
        # Restore original library_abspath function if we overrode it
        if hasattr(self, '_original_library_abspath'):
            try:
                from batter import asset_usage as au
                au.library_abspath.cache_clear()
                au.library_abspath = self._original_library_abspath
                print(f"[SheepIt Pack] DEBUG: Restored original library_abspath in cleanup")
            except Exception as e:
                print(f"[SheepIt Pack] DEBUG: WARNING: Could not restore library_abspath: {e}")
        
        # Remove timer
        if hasattr(self, '_timer') and self._timer:
            context.window_manager.event_timer_remove(self._timer)
        
        # Reset progress properties
        submit_settings.is_submitting = False
        submit_settings.submit_progress = 0.0
        if cancelled and self._error:
            submit_settings.submit_status_message = self._error
        else:
            submit_settings.submit_status_message = ""
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)


class SHEEPIT_OT_pack_blend(Operator):
    """Pack project and save (pack all assets into blend files) - submits blend to SheepIt."""
    bl_idname = "sheepit.pack_blend"
    bl_label = "Submit as Blend"
    bl_description = "Pack all assets into blend files and submit to SheepIt"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        """Initialize modal operator with timer."""
        submit_settings = context.scene.sheepit_submit
        
        # Check if already submitting
        if submit_settings.is_submitting:
            self.report({'WARNING'}, "A submission is already in progress.")
            return {'CANCELLED'}
        
        # Initialize progress properties
        submit_settings.is_submitting = True
        submit_settings.submit_progress = 0.0
        submit_settings.submit_status_message = "Initializing..."
        
        # Initialize phase tracking
        self._phase = 'INIT'
        self._original_filepath = bpy.data.filepath
        self._temp_blend_path = None
        self._temp_dir = None
        self._target_path = None
        self._blend_path = None
        self._frame_start = None
        self._frame_end = None
        self._frame_step = None
        self._auth_cookies = None
        self._username = None
        self._password = None
        self._success = False
        self._message = ""
        self._error = None
        self._packer = None  # IncrementalPacker instance
        
        # Create timer for modal updates
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
        
        # Start modal operation
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        """Handle modal events and update progress."""
        submit_settings = context.scene.sheepit_submit
        
        # Handle ESC key to cancel
        if event.type == 'ESC':
            self._cleanup(context, cancelled=True)
            self.report({'INFO'}, "Submission cancelled.")
            return {'CANCELLED'}
        
        # Handle timer events
        if event.type == 'TIMER':
            try:
                if self._phase == 'INIT':
                    submit_settings.submit_progress = 0.0
                    submit_settings.submit_status_message = "Initializing..."
                    self._phase = 'SAVING_BLEND'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_BLEND':
                    submit_settings.submit_progress = 5.0
                    submit_settings.submit_status_message = "Saving current blend state..."
                    
                    from .submit_ops import save_current_blend_with_frame_range, apply_frame_range_to_blend
                    
                    try:
                        self._temp_blend_path, self._frame_start, self._frame_end, self._frame_step = save_current_blend_with_frame_range(submit_settings)
                        self._temp_dir = self._temp_blend_path.parent
                        print(f"[SheepIt Pack] Saved to temp file: {self._temp_blend_path}")
                    except Exception as e:
                        self._error = f"Failed to save current blend state: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'APPLYING_FRAME_RANGE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE':
                    submit_settings.submit_progress = 10.0
                    submit_settings.submit_status_message = "Frame range applied."
                    # Frame range is already applied in save_current_blend_with_frame_range
                    self._phase = 'OVERRIDING_FILEPATH'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'OVERRIDING_FILEPATH':
                    submit_settings.submit_progress = 12.0
                    submit_settings.submit_status_message = "Preparing for packing..."
                    
                    # Temporarily override library_abspath to use temp file instead of opening it
                    # This avoids invalidating the operator instance
                    from batter import asset_usage as au
                    import functools
                    
                    # Store original function
                    self._original_library_abspath = au.library_abspath
                    
                    # Create override function
                    temp_file_path = self._temp_blend_path.resolve()
                    def override_library_abspath(lib):
                        if lib is None:
                            return temp_file_path
                        else:
                            return self._original_library_abspath(lib)
                    
                    # Replace the function (clear cache first)
                    au.library_abspath.cache_clear()
                    au.library_abspath = override_library_abspath
                    # Re-apply lru_cache decorator behavior by wrapping
                    au.library_abspath = functools.lru_cache(maxsize=None)(override_library_abspath)
                    
                    print(f"[SheepIt Pack] DEBUG: Overrode library_abspath to use temp file: {temp_file_path}")
                    
                    # Initialize IncrementalPacker
                    def progress_callback(progress_pct, message):
                        """Update progress during packing."""
                        # Map packer progress (0-100%) to operator progress (15-70%)
                        submit_settings.submit_progress = 15.0 + (progress_pct * 0.55)
                        submit_settings.submit_status_message = message
                        print(f"[SheepIt Pack] DEBUG: Progress update: {submit_settings.submit_progress:.1f}% - {message}")
                        # Force UI redraw on every update
                        for area in context.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
                    
                    def cancel_check():
                        """Check if user wants to cancel."""
                        return not submit_settings.is_submitting
                    
                    self._packer = IncrementalPacker(
                        WorkflowMode.PACK_AND_SAVE,
                        target_path=None,
                        enable_nla=True,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                        frame_start=self._frame_start,
                        frame_end=self._frame_end,
                        frame_step=self._frame_step
                    )
                    
                    self._phase = 'PACKING_INIT'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'PACKING_INIT' or self._phase.startswith('PACKING_'):
                    # Handle all packing sub-phases using IncrementalPacker
                    try:
                        # Process one batch
                        next_phase, is_complete = self._packer.process_batch(batch_size=20)
                        
                        if is_complete:
                            # Packing is complete
                            self._target_path = self._packer.target_path
                            self._blend_path = self._packer.file_path
                            print(f"[SheepIt Pack] DEBUG: Incremental packing completed")
                            print(f"[SheepIt Pack] Packed to: {self._target_path}")
                            context.scene.sheepit_submit.pack_output_path = str(self._target_path)
                            
                            if not self._blend_path or not self._blend_path.exists():
                                self._error = "Could not find target blend file for submission."
                                self._cleanup(context, cancelled=True)
                                self.report({'ERROR'}, self._error)
                                return {'CANCELLED'}
                            
                            self._phase = 'APPLYING_FRAME_RANGE_TO_TARGET'
                            print(f"[SheepIt Pack] DEBUG: Transitioning to APPLYING_FRAME_RANGE_TO_TARGET phase")
                        else:
                            # Continue with next phase from packer (prepend PACKING_ prefix)
                            self._phase = f'PACKING_{next_phase}'
                            print(f"[SheepIt Pack] DEBUG: Packing phase: {self._phase}, continuing...")
                        
                        return {'RUNNING_MODAL'}
                    except InterruptedError as e:
                        print(f"[SheepIt Pack] DEBUG: Packing cancelled by user")
                        self._cleanup(context, cancelled=True)
                        self.report({'INFO'}, "Packing cancelled.")
                        return {'CANCELLED'}
                    except Exception as e:
                        print(f"[SheepIt Pack] DEBUG: ERROR in PACKING: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = f"Packing failed: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE_TO_TARGET':
                    submit_settings.submit_progress = 70.0
                    submit_settings.submit_status_message = "Applying frame range to target blend..."
                    
                    from .submit_ops import apply_frame_range_to_blend
                    
                    # Apply frame range to the target blend file before submission
                    print(f"[SheepIt Pack] Applying frame range to target blend file: {self._blend_path.name}")
                    apply_frame_range_to_blend(self._blend_path, self._frame_start, self._frame_end, self._frame_step)
                    
                    self._phase = 'RESTORING_LIBRARY_ABSPATH'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'RESTORING_LIBRARY_ABSPATH':
                    submit_settings.submit_progress = 72.0
                    submit_settings.submit_status_message = "Restoring file paths..."
                    
                    # Restore original library_abspath function
                    if hasattr(self, '_original_library_abspath'):
                        from batter import asset_usage as au
                        au.library_abspath.cache_clear()
                        au.library_abspath = self._original_library_abspath
                        print(f"[SheepIt Pack] DEBUG: Restored original library_abspath function")
                    
                    self._phase = 'VALIDATING_FILE_SIZE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_FILE_SIZE':
                    submit_settings.submit_progress = 72.5
                    submit_settings.submit_status_message = "Validating file size before upload..."
                    
                    # Check blend file size
                    if self._blend_path and self._blend_path.exists():
                        blend_size = self._blend_path.stat().st_size
                        blend_size_gb = blend_size / (1024 * 1024 * 1024)
                        MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
                        
                        print(f"[SheepIt Pack] Blend file size: {blend_size_gb:.2f} GB")
                        
                        if blend_size > MAX_FILE_SIZE:
                            error_msg = (
                                f"Blend file size ({blend_size_gb:.2f} GB) exceeds 2GB limit. Cannot submit.\n\n"
                                "To reduce file size, consider:\n"
                                "- Optimizing the scene (reduce geometry, simplify materials)\n"
                                "- Optimizing asset files (compress textures, reduce resolution)\n"
                                "- Splitting the frame range (render in smaller chunks)\n"
                                "- Truncating caches to match your selected frame range\n"
                                "  (Note: Caches are automatically truncated to your selected frame range during packing)"
                            )
                            print(f"[SheepIt Pack] ERROR: {error_msg}")
                            self._error = error_msg
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    
                    self._phase = 'AUTHENTICATING'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'AUTHENTICATING':
                    submit_settings.submit_progress = 75.0
                    submit_settings.submit_status_message = "Authenticating..."
                    
                    from ..utils.compat import get_addon_prefs
                    from ..utils.auth import load_auth_cookies
                    
                    sheepit_prefs = get_addon_prefs()
                    if not sheepit_prefs:
                        self._error = "Addon preferences not found. Please configure SheepIt credentials in preferences."
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    # Get authentication
                    if sheepit_prefs.use_browser_login:
                        self._auth_cookies = load_auth_cookies()
                        if not self._auth_cookies:
                            self._error = "No browser login session found. Please login via browser in preferences."
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    else:
                        if not sheepit_prefs.sheepit_username or not sheepit_prefs.sheepit_password:
                            self._error = "Please configure SheepIt username and password in preferences."
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                        self._username = sheepit_prefs.sheepit_username
                        self._password = sheepit_prefs.sheepit_password
                    
                    self._phase = 'UPLOADING'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'UPLOADING':
                    submit_settings.submit_progress = 77.0
                    submit_settings.submit_status_message = "Uploading to SheepIt..."
                    
                    from .api_submit import submit_file_to_sheepit
                    
                    self._success, self._message = submit_file_to_sheepit(
                        self._blend_path,
                        context.scene.sheepit_submit,
                        auth_cookies=self._auth_cookies,
                        username=self._username,
                        password=self._password
                    )
                    
                    if self._success:
                        submit_settings.submit_progress = 90.0
                        submit_settings.submit_status_message = "Upload complete!"
                        self._phase = 'OPENING_BROWSER'
                    else:
                        self._error = self._message
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'OPENING_BROWSER':
                    submit_settings.submit_progress = 92.0
                    submit_settings.submit_status_message = "Opening browser..."
                    # Browser is already opened by submit_file_to_sheepit
                    self._phase = 'CLEANUP'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'CLEANUP':
                    submit_settings.submit_progress = 98.0
                    submit_settings.submit_status_message = "Cleaning up..."
                    
                    # Clean up temp file on success
                    if self._temp_blend_path and self._temp_blend_path.exists():
                        try:
                            self._temp_blend_path.unlink()
                            if self._temp_dir and self._temp_dir.exists():
                                try:
                                    self._temp_dir.rmdir()
                                except Exception:
                                    pass  # Directory may not be empty
                            print(f"[SheepIt Pack] Cleaned up temp file: {self._temp_blend_path}")
                        except Exception as e:
                            print(f"[SheepIt Pack] WARNING: Could not clean up temp file: {e}")
                    
                    self._phase = 'COMPLETE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'COMPLETE':
                    submit_settings.submit_progress = 100.0
                    submit_settings.submit_status_message = "Submission complete!"
                    
                    # Small delay to show completion
                    import time
                    time.sleep(0.2)
                    
                    self._cleanup(context, cancelled=False)
                    self.report({'INFO'}, self._message)
                    return {'FINISHED'}
                
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._error = f"Packing failed: {type(e).__name__}: {str(e)}"
                self._cleanup(context, cancelled=True)
                self.report({'ERROR'}, self._error)
                return {'CANCELLED'}
        
        return {'RUNNING_MODAL'}
    
    def _cleanup(self, context, cancelled=False):
        """Clean up progress properties and timer."""
        submit_settings = context.scene.sheepit_submit
        
        # Restore original library_abspath function if we overrode it
        if hasattr(self, '_original_library_abspath'):
            try:
                from batter import asset_usage as au
                au.library_abspath.cache_clear()
                au.library_abspath = self._original_library_abspath
                print(f"[SheepIt Pack] DEBUG: Restored original library_abspath in cleanup")
            except Exception as e:
                print(f"[SheepIt Pack] DEBUG: WARNING: Could not restore library_abspath: {e}")
        
        # Remove timer
        if hasattr(self, '_timer') and self._timer:
            context.window_manager.event_timer_remove(self._timer)
        
        # Reset progress properties
        submit_settings.is_submitting = False
        submit_settings.submit_progress = 0.0
        if cancelled and self._error:
            submit_settings.submit_status_message = self._error
        else:
            submit_settings.submit_status_message = ""
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)


def register():
    """Register operators."""
    bpy.utils.register_class(SHEEPIT_OT_pack_zip)
    bpy.utils.register_class(SHEEPIT_OT_pack_blend)


def unregister():
    """Unregister operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_pack_blend)
    bpy.utils.unregister_class(SHEEPIT_OT_pack_zip)
