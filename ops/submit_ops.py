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
bpy.ops.wm.save_mainfile(compress=True)
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
        bpy.ops.wm.save_as_mainfile(filepath=str(temp_blend), copy=True, compress=True)
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
        self._temp_blend_path = None
        self._temp_dir = None
        self._auth_cookies = None
        self._username = None
        self._password = None
        self._success = False
        self._message = ""
        self._error = None
        
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
                    submit_settings.submit_progress = 10.0
                    submit_settings.submit_status_message = "Saving current blend state..."
                    
                    # Save current blend state to temp file with frame range applied
                    try:
                        self._temp_blend_path, frame_start, frame_end, frame_step = save_current_blend_with_frame_range(submit_settings)
                        self._temp_dir = self._temp_blend_path.parent
                        print(f"[SheepIt Submit] Using temp blend file: {self._temp_blend_path}")
                    except Exception as e:
                        self._error = f"Failed to save current blend state: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'APPLYING_FRAME_RANGE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE':
                    submit_settings.submit_progress = 20.0
                    submit_settings.submit_status_message = "Frame range applied."
                    # Frame range is already applied in save_current_blend_with_frame_range
                    self._phase = 'AUTHENTICATING'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'AUTHENTICATING':
                    submit_settings.submit_progress = 25.0
                    submit_settings.submit_status_message = "Authenticating..."
                    
                    # Get preferences for authentication
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
                    
                    self._phase = 'VALIDATING_FILE_SIZE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_FILE_SIZE':
                    submit_settings.submit_progress = 27.0
                    submit_settings.submit_status_message = "Validating file size before upload..."
                    
                    # Check blend file size
                    if self._temp_blend_path and self._temp_blend_path.exists():
                        blend_size = self._temp_blend_path.stat().st_size
                        blend_size_gb = blend_size / (1024 * 1024 * 1024)
                        MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
                        
                        print(f"[SheepIt Submit] Blend file size: {blend_size_gb:.2f} GB")
                        
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
                            print(f"[SheepIt Submit] ERROR: {error_msg}")
                            self._error = error_msg
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    
                    self._phase = 'UPLOADING'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'UPLOADING_INIT':
                    # Set status and force UI update before blocking upload
                    submit_settings.submit_progress = 30.0
                    submit_settings.submit_status_message = "Uploading to SheepIt..."
                    
                    # Force UI redraw to show the status update
                    for area in context.screen.areas:
                        if area.type == 'PROPERTIES':
                            area.tag_redraw()
                    
                    # Process events to allow UI to update
                    bpy.app.timers.register(lambda: None)
                    
                    print(f"[SheepIt Submit] DEBUG: Status set to 'Uploading to SheepIt...', transitioning to UPLOADING phase")
                    self._phase = 'UPLOADING'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'UPLOADING':
                    # Now do the actual blocking upload
                    from .api_submit import submit_file_to_sheepit
                    
                    print(f"[SheepIt Submit] DEBUG: Starting blocking upload operation...")
                    self._success, self._message = submit_file_to_sheepit(
                        self._temp_blend_path,
                        submit_settings,
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
                    submit_settings.submit_progress = 95.0
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
                            print(f"[SheepIt Submit] Cleaned up temp file: {self._temp_blend_path}")
                        except Exception as e:
                            print(f"[SheepIt Submit] WARNING: Could not clean up temp file: {e}")
                    
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
                self._error = f"Submission failed: {type(e).__name__}: {str(e)}"
                self._cleanup(context, cancelled=True)
                self.report({'ERROR'}, self._error)
                return {'CANCELLED'}
        
        return {'RUNNING_MODAL'}
    
    def _cleanup(self, context, cancelled=False):
        """Clean up progress properties and timer."""
        submit_settings = context.scene.sheepit_submit
        
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


def create_zip_from_directory(directory: Path, output_zip: Path, progress_callback=None, cancel_check=None) -> None:
    """Create a ZIP file from a directory.
    
    Args:
        directory: Directory to zip
        output_zip: Output ZIP file path
        progress_callback: Optional callback(progress_pct, message) for progress updates
        cancel_check: Optional callback() -> bool to check for cancellation
    """
    import time
    
    print(f"[SheepIt Submit] Starting ZIP creation...")
    print(f"[SheepIt Submit]   Directory: {directory}")
    print(f"[SheepIt Submit]   Output: {output_zip}")
    
    # Delete .blend1 through .blend32 backup files before zipping
    if progress_callback:
        progress_callback(0.0, "Removing backup files...")
    
    backup_files = []
    for i in range(1, 33):  # blend1 through blend32
        pattern = f"*.blend{i}"
        backup_files.extend(directory.rglob(pattern))
    
    if backup_files:
        print(f"[SheepIt Submit]   Found {len(backup_files)} backup files (.blend1-.blend32), deleting...")
        deleted_count = 0
        for backup_file in backup_files:
            try:
                backup_file.unlink()
                deleted_count += 1
            except Exception as e:
                print(f"[SheepIt Submit]   WARNING: Could not delete {backup_file.name}: {e}")
        print(f"[SheepIt Submit]   Deleted {deleted_count}/{len(backup_files)} backup files")
    else:
        print(f"[SheepIt Submit]   No backup files (.blend1-.blend32) found")
    
    if progress_callback:
        progress_callback(0.0, "Counting files...")
    
    # Count files first
    file_count = 0
    total_size = 0
    file_list = []  # Store file list for progress tracking
    for root, dirs, files in os.walk(directory):
        for file in files:
            file_path = Path(root) / file
            if file_path.exists():
                file_count += 1
                total_size += file_path.stat().st_size
                file_list.append((file_path, file_path.relative_to(directory)))
    
    print(f"[SheepIt Submit]   Found {file_count} files, total size: {total_size / (1024*1024):.2f} MB")
    print(f"[SheepIt Submit]   Creating ZIP (this may take a while)...")
    
    if progress_callback:
        progress_callback(1.0, f"Creating ZIP archive ({file_count} files, {total_size / (1024*1024):.1f} MB)...")
    
    start_time = time.time()
    files_added = 0
    
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_STORED) as zipf:
        for file_path, arcname in file_list:
            if cancel_check and cancel_check():
                raise InterruptedError("ZIP creation cancelled by user")
            
            if not file_path.exists():
                continue
            
            try:
                zipf.write(file_path, arcname)
                files_added += 1
                
                # Progress updates - more frequent for large files
                if files_added == 1:
                    print(f"[SheepIt Submit]   Adding files to ZIP...")
                    if progress_callback:
                        progress_callback(2.0, f"Adding files to ZIP... (1/{file_count})")
                elif files_added % 10 == 0 or (file_count > 0 and files_added % max(1, file_count // 100) == 0):
                    elapsed = time.time() - start_time
                    rate = files_added / elapsed if elapsed > 0 else 0
                    progress_pct = 2.0 + (files_added / file_count * 93.0) if file_count > 0 else 2.0
                    print(f"[SheepIt Submit]   Progress: {files_added}/{file_count} files ({files_added*100//file_count}%), {rate:.1f} files/sec")
                    if progress_callback:
                        progress_callback(progress_pct, f"Creating ZIP... ({files_added}/{file_count} files, {rate:.1f} files/sec)")
            except Exception as e:
                print(f"[SheepIt Submit]   WARNING: Failed to add {arcname}: {type(e).__name__}: {str(e)}")
    
    elapsed = time.time() - start_time
    print(f"[SheepIt Submit] ZIP creation completed!")
    print(f"[SheepIt Submit]   Files added: {files_added}/{file_count}")
    print(f"[SheepIt Submit]   Time taken: {elapsed:.2f} seconds")
    if elapsed > 0:
        print(f"[SheepIt Submit]   Average rate: {files_added/elapsed:.1f} files/sec")
    
    if progress_callback:
        progress_callback(100.0, "ZIP archive created")


def register():
    """Register operators."""
    bpy.utils.register_class(SHEEPIT_OT_submit_current)


def unregister():
    """Unregister operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_submit_current)
