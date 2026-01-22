"""
Submission operations for SheepIt render farm.
"""

import os
import zipfile
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import bpy
from bpy.types import Operator

from .. import config


def apply_frame_range_to_blend(blend_path: Path, frame_start: int, frame_end: int, frame_step: int) -> None:
    """
    Apply frame range settings to a blend file using subprocess.
    
    Args:
        blend_path: Path to the blend file to modify
        frame_start: Start frame value
        frame_end: End frame value
        frame_step: Frame step value
    """
    script = f"""
import bpy
for scene in bpy.data.scenes:
    scene.frame_start = {frame_start}
    scene.frame_end = {frame_end}
    scene.frame_step = {frame_step}
bpy.ops.wm.save_mainfile()
print(f'Applied frame range {frame_start}-{frame_end} (step {frame_step}) to all scenes')
"""
    
    result = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)
    
    if result.returncode != 0:
        print(f"[SheepIt Submit] WARNING: Failed to apply frame range to {blend_path.name}")
        if result.stderr:
            print(f"[SheepIt Submit]   Error: {result.stderr[:200]}")
    else:
        print(f"[SheepIt Submit] Applied frame range {frame_start}-{frame_end} (step {frame_step}) to {blend_path.name}")


def save_current_blend_with_frame_range(submit_settings, temp_dir: Optional[Path] = None) -> Tuple[Path, int, int, int]:
    """
    Save current blend state to a temporary file and apply frame range from submit_settings.
    
    Args:
        submit_settings: Submit settings containing frame range configuration
        temp_dir: Optional temporary directory (if None, creates a new one)
    
    Returns:
        Tuple of (temp_blend_path, frame_start, frame_end, frame_step)
    """
    # Determine frame range from submit_settings
    if submit_settings.frame_range_mode == 'FULL':
        frame_start = bpy.context.scene.frame_start
        frame_end = bpy.context.scene.frame_end
        frame_step = bpy.context.scene.frame_step
    else:
        frame_start = submit_settings.frame_start
        frame_end = submit_settings.frame_end
        frame_step = submit_settings.frame_step
    
    # Create temp directory if not provided
    if temp_dir is None:
        temp_dir = Path(tempfile.mkdtemp(prefix="sheepit_submit_"))
    
    # Generate temp blend filename
    blend_name = bpy.data.filepath if bpy.data.filepath else "untitled"
    blend_name = Path(blend_name).stem if blend_name else "untitled"
    temp_blend = temp_dir / f"{blend_name}.blend"
    
    print(f"[SheepIt Submit] Saving current blend state to: {temp_blend}")
    print(f"[SheepIt Submit] Frame range: {frame_start} - {frame_end} (step: {frame_step})")
    
    # Save current blend state
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(temp_blend), copy=True)
        print(f"[SheepIt Submit] Saved current blend state to temp file")
    except Exception as e:
        error_msg = f"Failed to save current blend state: {type(e).__name__}: {str(e)}"
        print(f"[SheepIt Submit] ERROR: {error_msg}")
        raise RuntimeError(error_msg) from e
    
    # Apply frame range to the saved file
    apply_frame_range_to_blend(temp_blend, frame_start, frame_end, frame_step)
    
    return temp_blend, frame_start, frame_end, frame_step


