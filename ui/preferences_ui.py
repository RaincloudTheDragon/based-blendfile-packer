"""
Preferences UI for SheepIt Project Submitter.
Handles authentication credentials.
"""

import sys
import bpy
from bpy.types import AddonPreferences, Operator
from bpy.props import StringProperty, BoolProperty
from ..utils import compat
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


class SHEEPIT_OT_test_connection(Operator):
    """Test connection to SheepIt with current credentials."""
    bl_idname = "sheepit.test_connection"
    bl_label = "Test Connection"
    bl_description = "Test connection to SheepIt render farm with current credentials"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        prefs = context.preferences.addons[config.ADDON_ID].preferences
        
        if not prefs.sheepit_username:
            self.report({'ERROR'}, "Username is required")
            return {'CANCELLED'}
        
        if not prefs.sheepit_password and not prefs.sheepit_render_key:
            self.report({'ERROR'}, "Password or Render Key is required")
            return {'CANCELLED'}
        
        # Placeholder for actual API test
        # TODO: Implement actual API connection test
        self.report({'INFO'}, "Connection test not yet implemented. Please verify credentials manually.")
        return {'FINISHED'}


class SHEEPIT_AddonPreferences(AddonPreferences):
    """Addon preferences for SheepIt Project Submitter."""
    # bl_idname must match the add-on's module name exactly
    # Get it dynamically to ensure it matches what Blender registered
    bl_idname = _get_addon_module_name()
    
    # Authentication
    sheepit_username: StringProperty(
        name="Username",
        description="Your SheepIt render farm username",
        default="",
        subtype='NONE',
    )
    
    sheepit_password: StringProperty(
        name="Password",
        description="Your SheepIt account password (or leave empty to use Render Key)",
        default="",
        subtype='PASSWORD',
    )
    
    use_render_key: BoolProperty(
        name="Use Render Key",
        description="Use Render Key instead of password (more secure for scripts)",
        default=False,
    )
    
    sheepit_render_key: StringProperty(
        name="Render Key",
        description="Your SheepIt Render Key (found in profile settings)",
        default="",
        subtype='PASSWORD',
    )
    
    def draw(self, context):
        layout = self.layout
        
        # Header
        box = layout.box()
        box.label(text="SheepIt Authentication", icon='USER')
        
        # Username
        row = box.row()
        row.prop(self, "sheepit_username")
        
        # Password/Render Key toggle
        row = box.row()
        row.prop(self, "use_render_key")
        
        if self.use_render_key:
            # Render Key
            row = box.row()
            row.prop(self, "sheepit_render_key")
            box.label(text="Get your Render Key from:", icon='INFO')
            box.label(text="Profile → Edit Profile → Render keys")
        else:
            # Password
            row = box.row()
            row.prop(self, "sheepit_password")
            box.label(text="For better security, consider using a Render Key", icon='INFO')
        
        layout.separator()
        
        # Test connection button
        box = layout.box()
        row = box.row()
        row.scale_y = 1.5
        row.operator("sheepit.test_connection", icon='WORLD')
        
        layout.separator()
        
        # Info box
        box = layout.box()
        box.label(text="About Render Keys:", icon='QUESTION')
        box.label(text="Render Keys allow rendering without full account access.")
        box.label(text="They are safer to use in scripts and on shared machines.")


reg_list = [SHEEPIT_AddonPreferences]


def register():
    """Register preferences and operators."""
    # Register preferences class first
    for cls in reg_list:
        try:
            from bpy.utils import register_class
            register_class(cls)
            config.debug_print(f"[SheepIt Debug] Registered preferences class: {cls.__name__} with bl_idname: {cls.bl_idname}")
        except Exception as e:
            print(f"[SheepIt Error] Failed to register preferences class {cls.__name__}: {e}")
            import traceback
            traceback.print_exc()
    
    # Register test connection operator
    compat.safe_register_class(SHEEPIT_OT_test_connection)


def unregister():
    """Unregister preferences and operators."""
    # Unregister test connection operator
    compat.safe_unregister_class(SHEEPIT_OT_test_connection)
    
    # Unregister preferences class
    for cls in reg_list:
        compat.safe_unregister_class(cls)
