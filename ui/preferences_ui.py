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
        if not bpy.app.online_access:
            self.report({'ERROR'}, "Online access is disabled. Enable it in Preferences → System.")
            return {'CANCELLED'}
        
        prefs = compat.get_addon_prefs()
        if not prefs:
            self.report({'ERROR'}, "Addon preferences not found.")
            return {'CANCELLED'}
        
        # Try browser login cookies first
        if prefs.use_browser_login:
            from ..utils.auth import load_auth_cookies
            cookies = load_auth_cookies()
            if cookies:
                # Test with cookies
                # TODO: Implement actual API test with cookies
                self.report({'INFO'}, "Browser login cookies found. Connection test not yet implemented.")
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "No browser login cookies found. Please login via browser first.")
                return {'CANCELLED'}
        
        # Fallback to username/password or render key
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
    
    # Browser login status
    use_browser_login: BoolProperty(
        name="Use Browser Login",
        description="Login via browser and reuse session cookies (more secure, recommended)",
        default=True,
    )
    
    auth_status: StringProperty(
        name="Auth Status",
        description="Current authentication status",
        default="Not logged in",
    )
    
    # Manual session token entry (for browser login)
    session_token: StringProperty(
        name="Session Token",
        description="Session token from browser (extract from browser cookies after login)",
        default="",
        subtype='PASSWORD',
    )
    
    def draw(self, context):
        layout = self.layout
        
        # Update auth status if using browser login
        if self.use_browser_login:
            # Use direct import to avoid circular import issues
            from ..utils.auth import load_auth_cookies
            cookies = load_auth_cookies()
            if cookies:
                if self.auth_status == "Not logged in":
                    self.auth_status = "Logged in (Browser)"
            else:
                if self.auth_status != "Not logged in":
                    self.auth_status = "Not logged in"
        
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
        
        # Browser login option
        box = layout.box()
        box.label(text="Browser Login (Recommended):", icon='WORLD')
        box.prop(self, "use_browser_login")
        
        if self.use_browser_login:
            row = box.row()
            row.label(text=f"Status: {self.auth_status}")
            
            if self.auth_status == "Not logged in" or "Please login" in self.auth_status:
                row = box.row()
                row.scale_y = 1.2
                row.operator("sheepit.browser_login", text="Open Browser for Login", icon='WORLD')
                
                # Instructions
                box.label(text="After logging in:", icon='INFO')
                box.label(text="1. Open browser DevTools (F12)")
                box.label(text="2. Go to Application/Storage → Cookies")
                box.label(text="3. Find 'session' or 'PHPSESSID' cookie")
                box.label(text="4. Copy its value and paste below:")
                
                # Token entry
                row = box.row()
                row.prop(self, "session_token")
                row = box.row()
                row.scale_y = 1.2
                row.operator("sheepit.verify_login", text="Save Session Token", icon='CHECKMARK')
            else:
                row = box.row()
                row.scale_y = 1.2
                row.operator("sheepit.logout", text="Logout", icon='QUIT')
        
        layout.separator()
        
        # Info box
        box = layout.box()
        box.label(text="About Authentication:", icon='QUESTION')
        if self.use_browser_login:
            box.label(text="Browser Login: More secure than storing passwords.")
            box.label(text="Session tokens are stored encrypted on your system.")
            box.label(text="To get session token: Browser DevTools → Cookies → Copy session value.")
        else:
            box.label(text="Render Keys: Allow rendering without full account access.")
            box.label(text="They are safer to use in scripts and on shared machines.")
            box.label(text="Get your Render Key from: Profile → Edit Profile → Render keys")


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
