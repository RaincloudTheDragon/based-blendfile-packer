"""
Submission operations for SheepIt render farm.
"""

import os
import zipfile
from pathlib import Path
from typing import Optional

import bpy
from bpy.types import Operator

from .. import config


class SHEEPIT_OT_submit_current(Operator):
    """Submit current blend file to SheepIt without packing."""
    bl_idname = "sheepit.submit_current"
    bl_label = "Submit Current Blend"
    bl_description = "Submit the current blend file to SheepIt without packing (for already-packed files or test submissions)"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        print(f"[SheepIt Submit] Starting submission of current blend file...")
        
        # Check if file is saved
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Please save the blend file before submitting.")
            return {'CANCELLED'}
        
        blend_path = Path(bpy.data.filepath)
        if not blend_path.exists():
            self.report({'ERROR'}, f"Blend file does not exist: {blend_path}")
            return {'CANCELLED'}
        
        print(f"[SheepIt Submit] Submitting: {blend_path}")
        
        # Get preferences for authentication
        from ..utils.compat import get_addon_prefs
        from ..utils.auth import load_auth_cookies
        from .api_submit import submit_file_to_sheepit
        
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
            self.report({'INFO'}, message)
            return {'FINISHED'}
        else:
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
