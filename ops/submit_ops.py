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


class SHEEPIT_OT_submit_project(Operator):
    """Submit project to SheepIt render farm."""
    bl_idname = "sheepit.submit_project"
    bl_label = "Submit to SheepIt"
    bl_description = "Submit the packed project to SheepIt render farm"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        scene = context.scene
        submit_settings = scene.sheepit_submit
        
        # Check if project has been packed
        if not submit_settings.pack_output_path:
            self.report({'ERROR'}, "Please pack the project first using 'Pack Copy Only' or 'Pack and Save'")
            return {'CANCELLED'}
        
        pack_path = Path(submit_settings.pack_output_path)
        if not pack_path.exists():
            self.report({'ERROR'}, f"Packed project path does not exist: {pack_path}")
            return {'CANCELLED'}
        
        # Get preferences for authentication
        prefs = context.preferences.addons.get(config.ADDON_ID)
        if not prefs or not prefs.preferences:
            self.report({'ERROR'}, "Addon preferences not found. Please configure SheepIt credentials in preferences.")
            return {'CANCELLED'}
        
        sheepit_prefs = prefs.preferences
        if not sheepit_prefs.sheepit_username or not (sheepit_prefs.sheepit_password or sheepit_prefs.sheepit_render_key):
            self.report({'ERROR'}, "Please configure SheepIt username and password/render key in preferences.")
            return {'CANCELLED'}
        
        try:
            # Determine if we need to create a ZIP
            # For copy-only workflow, create ZIP of the directory
            # For pack-and-save, we might submit the blend file directly or create ZIP
            
            # For now, create ZIP for both workflows (can be optimized later)
            zip_path = pack_path.parent / f"{pack_path.name}.zip"
            create_zip_from_directory(pack_path, zip_path)
            
            self.report({'INFO'}, f"Created ZIP: {zip_path}")
            self.report({'WARNING'}, "API submission not yet implemented. Please upload manually.")
            self.report({'INFO'}, f"Upload this file to SheepIt: {zip_path}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Submission failed: {str(e)}")
            return {'CANCELLED'}


def create_zip_from_directory(directory: Path, output_zip: Path) -> None:
    """Create a ZIP file from a directory."""
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(directory):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(directory)
                zipf.write(file_path, arcname)


def register():
    """Register operators."""
    bpy.utils.register_class(SHEEPIT_OT_submit_project)


def unregister():
    """Unregister operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_submit_project)
