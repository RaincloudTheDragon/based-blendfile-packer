"""
Preferences UI for BasedBlendfilePacker.
"""

import sys
import bpy
from bpy.types import AddonPreferences
from bpy.props import StringProperty
from .. import config


def _get_addon_module_name():
    """Get the root addon module name for bl_idname."""
    try:
        if __package__:
            parent_pkg = __package__.rsplit('.', 1)[0] if '.' in __package__ else __package__
            parent_module = sys.modules.get(parent_pkg)
            if parent_module and hasattr(parent_module, '__name__'):
                module_name = parent_module.__name__
                config.debug_print(f"[BBP Debug] Using parent module __name__ as bl_idname: {module_name}")
                return module_name
            config.debug_print(f"[BBP Debug] Using parent package name as bl_idname: {parent_pkg}")
            return parent_pkg
    except Exception as e:
        config.debug_print(f"[BBP Debug] Could not get parent module name: {e}")

    module_name = config.ADDON_ID
    config.debug_print(f"[BBP Debug] Using fallback bl_idname: {module_name}")
    return module_name


class BBP_AddonPreferences(AddonPreferences):
    """Addon preferences for BasedBlendfilePacker."""
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

        box = layout.box()
        box.label(text="Output Settings:", icon='FILE_FOLDER')
        box.prop(self, "default_output_path")

        layout.separator()

        box = layout.box()
        box.label(text="About:", icon='INFO')
        box.label(text="BasedBlendfilePacker packs Blender projects for any render farm or pipeline.")
        box.label(text="Upload or transfer packed output to your target environment manually.")


def _sync_default_output_path(prefs, context):
    """Sync default output path to all scenes' output_path if they're empty."""
    if not prefs.default_output_path:
        return

    for scene in bpy.data.scenes:
        if hasattr(scene, 'bbp_pack') and scene.bbp_pack:
            if not scene.bbp_pack.output_path:
                scene.bbp_pack.output_path = prefs.default_output_path


reg_list = [BBP_AddonPreferences]


def register():
    """Register preferences."""
    for cls in reg_list:
        try:
            from bpy.utils import register_class
            register_class(cls)
            config.debug_print(f"[BBP Debug] Registered preferences class: {cls.__name__} with bl_idname: {cls.bl_idname}")
        except Exception as e:
            print(f"[BBP Error] Failed to register preferences class {cls.__name__}: {e}")
            import traceback
            traceback.print_exc()


def unregister():
    """Unregister preferences."""
    from ..utils import compat
    for cls in reg_list:
        compat.safe_unregister_class(cls)
