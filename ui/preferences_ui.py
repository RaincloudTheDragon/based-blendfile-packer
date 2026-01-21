"""
Preferences UI for SheepIt Project Submitter.
Handles authentication credentials.
"""

import bpy
from bpy.types import AddonPreferences, Operator
from bpy.props import StringProperty, BoolProperty
from ..utils import compat
from .. import config


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
    bl_idname = config.ADDON_ID
    
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


def register():
    """Register preferences and operators."""
    # Preferences class is registered in main __init__.py
    # Only register the test connection operator here
    compat.safe_register_class(SHEEPIT_OT_test_connection)


def unregister():
    """Unregister preferences and operators."""
    # Preferences class is unregistered in main __init__.py
    # Only unregister the test connection operator here
    compat.safe_unregister_class(SHEEPIT_OT_test_connection)
