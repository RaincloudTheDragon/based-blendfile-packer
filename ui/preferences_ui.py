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
        
        # Import test function
        from ..utils.auth import test_connection
        
        # Test connection based on authentication method
        if prefs.use_browser_login:
            # Test with browser login cookies
            success, message, user_info = test_connection(use_browser_login=True)
        else:
            # Test with username/password
            if not prefs.sheepit_username:
                self.report({'ERROR'}, "Username is required")
                return {'CANCELLED'}
            
            if not prefs.sheepit_password:
                self.report({'ERROR'}, "Password is required")
                return {'CANCELLED'}
            
            success, message, user_info = test_connection(
                use_browser_login=False,
                username=prefs.sheepit_username,
                password=prefs.sheepit_password
            )
        
        # Report results
        if success:
            self.report({'INFO'}, message)
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}


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
        description="Your SheepIt account password",
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
        
        # Main authentication box
        box = layout.box()
        box.label(text="SheepIt Authentication", icon='USER')
        
        # Browser login toggle
        row = box.row()
        row.prop(self, "use_browser_login")
        
        if self.use_browser_login:
            # Browser login mode
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
        else:
            # Username/Password mode
            row = box.row()
            row.prop(self, "sheepit_username")
            
            row = box.row()
            row.prop(self, "sheepit_password")
        
        box.separator()
        
        # Test connection button
        row = box.row()
        row.scale_y = 1.5
        row.operator("sheepit.test_connection", icon='WORLD')
        
        box.separator()
        
        # Info box
        box.label(text="About Authentication:", icon='QUESTION')
        if self.use_browser_login:
            box.label(text="Browser Login: More secure than storing passwords.")
            box.label(text="Session tokens are stored encrypted on your system.")
            box.label(text="To get session token: Browser DevTools → Cookies → Copy session value.")
        else:
            box.label(text="Username/Password: Traditional login method.")
            box.label(text="For better security, consider using Browser Login instead.")


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
