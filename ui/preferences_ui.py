"""
Preferences UI for SheepIt Project Submitter.
"""

import sys
import bpy
from bpy.types import AddonPreferences
from bpy.props import StringProperty
from .. import config


# Get the root module name dynamically
def _get_addon_module_name():
    """Get the root addon module name for bl_idname."""
    # In Blender 5.0 extensions loaded via VSCode, the module name is the full path
    # e.g., "bl_ext.vscode_development.sheepit_project_submitter"
    # We need to get it from the parent package (sheepit_project_submitter)
    try:
        # Get parent package name from __package__ (remove .ui suffix)
        if __package__:
            parent_pkg = __package__.rsplit('.', 1)[0] if '.' in __package__ else __package__
            # Get the actual module from sys.modules to get its __name__
            parent_module = sys.modules.get(parent_pkg)
            if parent_module and hasattr(parent_module, '__name__'):
                module_name = parent_module.__name__
                config.debug_print(f"[SheepIt Debug] Using parent module __name__ as bl_idname: {module_name}")
                return module_name
            else:
                # Use the package name directly
                config.debug_print(f"[SheepIt Debug] Using parent package name as bl_idname: {parent_pkg}")
                return parent_pkg
    except Exception as e:
        config.debug_print(f"[SheepIt Debug] Could not get parent module name: {e}")
    
    # Last fallback
    module_name = config.ADDON_ID
    config.debug_print(f"[SheepIt Debug] Using fallback bl_idname: {module_name}")
    return module_name


class SHEEPIT_AddonPreferences(AddonPreferences):
    """Addon preferences for SheepIt Project Submitter."""
    # bl_idname must match the add-on's module name exactly
    # Get it dynamically to ensure it matches what Blender registered
    bl_idname = _get_addon_module_name()
    
    default_output_path: StringProperty(
        name="Default Output Path",
        description="Default directory path where packed files will be saved",
        default="",
        subtype='DIR_PATH',
        update=lambda self, context: _sync_default_output_path(self, context),
    )
    
    def draw(self, context):
        layout = self.layout
        
        # Output path settings
        box = layout.box()
        box.label(text="Output Settings:", icon='FILE_FOLDER')
        box.prop(self, "default_output_path")
        
        layout.separator()
        
        # Info box
        box = layout.box()
        box.label(text="About:", icon='INFO')
        box.label(text="This addon packs your Blender projects for manual upload to SheepIt.")
        box.label(text="You must manually upload and configure projects on the SheepIt website.")


def _sync_default_output_path(prefs, context):
    """Sync default output path to all scenes' output_path if they're empty."""
    if not prefs.default_output_path:
        return
    
    # Update all scenes' output_path if they're empty
    for scene in bpy.data.scenes:
        if hasattr(scene, 'sheepit_submit') and scene.sheepit_submit:
            if not scene.sheepit_submit.output_path:
                scene.sheepit_submit.output_path = prefs.default_output_path


reg_list = [SHEEPIT_AddonPreferences]


def register():
    """Register preferences."""
    # Register preferences class
    for cls in reg_list:
        try:
            from bpy.utils import register_class
            register_class(cls)
            config.debug_print(f"[SheepIt Debug] Registered preferences class: {cls.__name__} with bl_idname: {cls.bl_idname}")
        except Exception as e:
            print(f"[SheepIt Error] Failed to register preferences class {cls.__name__}: {e}")
            import traceback
            traceback.print_exc()


def unregister():
    """Unregister preferences."""
    # Unregister preferences class
    from ..utils import compat
    for cls in reg_list:
        compat.safe_unregister_class(cls)
