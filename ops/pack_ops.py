"""
Packing operations for SheepIt Project Submitter.
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
# Use importlib to avoid sys.path manipulation and top-level module policy violations
import importlib.util
import sys
from pathlib import Path as PathLib

# Get the addon directory
_my_dir = PathLib(__file__).resolve().parent.parent

# Cache for the loaded module
_asset_usage_module = None


def _get_asset_usage_module():
    """Get the batter.asset_usage module without violating Blender extension policies.
    
    This function loads the module using importlib without modifying sys.path,
    avoiding "Policy violation with top level module" and "Policy violation with sys.path" errors.
    
    The module is registered as a private submodule of the ops package to avoid policy violations.
    """
    global _asset_usage_module
    
    if _asset_usage_module is not None:
        return _asset_usage_module
    
    # Determine the proper module name as a submodule of the current package
    # This avoids "top level module" policy violations
    if __package__:
        # Register as a private submodule of the ops package
        # e.g., "sheepit_project_submitter.ops._asset_usage" or "bl_ext.vscode_development.sheepit_project_submitter.ops._asset_usage"
        module_name = f"{__package__}._asset_usage"
    else:
        # Fallback: use a name based on the file location
        # This shouldn't happen in normal operation, but provides a fallback
        module_name = "sheepit_project_submitter.ops._asset_usage"
    
    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            _my_dir / "batter" / "asset_usage.py"
        )
        if spec and spec.loader:
            _asset_usage_module = importlib.util.module_from_spec(spec)
            # Set module metadata before execution so dataclasses can resolve __module__ correctly
            _asset_usage_module.__name__ = module_name
            _asset_usage_module.__file__ = str(_my_dir / "batter" / "asset_usage.py")
            # Set package to parent package (ops)
            if __package__:
                _asset_usage_module.__package__ = __package__
            else:
                _asset_usage_module.__package__ = "sheepit_project_submitter.ops"
            
            # Register in sys.modules BEFORE execution so classes can resolve their __module__ attribute
            # Registering as a submodule (with dots) avoids "top level module" policy violations
            sys.modules[module_name] = _asset_usage_module
            
            # Now execute the module
            spec.loader.exec_module(_asset_usage_module)
            return _asset_usage_module
    except Exception as e:
        # Clean up on error
        if module_name in sys.modules:
            del sys.modules[module_name]
        raise ImportError(f"Could not import batter.asset_usage module using importlib: {e}")
    
    raise ImportError("Could not import batter.asset_usage module using importlib")


# Load the module at import time
try:
    au = _get_asset_usage_module()
except ImportError as e:
    raise ImportError(f"Could not import batter.asset_usage module: {e}")


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


def copy_blend_caches(src_blend: Path, dst_blend: Path, missing_on_copy: list, 
                      frame_start: Optional[int] = None, frame_end: Optional[int] = None, 
                      frame_step: Optional[int] = None) -> list[Path]:
    """Copy common cache folders for a given .blend next to its target copy.

    If frame range parameters are provided, only copies cache files within that range.
    Otherwise, copies all cache files.
    On Windows we use robocopy when frame filtering; source path is kept as given (e.g. P:\)
    so mapped drives work instead of resolving to UNC.
    """
    import re
    import subprocess as _sub
    copied = []
    # Keep source path as-is on Windows so P:\ stays P:\ (resolve can turn it into UNC and break robocopy)
    if os.name != "nt":
        src_blend = src_blend.resolve()
    dst_blend = dst_blend.resolve()
    filter_by_frame = frame_start is not None and frame_end is not None and frame_step is not None
    valid_frames = None
    if filter_by_frame:
        valid_frames = set(range(frame_start, frame_end + 1, frame_step))

    def should_copy_file(file_path: Path) -> bool:
        if not filter_by_frame:
            return True
        if not file_path.is_file():
            return True
        frame_num = None
        match = re.search(r'(?:frame_|cache[^_]*_)(\d+)', file_path.stem, re.IGNORECASE)
        if match:
            frame_num = int(match.group(1))
        else:
            match = re.search(r'(\d+)$', file_path.stem)
            if match:
                frame_num = int(match.group(1))
        if frame_num is None:
            return True
        return frame_num in valid_frames

    def copy_tree_filtered(src: Path, dst: Path):
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            src_item = src / item.name
            dst_item = dst / item.name
            if src_item.is_dir():
                copy_tree_filtered(src_item, dst_item)
            elif src_item.is_file() and should_copy_file(src_item):
                shutil.copy2(src_item, dst_item)

    def _dst_has_files(p: Path) -> bool:
        """True if directory exists and contains at least one file (quick check)."""
        try:
            if not p.exists() or not p.is_dir():
                return False
            for _ in p.rglob("*"):
                return True  # at least one entry
            return False
        except Exception:
            return False

    try:
        src_parent = src_blend.parent
        dst_parent = dst_blend.parent
        blendname = src_blend.stem
        candidates = [
            (src_parent / f"blendcache_{blendname}", dst_parent / f"blendcache_{blendname}"),
        ]
        # Only add bakes if it exists in the source (avoid creating empty bakes/ in pack)
        bakes_src = src_parent / "bakes" / blendname
        if bakes_src.exists() and bakes_src.is_dir():
            candidates.append((bakes_src, dst_parent / "bakes" / blendname))
        try:
            for entry in src_parent.iterdir():
                if entry.is_dir() and entry.name.startswith("cache_"):
                    candidates.append((entry, dst_parent / entry.name))
        except Exception:
            pass

        for src_dir, dst_dir in candidates:
            if os.name == "nt":
                src_dir = src_dir  # keep as P:\ form, do not resolve to UNC
            else:
                src_dir = src_dir.resolve()
            dst_dir = dst_dir.resolve()
            try:
                # On Windows with frame filter: try Python copy first; if 0 files or PermissionError, use robocopy
                if filter_by_frame and os.name == "nt":
                    def _try_robocopy():
                        robocopy_exe = (getattr(shutil, "which", lambda x: None)("robocopy")
                            or os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "robocopy.exe")
                            or "robocopy")
                        src_str = str(src_dir)
                        dst_str = str(dst_dir)
                        print(f"[SheepIt Pack]   robocopy: {src_str} -> {dst_str}")
                        cmd = f'"{robocopy_exe}" "{src_str}" "{dst_str}" /E /R:2 /W:1 /NFL /NDL /NJH /NJS'
                        rc = _sub.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
                        print(f"[SheepIt Pack]   robocopy exit code: {rc.returncode}")
                        return rc.returncode
                    dst_dir.parent.mkdir(parents=True, exist_ok=True)
                    if dst_dir.exists():
                        try:
                            shutil.rmtree(dst_dir)
                        except Exception:
                            pass
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    used_robocopy = False
                    try:
                        src_exists = src_dir.exists()
                        src_count = "n/a"
                        if src_exists:
                            try:
                                src_count = sum(1 for _ in src_dir.rglob("*"))
                            except Exception:
                                src_count = "?"
                        print(f"[SheepIt Pack]   {src_dir.name}: exists={src_exists}, items={src_count}")
                        copy_tree_filtered(src_dir, dst_dir)
                    except PermissionError:
                        used_robocopy = True
                        rc = _try_robocopy()
                        if rc >= 8:
                            missing_on_copy.append(src_dir)
                            if dst_dir.exists() and not _dst_has_files(dst_dir):
                                try:
                                    shutil.rmtree(dst_dir)
                                except Exception:
                                    pass
                            continue
                    except Exception as e:
                        print(f"[SheepIt Pack]   WARNING: cache copy failed for {src_dir.name}: {e}")
                        used_robocopy = True
                        rc = _try_robocopy()
                        if rc >= 8 or not _dst_has_files(dst_dir):
                            missing_on_copy.append(src_dir)
                            if dst_dir.exists() and not _dst_has_files(dst_dir):
                                try:
                                    shutil.rmtree(dst_dir)
                                except Exception:
                                    pass
                            continue
                    if not _dst_has_files(dst_dir) and not used_robocopy:
                        print(f"[SheepIt Pack]   {src_dir.name}: Python copy produced 0 files, trying robocopy")
                        used_robocopy = True
                        rc = _try_robocopy()
                        if rc >= 8 or not _dst_has_files(dst_dir):
                            if dst_dir.exists() and not _dst_has_files(dst_dir):
                                try:
                                    shutil.rmtree(dst_dir)
                                except Exception:
                                    pass
                            continue
                    if _dst_has_files(dst_dir):
                        n_before = sum(1 for _ in dst_dir.rglob("*") if _.is_file())
                        if not used_robocopy:
                            truncate_caches_to_frame_range(dst_dir, frame_start, frame_end, frame_step)
                        else:
                            print(f"[SheepIt Pack]   {dst_dir.name}: keeping full cache ({n_before} files, robocopy)")
                        if _dst_has_files(dst_dir):
                            n_after = sum(1 for _ in dst_dir.rglob("*") if _.is_file())
                            if used_robocopy:
                                print(f"[SheepIt Pack]   {dst_dir.name}: {n_after} files")
                            else:
                                print(f"[SheepIt Pack]   {dst_dir.name}: {n_before} files before truncate, {n_after} after")
                            copied.append(dst_dir)
                        else:
                            print(f"[SheepIt Pack]   {dst_dir.name}: empty after truncate, skipping")
                            try:
                                shutil.rmtree(dst_dir)
                            except Exception:
                                pass
                    continue
                if not src_dir.exists() or not src_dir.is_dir():
                    continue
                # Don't pre-create dst_dir for non-Windows path; copy_tree/copytree will create it
                dst_dir.parent.mkdir(parents=True, exist_ok=True)
                if dst_dir.exists():
                    try:
                        shutil.rmtree(dst_dir)
                    except Exception:
                        pass
                dst_dir.mkdir(parents=True, exist_ok=True)
                if filter_by_frame:
                    try:
                        copy_tree_filtered(src_dir, dst_dir)
                        if _dst_has_files(dst_dir):
                            copied.append(dst_dir)
                    except PermissionError:
                        if os.name == "nt":
                            rc = _sub.run(
                                ["robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"],
                                capture_output=True, text=True,
                            )
                            if rc.returncode < 8 and _dst_has_files(dst_dir):
                                truncate_caches_to_frame_range(dst_dir, frame_start, frame_end, frame_step)
                                copied.append(dst_dir)
                        else:
                            missing_on_copy.append(src_dir)
                    except Exception as e:
                        print(f"[SheepIt Pack] WARNING: Error copying filtered cache {src_dir.name}: {e}")
                        missing_on_copy.append(src_dir)
                else:
                    try:
                        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                        copied.append(dst_dir)
                    except PermissionError:
                        if os.name == "nt":
                            rc = _sub.run(
                                ["robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"],
                                capture_output=True, text=True,
                            )
                            if rc.returncode < 8:
                                copied.append(dst_dir)
                        else:
                            missing_on_copy.append(src_dir)
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


def _run_blender_script(script: str, blend_path: Path, timeout: int = 300) -> tuple[str, str, int]:
    """Run a Python script in a Blender subprocess.
    
    Args:
        script: Python script to execute
        blend_path: Path to blend file to process
        timeout: Timeout in seconds (default 300 = 5 minutes)
    
    Returns:
        Tuple of (stdout, stderr, returncode)
    """
    import subprocess
    import time
    print(f"[SheepIt Pack] Running Blender script on: {blend_path.name}")
    print(f"[SheepIt Pack]   Full path: {blend_path}")
    print(f"[SheepIt Pack]   Timeout: {timeout}s")
    start_time = time.time()
    try:
        result = subprocess.run([
            "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
        ], capture_output=True, text=True, check=False, timeout=timeout)
        elapsed = time.time() - start_time
        print(f"[SheepIt Pack]   Script completed in {elapsed:.2f}s, return code: {result.returncode}")
        if result.stdout:
            stdout_lines = result.stdout.strip().split('\n')
            print(f"[SheepIt Pack]   stdout ({len(stdout_lines)} lines):")
            for line in stdout_lines[:10]:  # First 10 lines
                print(f"[SheepIt Pack]     {line}")
            if len(stdout_lines) > 10:
                print(f"[SheepIt Pack]     ... ({len(stdout_lines) - 10} more lines)")
        if result.stderr:
            stderr_lines = result.stderr.strip().split('\n')
            print(f"[SheepIt Pack]   stderr ({len(stderr_lines)} lines):")
            for line in stderr_lines[:10]:  # First 10 lines
                print(f"[SheepIt Pack]     {line}")
            if len(stderr_lines) > 10:
                print(f"[SheepIt Pack]     ... ({len(stderr_lines) - 10} more lines)")
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        print(f"[SheepIt Pack]   ERROR: Script timed out after {elapsed:.2f}s (timeout: {timeout}s)")
        print(f"[SheepIt Pack]   This may indicate the blend file has issues or is very large")
        return "", f"Script timed out after {timeout} seconds", -1
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[SheepIt Pack]   ERROR: Script failed after {elapsed:.2f}s: {type(e).__name__}: {str(e)}")
        return "", str(e), -1


def remap_library_paths(blend_path: Path, copy_map: dict[str, str], common_root: Path, target_path: Path, ensure_autopack: bool = True) -> list[Path]:
    """Open a blend file and remap all library paths to be relative to the copied tree."""
    import json
    import tempfile
    
    # Write copy_map to a temporary JSON file to avoid Windows command line length limits
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(copy_map, f, indent=None)
        copy_map_file = Path(f.name)
    
    try:
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
        
        # Escape backslashes in paths for the script
        copy_map_file_str = str(copy_map_file).replace('\\', '\\\\')
        common_root_str = str(common_root).replace('\\', '\\\\')
        target_path_str = str(target_path).replace('\\', '\\\\')
        
        remap_script = (
            "import bpy, json\n"
            "from pathlib import Path\n"
            f"copy_map_file = Path(r'{copy_map_file_str}')\n"
            f"with open(copy_map_file, 'r', encoding='utf-8') as f:\n"
            f"    copy_map = json.load(f)\n"
            f"common_root = Path(r'{common_root_str}')\n"
            f"target_path = Path(r'{target_path_str}')\n"
            "blend_dir = Path(bpy.data.filepath).parent\n"
        "bpy.context.preferences.filepaths.use_relative_paths = True\n"
        "remapped = 0\n"
        "unresolved = []\n"
        "print(f'Remapping library paths in: {{bpy.path.basename(bpy.data.filepath)}}')\n"
        "print(f'Found {{len(bpy.data.libraries)}} libraries')\n"
        "for lib in bpy.data.libraries:\n"
        "    src = lib.filepath\n"
        "    print(f'  Processing library: {{lib.name}}, current path: {{src}}')\n"
        "    # Convert to absolute path\n"
        "    if src.startswith('//'):\n"
        "        abs_src = (blend_dir / src[2:]).resolve()\n"
        "    else:\n"
        "        abs_src = Path(src).resolve()\n"
        "    key = str(abs_src)\n"
        "    new_abs = None\n"
        "    # Check if already in target path\n"
        "    try:\n"
        "        if abs_src.relative_to(target_path):\n"
        "            new_abs = abs_src\n"
        "            print(f'    Already in target path: {{new_abs}}')\n"
        "    except Exception:\n"
        "        pass\n"
        "    # Look up in copy_map first (most reliable)\n"
        "    if new_abs is None and key in copy_map:\n"
        "        new_abs = Path(copy_map[key])\n"
        "        print(f'    Found in copy_map: {{new_abs}}')\n"
        "    # Try relative to common_root\n"
        "    if new_abs is None:\n"
        "        try:\n"
        "            rel_to_root = abs_src.relative_to(common_root)\n"
        "            new_abs = (target_path / rel_to_root).resolve()\n"
        "            print(f'    Computed from common_root: {{new_abs}}')\n"
        "        except Exception:\n"
        "            pass\n"
        "    # If we found a new path, verify it exists and remap\n"
        "    if new_abs is not None:\n"
        "        if new_abs.exists():\n"
        "            # Set absolute path first\n"
        "            lib.filepath = str(new_abs)\n"
        "            # Then convert to relative\n"
        "            try:\n"
        "                rel_path = bpy.path.relpath(str(new_abs))\n"
        "                lib.filepath = rel_path\n"
        "                print(f'    Remapped to relative: {{rel_path}}')\n"
        "                remapped += 1\n"
        "            except Exception as e:\n"
        "                print(f'    WARNING: Could not make relative: {{e}}, keeping absolute')\n"
        "                remapped += 1\n"
        "        else:\n"
        "            print(f'    WARNING: Target file does not exist: {{new_abs}}')\n"
        "            unresolved.append(str(new_abs))\n"
        "    else:\n"
        "        print(f'    WARNING: Could not determine new path for: {{abs_src}}')\n"
        "        unresolved.append(str(abs_src))\n"
        "print(f'Remapped {{remapped}} libraries, {{len(unresolved)}} unresolved')\n"
        "if unresolved:\n"
        "    print(f'Unresolved paths: {{unresolved}}')\n"
        "# Remap image/texture paths\n"
        "images_remapped = 0\n"
        "for img in bpy.data.images:\n"
        "    if img.filepath and img.filepath not in ('', '<builtin>', '<memory>'):\n"
        "        src = img.filepath\n"
        "        # Convert to absolute path\n"
        "        if src.startswith('//'):\n"
        "            abs_src = (blend_dir / src[2:]).resolve()\n"
        "        else:\n"
        "            abs_src = Path(src).resolve()\n"
        "        key = str(abs_src)\n"
        "        new_abs = None\n"
        "        # Check if already in target path\n"
        "        try:\n"
        "            if abs_src.relative_to(target_path):\n"
        "                new_abs = abs_src\n"
        "        except Exception:\n"
        "            pass\n"
        "        # Look up in copy_map\n"
        "        if new_abs is None and key in copy_map:\n"
        "            new_abs = Path(copy_map[key])\n"
        "        # Try relative to common_root\n"
        "        if new_abs is None:\n"
        "            try:\n"
        "                rel_to_root = abs_src.relative_to(common_root)\n"
        "                new_abs = (target_path / rel_to_root).resolve()\n"
        "            except Exception:\n"
        "                pass\n"
        "        # If we found a new path and it exists, remap\n"
        "        if new_abs is not None and new_abs.exists():\n"
        "            # Set absolute path first\n"
        "            img.filepath = str(new_abs)\n"
        "            # Then convert to relative\n"
        "            try:\n"
        "                rel_path = bpy.path.relpath(str(new_abs))\n"
        "                img.filepath = rel_path\n"
        "                images_remapped += 1\n"
        "            except Exception:\n"
        "                images_remapped += 1\n"
        "print(f'Remapped {{images_remapped}} image/texture paths')\n"
        "# Save after remapping\n"
        "bpy.ops.wm.save_as_mainfile(filepath=str(Path(bpy.data.filepath)), compress=True)\n"
        "# Make all paths relative\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative(basedir=str(blend_dir))\n"
        "    print('Made all paths relative')\n"
        "except Exception as e:\n"
        "    print(f'Warning: make_paths_relative failed: {{e}}')\n"
        f"{autopack_block}"
        "# Final save\n"
        "bpy.ops.wm.save_as_mainfile(filepath=str(Path(bpy.data.filepath)), compress=True)\n"
        "print('Remapping complete')\n"
        )
        
        stdout, stderr, returncode = _run_blender_script(remap_script, blend_path)
    finally:
        # Clean up temp file
        try:
            if copy_map_file.exists():
                copy_map_file.unlink()
        except Exception:
            pass
    
    unresolved = []
    
    # Parse unresolved paths from output
    if stdout:
        import re
        for line in stdout.splitlines():
            if 'Unresolved paths:' in line or 'WARNING: Target file does not exist:' in line or 'WARNING: Could not determine new path for:' in line:
                # Try to extract path from the line
                match = re.search(r':\s*(.+)$', line)
                if match:
                    unresolved_path = match.group(1).strip()
                    try:
                        unresolved.append(Path(unresolved_path))
                    except Exception:
                        pass
    
    if returncode != 0:
        print(f"[SheepIt Pack] WARNING: remap_library_paths returned non-zero exit code: {returncode}")
        if stderr:
            print(f"[SheepIt Pack]   Error details: {stderr[:500]}")
    
    if unresolved:
        print(f"[SheepIt Pack] WARNING: {len(unresolved)} library paths could not be remapped")
        for up in unresolved[:5]:  # Show first 5
            print(f"[SheepIt Pack]   - {up}")
        if len(unresolved) > 5:
            print(f"[SheepIt Pack]   ... and {len(unresolved) - 5} more")
    
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
        "    bpy.ops.wm.save_mainfile(compress=True)\n"
        "except Exception as e:\n"
        "    print('Pack all failed:', e)\n"
    )
    
    stdout, stderr, returncode = _run_blender_script(script, blend_path)
    missing = []
    # Parse missing files from output if needed
    return missing


def pack_linked_in_blend(blend_path: Path) -> tuple[list[Path], list[Path]]:
    """Open a blend and run Pack Linked (pack libraries), then save with autopack on.
    
    Returns:
        Tuple of (missing_files: list[Path], oversized_files: list[Path])
        - missing_files: Files that don't exist and couldn't be packed
        - oversized_files: Files over 2GB that Blender can't pack
    """
    # 2GB limit (2 * 1024 * 1024 * 1024 bytes)
    MAX_PACKABLE_SIZE = 2 * 1024 * 1024 * 1024
    
    script = (
        "import bpy\n"
        "from pathlib import Path\n"
        "print('=== Pack Linked Operation ===')\n"
        "print(f'Processing: {bpy.path.basename(bpy.data.filepath)}')\n"
        "print(f'Libraries found: {len(bpy.data.libraries)}')\n"
        "missing_files = []\n"
        "oversized_files = []\n"
        "for lib in bpy.data.libraries:\n"
        "    lib_path = Path(lib.filepath)\n"
        "    if lib.filepath.startswith('//'):\n"
        "        lib_path = Path(bpy.data.filepath).parent / lib.filepath[2:]\n"
        "    if not lib_path.exists():\n"
        "        missing_files.append(str(lib_path))\n"
        "        print(f'  Library (MISSING): {lib.name}, path: {lib.filepath}')\n"
        "    else:\n"
        "        file_size = lib_path.stat().st_size\n"
        "        file_size_gb = file_size / (1024 * 1024 * 1024)\n"
        "        if file_size > " + str(MAX_PACKABLE_SIZE) + ":\n"
        "            oversized_files.append(str(lib_path))\n"
        "            print(f'  Library (OVER 2GB, cannot pack): {lib.name}, path: {lib.filepath}, size: {file_size_gb:.2f} GB')\n"
        "        else:\n"
        "            print(f'  Library (found, {file_size_gb:.2f} GB): {lib.name}, path: {lib.filepath}')\n"
        "if missing_files:\n"
        "    print(f'WARNING: {len(missing_files)} linked libraries not found and cannot be packed')\n"
        "if oversized_files:\n"
        "    print(f'WARNING: {len(oversized_files)} linked libraries are over 2GB and cannot be packed by Blender')\n"
        "try:\n"
        "    bpy.ops.file.make_paths_relative()\n"
        "    print('Made paths relative')\n"
        "except Exception as e:\n"
        "    print(f'Warning: make_paths_relative failed: {e}')\n"
        "packed_count = 0\n"
        "pack_errors = []\n"
        "try:\n"
        "    print('Starting pack_libraries()...')\n"
        "    bpy.ops.file.pack_libraries()\n"
        "    packed_count = 1\n"
        "    print('pack_libraries() completed successfully')\n"
        "except Exception as e:\n"
        "    error_msg = f'{type(e).__name__}: {str(e)}'\n"
        "    pack_errors.append(error_msg)\n"
        "    print(f'Warning: pack_libraries() failed: {error_msg}')\n"
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
        "print('Saving file...')\n"
        "bpy.ops.wm.save_mainfile(compress=True)\n"
        "print(f'=== Pack Linked Complete (packed: {packed_count}, missing: {len(missing_files)}, oversized: {len(oversized_files)}) ===')\n"
        "for mf in missing_files:\n"
        "    print(f'MISSING_FILE: {mf}')\n"
        "for of in oversized_files:\n"
        "    print(f'OVERSIZED_FILE: {of}')\n"
        "for err in pack_errors:\n"
        "    print(f'PACK_ERROR: {err}')\n"
    )
    
    stdout, stderr, returncode = _run_blender_script(script, blend_path, timeout=600)  # 10 minute timeout for pack_linked
    
    # Parse missing and oversized files from output
    missing_files = []
    oversized_files = []
    if stdout:
        for line in stdout.splitlines():
            if line.startswith('MISSING_FILE:'):
                missing_path = line.replace('MISSING_FILE:', '').strip()
                try:
                    missing_files.append(Path(missing_path))
                except Exception:
                    pass
            elif line.startswith('OVERSIZED_FILE:'):
                oversized_path = line.replace('OVERSIZED_FILE:', '').strip()
                try:
                    oversized_files.append(Path(oversized_path))
                except Exception:
                    pass
    
    # Also check for Blender's standard missing file warnings
    combined_output = (stdout or "") + "\n" + (stderr or "")
    import re
    # Pattern: "Warning, files not found: //path/to/file.blend"
    missing_patterns = [
        r"Warning, files not found:\s*(.+)",
        r"Unable to pack file, source path '([^']+)' not found",
        r"File not found:\s*(.+)",
    ]
    for pattern in missing_patterns:
        for match in re.finditer(pattern, combined_output, re.IGNORECASE):
            missing_path_str = match.group(1).strip()
            # Handle relative paths (//path)
            if missing_path_str.startswith('//'):
                try:
                    # Convert relative path to absolute
                    blend_dir = blend_path.parent
                    rel_path = missing_path_str[2:]
                    missing_path = (blend_dir / rel_path).resolve()
                except Exception:
                    missing_path = Path(missing_path_str)
            else:
                missing_path = Path(missing_path_str)
            if missing_path not in missing_files:
                missing_files.append(missing_path)
    
    # Check for 2GB size limit errors in Blender output
    size_error_patterns = [
        r"file.*too large.*2.*GB",
        r"exceeds.*2.*GB",
        r"over.*2.*GB",
        r"larger than.*2.*GB",
    ]
    for pattern in size_error_patterns:
        for match in re.finditer(pattern, combined_output, re.IGNORECASE):
            # Try to extract file path from context
            context_start = max(0, match.start() - 200)
            context_end = min(len(combined_output), match.end() + 200)
            context = combined_output[context_start:context_end]
            # Look for file paths in the context
            path_matches = re.finditer(r"['\"]([^'\"]+\.blend)['\"]", context, re.IGNORECASE)
            for path_match in path_matches:
                path_str = path_match.group(1)
                try:
                    oversized_path = Path(path_str)
                    if oversized_path.exists() and oversized_path not in oversized_files:
                        oversized_files.append(oversized_path)
                except Exception:
                    pass
    
    if missing_files:
        print(f"[SheepIt Pack]   WARNING: {len(missing_files)} linked files could not be packed (files not found):")
        for mf in missing_files[:5]:  # Show first 5
            print(f"[SheepIt Pack]     - {mf.name if mf.name else mf}")
        if len(missing_files) > 5:
            print(f"[SheepIt Pack]     ... and {len(missing_files) - 5} more")
        print(f"[SheepIt Pack]   Note: Missing linked files cannot be packed. The blend file will still be saved without these libraries.")
    
    if oversized_files:
        print(f"[SheepIt Pack]   WARNING: {len(oversized_files)} linked files could not be packed (files over 2GB):")
        for of in oversized_files[:5]:  # Show first 5
            file_size_gb = of.stat().st_size / (1024 * 1024 * 1024) if of.exists() else 0
            print(f"[SheepIt Pack]     - {of.name if of.name else of} ({file_size_gb:.2f} GB)")
        if len(oversized_files) > 5:
            print(f"[SheepIt Pack]     ... and {len(oversized_files) - 5} more")
        print(f"[SheepIt Pack]   Note: Blender cannot pack linked files over 2GB. These libraries will remain as external references.")
        print(f"[SheepIt Pack]   To fix: Reduce the size of these files or split them into smaller files.")
    
    if returncode != 0:
        print(f"[SheepIt Pack] WARNING: pack_linked_in_blend returned non-zero exit code: {returncode}")
        if stderr:
            print(f"[SheepIt Pack]   Error details: {stderr[:500]}")
    
    return missing_files, oversized_files


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
        "bpy.ops.wm.save_mainfile(compress=True)\n"
    )
    
    _run_blender_script(script, blend_path)


class IncrementalPacker:
    """Stateful incremental packer that processes files in batches across multiple timer events."""
    
    def __init__(self, workflow: str, target_path: Optional[Path], enable_nla: bool, 
                 progress_callback=None, cancel_check=None,
                 frame_start=None, frame_end=None, frame_step=None,
                 temp_blend_path: Optional[Path] = None,
                 original_blend_path: Optional[Path] = None):
        self.workflow = workflow
        self.target_path = target_path
        self.enable_nla = enable_nla
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check
        self.frame_start = frame_start  # For cache truncation
        self.frame_end = frame_end
        self.frame_step = frame_step
        self.temp_blend_path = temp_blend_path  # Temp file used as source (should be copied directly to root)
        self.original_blend_path = original_blend_path  # Original blend file path (for cache lookup)
        
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
        
        # Pack linked issues tracking
        self.oversized_files_all = []  # Collect all oversized files from pack_linked operations
        
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
            # Exclude temp file from common root calculation (it's just a source, not part of the project)
            if self.temp_blend_path:
                temp_path_resolved = self.temp_blend_path.resolve()
                self.all_filepaths = [p for p in self.all_filepaths if p.resolve() != temp_path_resolved]
                print(f"[SheepIt Pack]   Excluded temp file from common root calculation")
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
            
            # If this is a temp file, copy it directly to target root with just its filename
            # This avoids the DRIVE_C path structure issue
            is_temp_file = (self.temp_blend_path and 
                          current_blend_abspath.resolve() == self.temp_blend_path.resolve())
            
            if is_temp_file:
                # Copy temp file directly to target root
                target_path_file = self.target_path / current_blend_abspath.name
                print(f"[SheepIt Pack]   Temp file detected, copying directly to target root: {target_path_file.name}")
            else:
                try:
                    current_relpath = current_blend_abspath.relative_to(self.common_root)
                    print(f"[SheepIt Pack]   Relative path: {current_relpath}")
                except ValueError:
                    current_relpath = compute_target_relpath(current_blend_abspath, self.common_root)
                    print(f"[SheepIt Pack]   Computed relative path: {current_relpath}")
                target_path_file = self.target_path / current_relpath
            
            if current_blend_abspath not in self.copied_paths:
                print(f"[SheepIt Pack]   Copying: {current_blend_abspath} -> {target_path_file}")
                try:
                    target_path_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(current_blend_abspath, target_path_file)
                    self.copied_paths.add(current_blend_abspath)
                    if current_blend_abspath.suffix.lower() == ".blend":
                        self.copy_map[str(current_blend_abspath.resolve())] = str(target_path_file.resolve())
                    self.top_level_target_blend = target_path_file.resolve()
                    print(f"[SheepIt Pack]   Copied successfully, size: {target_path_file.stat().st_size} bytes")
                    # Copy caches - use original blend path for cache lookup if temp file
                    cache_source_blend = self.original_blend_path if (is_temp_file and self.original_blend_path) else current_blend_abspath
                    if cache_source_blend:
                        print(f"[SheepIt Pack]   Copying blend caches from: {cache_source_blend}")
                        # For COPY_ONLY workflow, filter caches during copy if frame range is specified
                        filter_during_copy = (self.copy_only_mode and 
                                             self.frame_start is not None and 
                                             self.frame_end is not None and 
                                             self.frame_step is not None)
                        if filter_during_copy:
                            print(f"[SheepIt Pack]   Filtering caches to frame range {self.frame_start}-{self.frame_end} (step: {self.frame_step}) during copy...")
                        copied_cache_dirs = copy_blend_caches(
                            cache_source_blend, target_path_file, self.missing_on_copy,
                            frame_start=self.frame_start if filter_during_copy else None,
                            frame_end=self.frame_end if filter_during_copy else None,
                            frame_step=self.frame_step if filter_during_copy else None
                        )
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
                    # Skip cache directories: already copied in copy_blend_caches from blend dir;
                    # including them here would try UNC path and fail with PermissionError.
                    name = asset_usage.abspath.name
                    if name.startswith("blendcache_") or name.startswith("cache_") or (
                        len(asset_usage.abspath.parts) >= 2 and asset_usage.abspath.parts[-2] == "bakes"
                    ):
                        continue
                    try:
                        # Try to get relative path to common root
                        asset_relpath = asset_usage.abspath.relative_to(self.common_root)
                        # Use relative path as-is (even if it's just a filename)
                        # This preserves the original relative structure
                    except ValueError:
                        # Paths are not relative to common root (different drive/UNC), 
                        # use compute_target_relpath to create DRIVE_C/UNC structure
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
                    # Add to copy_map for remapping (blend files and image/texture files)
                    if asset_usage.abspath.suffix.lower() in (".blend", ".png", ".jpg", ".jpeg", ".tga", ".tiff", ".exr", ".hdr", ".bmp", ".dds", ".mp4", ".avi", ".mov"):
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
                # Skip truncation for COPY_ONLY workflow if caches were filtered during copy
                caches_filtered_during_copy = (self.copy_only_mode and 
                                             self.frame_start is not None and 
                                             self.frame_end is not None and 
                                             self.frame_step is not None)
                if (self.frame_start is not None and self.frame_end is not None and 
                    self.frame_step is not None and self.cache_dirs and 
                    not caches_filtered_during_copy):
                    self.cache_truncate_index = 0
                    self.phase = 'TRUNCATING_CACHES'
                    return ('TRUNCATING_CACHES', False)
                else:
                    if caches_filtered_during_copy:
                        print(f"[SheepIt Pack] Caches were filtered during copy, skipping truncation phase")
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
            
            # Add top-level blend (use the copied target path, not the original)
            if self.top_level_target_blend and self.top_level_target_blend.exists():
                self.to_remap.append(self.top_level_target_blend)
                print(f"[SheepIt Pack]   Added top-level blend to remap list: {self.top_level_target_blend.name}")
            
            # Add all dependent blend files
            for lib in self.blend_deps.keys():
                abs_path = au.library_abspath(lib)
                if abs_path.suffix.lower() != ".blend":
                    continue
                try:
                    rel = abs_path.relative_to(self.common_root)
                except ValueError:
                    rel = compute_target_relpath(abs_path, self.common_root)
                target_blend = self.target_path / rel
                if target_blend.exists():
                    self.to_remap.append(target_blend)
                    print(f"[SheepIt Pack]   Added dependent blend to remap list: {target_blend.name}")
                else:
                    print(f"[SheepIt Pack]   WARNING: Dependent blend not found at target: {target_blend}")
            
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
                    unresolved = remap_library_paths(
                        blend_to_fix,
                        self.copy_map,
                        self.common_root,
                        self.target_path,
                        ensure_autopack=self.autopack_on_save,
                    )
                    if unresolved:
                        print(f"[SheepIt Pack]     WARNING: {len(unresolved)} paths could not be remapped in {blend_to_fix.name}")
                        for up in unresolved[:3]:  # Show first 3
                            print(f"[SheepIt Pack]       - {up}")
                        if len(unresolved) > 3:
                            print(f"[SheepIt Pack]       ... and {len(unresolved) - 3} more")
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
                    print(f"[SheepIt Pack]   Starting pack_linked operation (this may take a while for large files)...")
                    try:
                        missing_files, oversized_files = pack_linked_in_blend(blend_to_fix)
                        # Track oversized files for user reporting
                        if oversized_files:
                            self.oversized_files_all.extend(oversized_files)
                        issues = []
                        if missing_files:
                            issues.append(f"{len(missing_files)} missing")
                        if oversized_files:
                            issues.append(f"{len(oversized_files)} over 2GB")
                        if issues:
                            print(f"[SheepIt Pack]   Completed pack_linked for: {blend_to_fix.name} (with {', '.join(issues)} linked files that couldn't be packed)")
                        else:
                            print(f"[SheepIt Pack]   Completed pack_linked for: {blend_to_fix.name}")
                    except Exception as e:
                        print(f"[SheepIt Pack]   ERROR during pack_linked: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        # Continue with next file rather than failing completely
                else:
                    print(f"[SheepIt Pack]   WARNING: Blend file does not exist: {blend_to_fix}")
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
            # Legacy function doesn't support frame range filtering - copy all caches
            cache_count = copy_blend_caches(current_blend_abspath, target_path_file, missing_on_copy,
                                          frame_start=None, frame_end=None, frame_step=None)
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
                # Add to copy_map for remapping (blend files and image/texture files)
                if asset_usage.abspath.suffix.lower() in (".blend", ".png", ".jpg", ".jpeg", ".tga", ".tiff", ".exr", ".hdr", ".bmp", ".dds", ".mp4", ".avi", ".mov"):
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
                    missing_files, oversized_files = pack_linked_in_blend(blend_to_fix)
                    issues = []
                    if missing_files:
                        issues.append(f"{len(missing_files)} missing")
                    if oversized_files:
                        issues.append(f"{len(oversized_files)} over 2GB")
                    if issues:
                        print(f"[SheepIt Pack]     Note: {', '.join(issues)} linked files could not be packed")
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
    """Pack project as ZIP (for scenes with caches) - creates ZIP and saves to output location."""
    bl_idname = "sheepit.pack_zip"
    bl_label = "Pack as ZIP"
    bl_description = "Copy assets without packing (for scenes with caches), create ZIP, and save to output location"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        """Start the packing operation."""
        submit_settings = context.scene.sheepit_submit
        
        # Check if already packing
        if submit_settings.is_submitting:
            self.report({'WARNING'}, "A packing operation is already in progress.")
            return {'CANCELLED'}
        
        # Get output path from settings or preferences
        output_dir = submit_settings.output_path
        if not output_dir:
            from ..utils.compat import get_addon_prefs
            prefs = get_addon_prefs()
            if prefs and prefs.default_output_path:
                output_dir = prefs.default_output_path
                submit_settings.output_path = output_dir
        
        if not output_dir:
            self.report({'ERROR'}, "Please specify an output path in the panel below.")
            return {'CANCELLED'}
        
        # Generate filename (will be set after ZIP creation with pack indicator)
        blend_name = bpy.data.filepath if bpy.data.filepath else "untitled"
        if blend_name:
            blend_name = Path(blend_name).stem
        else:
            blend_name = "untitled"
        # Output path will be set after ZIP creation with pack indicator
        self._output_dir = Path(output_dir)
        
        # Initialize progress properties
        submit_settings.is_submitting = True
        submit_settings.submit_progress = 0.0
        submit_settings.submit_status_message = "Initializing..."
        
        # Initialize phase tracking
        self._phase = 'INIT'
        self._output_path = None  # Will be set after ZIP creation
        self._original_filepath = bpy.data.filepath
        self._temp_blend_path = None
        self._temp_dir = None
        self._target_path = None
        self._zip_path = None
        self._frame_start = None
        self._frame_end = None
        self._frame_step = None
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
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)
    
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
            self.report({'INFO'}, "Packing cancelled.")
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
                    au = _get_asset_usage_module()
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
                        enable_nla=False,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                        frame_start=self._frame_start,
                        frame_end=self._frame_end,
                        frame_step=self._frame_step,
                        temp_blend_path=self._temp_blend_path,
                        original_blend_path=Path(self._original_filepath) if self._original_filepath else None
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
                            
                            # Check for oversized files that couldn't be packed
                            if self._packer.oversized_files_all:
                                oversized_list = "\n".join(f"  - {f.name if f.name else f} ({f.stat().st_size / (1024**3):.2f} GB)" 
                                                          for f in self._packer.oversized_files_all[:10])
                                if len(self._packer.oversized_files_all) > 10:
                                    oversized_list += f"\n  ... and {len(self._packer.oversized_files_all) - 10} more"
                                warning_msg = (
                                    f"Warning: {len(self._packer.oversized_files_all)} linked file(s) over 2GB could not be packed:\n"
                                    f"{oversized_list}\n\n"
                                    "Blender cannot pack linked files over 2GB. These files will remain as external references.\n"
                                    "To fix: Reduce the size of these files or split them into smaller files."
                                )
                                print(f"[SheepIt Pack] {warning_msg}")
                                # Report as warning (non-blocking)
                                self.report({'WARNING'}, f"{len(self._packer.oversized_files_all)} linked file(s) over 2GB could not be packed")
                            
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
                        au = _get_asset_usage_module()
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
                        exclude_video = getattr(context.scene.sheepit_submit, 'exclude_video_from_zip', False)
                        create_zip_from_directory(
                            self._target_path,
                            self._zip_path,
                            progress_callback=zip_progress_callback,
                            cancel_check=zip_cancel_check,
                            exclude_video=exclude_video,
                        )
                        
                        # Rename ZIP to use blend file name, with suffix only if there's a conflict
                        # Extract blend file name
                        if self._original_filepath:
                            blend_name = Path(self._original_filepath).stem
                        elif self._temp_blend_path:
                            blend_name = self._temp_blend_path.stem
                        else:
                            blend_name = "untitled"
                        
                        # Check if the desired ZIP name already exists in output directory
                        desired_zip_name = f"{blend_name}.zip"
                        desired_zip_path = self._output_dir / desired_zip_name
                        
                        if desired_zip_path.exists():
                            # File conflict - add suffix with pack indicator
                            # Extract pack indicator from temp directory name (e.g., "0t2v99gf" from "sheepit_pack_0t2v99gf")
                            pack_indicator = self._target_path.name
                            if pack_indicator.startswith("sheepit_pack_"):
                                pack_indicator = pack_indicator[len("sheepit_pack_"):]
                            
                            # Create new ZIP name with suffix: {blend_name}_{pack_indicator}.zip
                            new_zip_name = f"{blend_name}_{pack_indicator}.zip"
                        else:
                            # No conflict - use simple name
                            new_zip_name = desired_zip_name
                        
                        new_zip_path = self._zip_path.parent / new_zip_name
                        
                        # Rename the ZIP file
                        if self._zip_path.exists():
                            self._zip_path.rename(new_zip_path)
                            self._zip_path = new_zip_path
                            print(f"[SheepIt Pack] Renamed ZIP to: {new_zip_name}")
                        
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
                    
                    self._phase = 'SAVING_FILE'
                    print(f"[SheepIt Pack] DEBUG: Transitioning to SAVING_FILE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_FILE':
                    submit_settings.submit_progress = 85.0
                    submit_settings.submit_status_message = "Saving ZIP to output location..."
                    
                    try:
                        # Ensure output directory exists
                        self._output_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Move ZIP file to output location (use the renamed ZIP path)
                        final_zip_path = self._output_dir / self._zip_path.name
                        import shutil
                        shutil.move(str(self._zip_path), str(final_zip_path))
                        self._zip_path = final_zip_path
                        self._output_path = final_zip_path
                        
                        print(f"[SheepIt Pack] Saved ZIP file to: {self._output_path}")
                        self._success = True
                        self._message = f"ZIP file saved to: {self._output_path}"
                        
                        submit_settings.submit_progress = 95.0
                        submit_settings.submit_status_message = "File saved successfully!"
                        self._phase = 'CLEANUP'
                    except Exception as e:
                        self._error = f"Failed to save file: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
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
                    submit_settings.submit_status_message = "Packing complete!"
                    
                    # Small delay to show completion
                    import time
                    time.sleep(0.2)
                    
                    self._cleanup(context, cancelled=False)
                    self.report({'INFO'}, f"ZIP file saved to: {self._output_path}")
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
                au = _get_asset_usage_module()
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
    """Pack project and save (pack all assets into blend files) - saves blend to output location."""
    bl_idname = "sheepit.pack_blend"
    bl_label = "Pack as Blend"
    bl_description = "Pack all assets into blend files and save to output location"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        """Start the packing operation."""
        submit_settings = context.scene.sheepit_submit
        
        # Check if already packing
        if submit_settings.is_submitting:
            self.report({'WARNING'}, "A packing operation is already in progress.")
            return {'CANCELLED'}
        
        # Get output path from settings or preferences
        output_dir = submit_settings.output_path
        if not output_dir:
            from ..utils.compat import get_addon_prefs
            prefs = get_addon_prefs()
            if prefs and prefs.default_output_path:
                output_dir = prefs.default_output_path
                submit_settings.output_path = output_dir
        
        if not output_dir:
            self.report({'ERROR'}, "Please specify an output path in the panel below.")
            return {'CANCELLED'}
        
        # Generate filename
        blend_name = bpy.data.filepath if bpy.data.filepath else "untitled"
        if blend_name:
            blend_name = Path(blend_name).stem
        else:
            blend_name = "untitled"
        output_file = Path(output_dir) / f"{blend_name}.blend"
        
        # Initialize progress properties
        submit_settings.is_submitting = True
        submit_settings.submit_progress = 0.0
        submit_settings.submit_status_message = "Initializing..."
        
        # Initialize phase tracking
        self._phase = 'INIT'
        self._output_path = output_file
        self._original_filepath = bpy.data.filepath
        self._temp_blend_path = None
        self._temp_dir = None
        self._target_path = None
        self._blend_path = None
        self._frame_start = None
        self._frame_end = None
        self._frame_step = None
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
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)
    
    def modal(self, context, event):
        """Handle modal events and update progress."""
        submit_settings = context.scene.sheepit_submit
        
        # Handle ESC key to cancel
        if event.type == 'ESC':
            self._cleanup(context, cancelled=True)
            self.report({'INFO'}, "Packing cancelled.")
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
                    au = _get_asset_usage_module()
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
                        enable_nla=False,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                        frame_start=self._frame_start,
                        frame_end=self._frame_end,
                        frame_step=self._frame_step,
                        temp_blend_path=self._temp_blend_path,
                        original_blend_path=Path(self._original_filepath) if self._original_filepath else None
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
                            
                            # Check for oversized files that couldn't be packed
                            if self._packer.oversized_files_all:
                                oversized_list = "\n".join(f"  - {f.name if f.name else f} ({f.stat().st_size / (1024**3):.2f} GB)" 
                                                          for f in self._packer.oversized_files_all[:10])
                                if len(self._packer.oversized_files_all) > 10:
                                    oversized_list += f"\n  ... and {len(self._packer.oversized_files_all) - 10} more"
                                warning_msg = (
                                    f"Warning: {len(self._packer.oversized_files_all)} linked file(s) over 2GB could not be packed:\n"
                                    f"{oversized_list}\n\n"
                                    "Blender cannot pack linked files over 2GB. These files will remain as external references.\n"
                                    "To fix: Reduce the size of these files or split them into smaller files."
                                )
                                print(f"[SheepIt Pack] {warning_msg}")
                                # Report as warning (non-blocking)
                                self.report({'WARNING'}, f"{len(self._packer.oversized_files_all)} linked file(s) over 2GB could not be packed")
                            
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
                        au = _get_asset_usage_module()
                        au.library_abspath.cache_clear()
                        au.library_abspath = self._original_library_abspath
                        print(f"[SheepIt Pack] DEBUG: Restored original library_abspath function")
                    
                    self._phase = 'VALIDATING_FILE_SIZE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_FILE_SIZE':
                    submit_settings.submit_progress = 72.5
                    submit_settings.submit_status_message = "Validating file size..."
                    
                    # Check blend file size
                    if self._blend_path and self._blend_path.exists():
                        blend_size = self._blend_path.stat().st_size
                        blend_size_gb = blend_size / (1024 * 1024 * 1024)
                        MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
                        
                        print(f"[SheepIt Pack] Blend file size: {blend_size_gb:.2f} GB")
                        
                        if blend_size > MAX_FILE_SIZE:
                            error_msg = (
                                f"Blend file size ({blend_size_gb:.2f} GB) exceeds 2GB limit.\n\n"
                                "To reduce file size, consider:\n"
                                "- Optimizing the scene (reduce geometry, simplify materials)\n"
                                "- Optimizing asset files (compress textures, reduce resolution)\n"
                                "- Splitting the frame range (render in smaller chunks)"
                            )
                            print(f"[SheepIt Pack] ERROR: {error_msg}")
                            self._error = error_msg
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    
                    self._phase = 'SAVING_FILE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_FILE':
                    submit_settings.submit_progress = 75.0
                    submit_settings.submit_status_message = "Saving blend file to output location..."
                    
                    try:
                        # Ensure output directory exists
                        self._output_path.parent.mkdir(parents=True, exist_ok=True)
                        
                        # Copy blend file to output location
                        import shutil
                        shutil.copy2(self._blend_path, self._output_path)
                        
                        print(f"[SheepIt Pack] Saved blend file to: {self._output_path}")
                        self._success = True
                        self._message = f"Blend file saved to: {self._output_path}"
                        
                        submit_settings.submit_progress = 90.0
                        submit_settings.submit_status_message = "File saved successfully!"
                        self._phase = 'CLEANUP'
                    except Exception as e:
                        self._error = f"Failed to save file: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
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
                    submit_settings.submit_status_message = "Packing complete!"
                    
                    # Small delay to show completion
                    import time
                    time.sleep(0.2)
                    
                    self._cleanup(context, cancelled=False)
                    self.report({'INFO'}, f"ZIP file saved to: {self._output_path}")
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
                au = _get_asset_usage_module()
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


class SHEEPIT_OT_enable_nla(Operator):
    """Enable NLA only on objects/rigs that have Animation Layers turned on (Animation Layers addon)."""
    bl_idname = "sheepit.enable_nla"
    bl_label = "Enable NLA"
    bl_description = "Disable animation layers, remove current action, and enable NLA on objects that have Animation Layers on"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        """Execute the NLA enable operation only on objects with Animation Layers on."""
        objects_processed = 0
        animation_layers_disabled = 0
        actions_removed = 0
        nla_enabled = 0
        
        for obj in bpy.data.objects:
            ad = getattr(obj, 'animation_data', None)
            if not ad:
                continue
            anim_layers = getattr(obj, 'AnimLayersSettings', None) or getattr(obj, 'als', None)
            turn_on = getattr(anim_layers, 'turn_on', None) if anim_layers else None
            if anim_layers is None or turn_on is not True:
                continue
            
            try:
                anim_layers.turn_on = False
                animation_layers_disabled += 1
                if ad.action is not None:
                    ad.action = None
                    actions_removed += 1
                if hasattr(ad, 'use_nla') and not ad.use_nla:
                    ad.use_nla = True
                    nla_enabled += 1
                objects_processed += 1
            except Exception as e:
                print(f"[SheepIt NLA] Warning: Could not process object '{obj.name}': {e}")
        
        if objects_processed > 0:
            self.report({'INFO'},
                f"Processed {objects_processed} objects (Animation Layers on): "
                f"{animation_layers_disabled} disabled, {actions_removed} actions removed, {nla_enabled} NLA enabled")
        else:
            self.report({'INFO'}, "No objects with Animation Layers on found.")
        
        return {'FINISHED'}


class SHEEPIT_OT_pack_zip_sync(Operator):
    """Pack project as ZIP synchronously (for scripting/MCP). Same as Pack as ZIP but runs to completion in one call."""
    bl_idname = "sheepit.pack_zip_sync"
    bl_label = "Pack as ZIP (Sync)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from .submit_ops import (
            save_current_blend_with_frame_range,
            apply_frame_range_to_blend,
            create_zip_from_directory,
        )
        try:
            from ..utils.compat import get_addon_prefs
        except Exception:
            get_addon_prefs = lambda: None
        submit_settings = context.scene.sheepit_submit
        output_dir = submit_settings.output_path
        if not output_dir:
            prefs = get_addon_prefs()
            if prefs and prefs.default_output_path:
                output_dir = prefs.default_output_path
        if not output_dir:
            self.report({'ERROR'}, "Please specify an output path.")
            return {'CANCELLED'}
        submit_settings.is_submitting = True
        output_dir = Path(output_dir)
        original_filepath = bpy.data.filepath
        blend_name = Path(original_filepath).stem if original_filepath else "untitled"
        try:
            temp_blend_path, frame_start, frame_end, frame_step = save_current_blend_with_frame_range(submit_settings)
        except Exception as e:
            submit_settings.is_submitting = False
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        au = _get_asset_usage_module()
        import functools
        _orig_lib_abspath = au.library_abspath
        temp_file_path = temp_blend_path.resolve()
        def _override(lib):
            return temp_file_path if lib is None else _orig_lib_abspath(lib)
        au.library_abspath.cache_clear()
        au.library_abspath = functools.lru_cache(maxsize=None)(_override)
        def _progress(pct, msg):
            submit_settings.submit_progress = 15.0 + (pct * 0.46)
            submit_settings.submit_status_message = msg
        packer = IncrementalPacker(
            WorkflowMode.COPY_ONLY,
            target_path=None,
            enable_nla=False,
            progress_callback=_progress,
            cancel_check=lambda: False,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            temp_blend_path=temp_blend_path,
            original_blend_path=Path(original_filepath) if original_filepath else None,
        )
        try:
            while True:
                next_phase, is_complete = packer.process_batch(batch_size=20)
                if is_complete:
                    break
            target_path = packer.target_path
        except Exception as e:
            au.library_abspath.cache_clear()
            au.library_abspath = _orig_lib_abspath
            submit_settings.is_submitting = False
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        for blend_file in target_path.rglob("*.blend"):
            if blend_file.exists():
                apply_frame_range_to_blend(blend_file, frame_start, frame_end, frame_step)
        au.library_abspath.cache_clear()
        au.library_abspath = _orig_lib_abspath
        zip_path = target_path.parent / f"{target_path.name}.zip"
        exclude_video = getattr(submit_settings, 'exclude_video_from_zip', False)
        create_zip_from_directory(target_path, zip_path, cancel_check=lambda: False, exclude_video=exclude_video)
        desired_zip_name = f"{blend_name}.zip"
        desired_zip_path = output_dir / desired_zip_name
        pack_indicator = target_path.name
        if pack_indicator.startswith("sheepit_pack_"):
            pack_indicator = pack_indicator[len("sheepit_pack_"):]
        new_zip_name = f"{blend_name}_{pack_indicator}.zip" if desired_zip_path.exists() else desired_zip_name
        final_zip_path = output_dir / new_zip_name
        output_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(zip_path), str(final_zip_path))
        if temp_blend_path.exists():
            try:
                temp_blend_path.unlink()
            except Exception:
                pass
        submit_settings.is_submitting = False
        submit_settings.submit_progress = 100.0
        submit_settings.submit_status_message = ""
        self.report({'INFO'}, f"ZIP saved to: {final_zip_path}")
        return {'FINISHED'}


def register():
    """Register operators."""
    bpy.utils.register_class(SHEEPIT_OT_pack_zip)
    bpy.utils.register_class(SHEEPIT_OT_pack_zip_sync)
    bpy.utils.register_class(SHEEPIT_OT_pack_blend)
    bpy.utils.register_class(SHEEPIT_OT_enable_nla)


def unregister():
    """Unregister operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_enable_nla)
    bpy.utils.unregister_class(SHEEPIT_OT_pack_blend)
    bpy.utils.unregister_class(SHEEPIT_OT_pack_zip_sync)
    bpy.utils.unregister_class(SHEEPIT_OT_pack_zip)
