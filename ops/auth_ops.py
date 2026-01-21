"""
Authentication operators for browser-based login.
"""

import bpy
from bpy.types import Operator
from ..utils.auth import browser_login, save_auth_cookies, clear_auth_cookies, load_auth_cookies
from .. import config


class SHEEPIT_OT_browser_login(Operator):
    """Open browser for SheepIt login."""
    bl_idname = "sheepit.browser_login"
    bl_label = "Open Browser for Login"
    bl_description = "Open browser to login to SheepIt. After logging in, click 'Verify Login' to save session."
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        if not bpy.app.online_access:
            self.report({'ERROR'}, "Online access is disabled. Enable it in Preferences → System.")
            return {'CANCELLED'}
        
        # Open browser
        if browser_login():
            # Update preferences status
            from ..utils.compat import get_addon_prefs
            prefs = get_addon_prefs()
            if prefs:
                prefs.auth_status = "Please login in browser, then click 'Verify Login'"
            self.report({'INFO'}, "Browser opened. Please login, then click 'Verify Login'.")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Failed to open browser.")
            return {'CANCELLED'}


class SHEEPIT_OT_verify_login(Operator):
    """Save session token from browser login."""
    bl_idname = "sheepit.verify_login"
    bl_label = "Save Session Token"
    bl_description = "Save the session token you copied from browser cookies"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        if not bpy.app.online_access:
            self.report({'ERROR'}, "Online access is disabled. Enable it in Preferences → System.")
            return {'CANCELLED'}
        
        from ..utils.compat import get_addon_prefs
        prefs = get_addon_prefs()
        if not prefs:
            self.report({'ERROR'}, "Addon preferences not found.")
            return {'CANCELLED'}
        
        if not prefs.session_token:
            self.report({'ERROR'}, "Please enter the session token from your browser.")
            return {'CANCELLED'}
        
        # Verify token (optional - can skip if user trusts it)
        # if not auth.verify_session_with_token(prefs.session_token):
        #     self.report({'WARNING'}, "Token verification failed, but saving anyway.")
        
        # Save as cookie
        cookies = {'session': prefs.session_token}  # Adjust cookie name based on SheepIt's actual cookie name
        
        if save_auth_cookies(cookies):
            # Update preferences status
            prefs.auth_status = "Logged in (Browser)"
            prefs.session_token = ""  # Clear token field for security
            self.report({'INFO'}, "Session token saved successfully!")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Failed to save session token.")
            return {'CANCELLED'}


class SHEEPIT_OT_logout(Operator):
    """Logout and clear stored authentication cookies."""
    bl_idname = "sheepit.logout"
    bl_label = "Logout"
    bl_description = "Clear stored authentication cookies and logout"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        if clear_auth_cookies():
            # Update preferences status
            from ..utils.compat import get_addon_prefs
            prefs = get_addon_prefs()
            if prefs:
                prefs.auth_status = "Not logged in"
            self.report({'INFO'}, "Logged out successfully.")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "No stored cookies to clear.")
            return {'FINISHED'}


def register():
    """Register authentication operators."""
    bpy.utils.register_class(SHEEPIT_OT_browser_login)
    bpy.utils.register_class(SHEEPIT_OT_verify_login)
    bpy.utils.register_class(SHEEPIT_OT_logout)


def unregister():
    """Unregister authentication operators."""
    bpy.utils.unregister_class(SHEEPIT_OT_logout)
    bpy.utils.unregister_class(SHEEPIT_OT_verify_login)
    bpy.utils.unregister_class(SHEEPIT_OT_browser_login)