class SHEEPIT_OT_submit_current(Operator):
    """Submit current blend file to SheepIt without packing."""
    bl_idname = "sheepit.submit_current"
    bl_label = "Submit Current Blend"
    bl_description = "Submit the current blend file to SheepIt without packing (for already-packed files or test submissions)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        print(f"[SheepIt Submit] Starting submission of current blend file...")
        
        submit_settings = context.scene.sheepit_submit
        
        # Save current blend state to temp file with frame range applied
        try:
            temp_blend_path, frame_start, frame_end, frame_step = save_current_blend_with_frame_range(submit_settings)
            print(f"[SheepIt Submit] Using temp blend file: {temp_blend_path}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to save current blend state: {str(e)}")
            return {'CANCELLED'}
        
        # Get preferences for authentication
        from ..utils.compat import get_addon_prefs
        from ..utils.auth import load_auth_cookies
        from .api_submit import submit_file_to_sheepit
        
        sheepit_prefs = get_addon_prefs()
        if not sheepit_prefs:
            self.report({'ERROR'}, "Addon preferences not found. Please configure SheepIt credentials in preferences.")
            # Clean up temp file
            try:
                temp_blend_path.unlink()
                temp_blend_path.parent.rmdir()
            except Exception:
                pass
            return {'CANCELLED'}
        
        # Get authentication
        auth_cookies = None
        username = None
        password = None
        
        if sheepit_prefs.use_browser_login:
            auth_cookies = load_auth_cookies()
            if not auth_cookies:
                self.report({'ERROR'}, "No browser login session found. Please login via browser in preferences.")
                # Clean up temp file
                try:
                    temp_blend_path.unlink()
                    temp_blend_path.parent.rmdir()
                except Exception:
                    pass
                return {'CANCELLED'}
        else:
            if not sheepit_prefs.sheepit_username or not sheepit_prefs.sheepit_password:
                self.report({'ERROR'}, "Please configure SheepIt username and password in preferences.")
                # Clean up temp file
                try:
                    temp_blend_path.unlink()
                    temp_blend_path.parent.rmdir()
                except Exception:
                    pass
                return {'CANCELLED'}
            username = sheepit_prefs.sheepit_username
            password = sheepit_prefs.sheepit_password
        
        # Submit temp file
        success, message = submit_file_to_sheepit(
            temp_blend_path,
            submit_settings,
            auth_cookies=auth_cookies,
            username=username,
            password=password
        )
        
        # Clean up temp file on success (leave for debugging on failure)
        if success:
            try:
                temp_blend_path.unlink()
                temp_blend_path.parent.rmdir()
                print(f"[SheepIt Submit] Cleaned up temp file: {temp_blend_path}")
            except Exception as e:
                print(f"[SheepIt Submit] WARNING: Could not clean up temp file: {e}")
            self.report({'INFO'}, message)
            return {'FINISHED'}
        else:
            # Leave temp file for debugging on failure
            print(f"[SheepIt Submit] Temp file left for debugging: {temp_blend_path}")
            self.report({'ERROR'}, message)
            return {'CANCELLED'}


def create_zip_from_directory(directory: Path, output_zip: Path) -> None:
    """Create a ZIP file from a directory."""
    import time
    
    print(f"[SheepIt Submit] Starting ZIP creation...")
    print(f"[SheepIt Submit]   Directory: {directory}")
    print(f"[SheepIt Submit]   Output: {output_zip}")
    
    # Count files first
    file_count = 0
    total_size = 0
    for root, dirs, files in os.walk(directory):
        for file in files:
            file_path = Path(root) / file
            if file_path.exists():
                file_count += 1
                total_size += file_path.stat().st_size
    
    print(f"[SheepIt Submit]   Found {file_count} files, total size: {total_size / (1024*1024):.2f} MB")
    print(f"[SheepIt Submit]   Creating ZIP (this may take a while)...")
    
    start_time = time.time()
    files_added = 0
    
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(directory):
            for file in files:
                file_path = Path(root) / file
                if not file_path.exists():
                    continue
                
                arcname = file_path.relative_to(directory)
                try:
                    zipf.write(file_path, arcname)
                    files_added += 1
                    
                    # Progress updates
                    if files_added == 1:
                        print(f"[SheepIt Submit]   Adding files to ZIP...")
                    elif files_added % 100 == 0:
                        elapsed = time.time() - start_time
                        rate = files_added / elapsed if elapsed > 0 else 0
                        print(f"[SheepIt Submit]   Progress: {files_added}/{file_count} files ({files_added*100//file_count}%), {rate:.1f} files/sec")
                except Exception as e:
                    print(f"[SheepIt Submit]   WARNING: Failed to add {arcname}: {type(e).__name__}: {str(e)}")
    
    elapsed = time.time() - start_time
    print(f"[SheepIt Submit] ZIP creation completed!")
    print(f"[SheepIt Submit]   Files added: {files_added}/{file_count}")
    print(f"[SheepIt Submit]   Time taken: {elapsed:.2f} seconds")
    if elapsed > 0:
        print(f"[SheepIt Submit]   Average rate: {files_added/elapsed:.1f} files/sec")


def register():
    """Register operators."""
    bpy.utils.register_class(SHEEPIT_OT_submit_current)


def unregister():
    """Unregister operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_submit_current)
