"""
BasedBlendfilePacker Addon

A farm-agnostic Blender addon for packing projects with automatic asset discovery and export.
"""

import bpy
from bpy.utils import register_class
from .utils import compat
from . import ops
from . import ui
from . import rainys_repo_bootstrap


def _update_output_path(self, context):
    """Update callback for output_path property - auto-populates from preferences if empty."""
    if not self.output_path:
        from .utils.compat import get_addon_prefs
        prefs = get_addon_prefs()
        if prefs and prefs.default_output_path:
            self.output_path = prefs.default_output_path


class BBP_PG_pack_settings(bpy.types.PropertyGroup):
    """Property group for blend packing settings."""

    frame_range_mode: bpy.props.EnumProperty(
        name="Frame Range Mode",
        description="Choose between full range or custom frame range",
        items=[
            ('FULL', "Full Range", "Use the full frame range from scene settings"),
            ('CUSTOM', "Custom", "Specify custom start, end, and step frames"),
        ],
        default='FULL',
    )

    frame_start: bpy.props.IntProperty(
        name="Start Frame",
        description="Start frame for rendering",
        default=1,
        min=0,
    )

    frame_end: bpy.props.IntProperty(
        name="End Frame",
        description="End frame for rendering",
        default=250,
        min=0,
    )

    frame_step: bpy.props.IntProperty(
        name="Frame Step",
        description="Frame step (render every Nth frame)",
        default=1,
        min=1,
    )

    exclude_video_from_zip: bpy.props.BoolProperty(
        name="Exclude video/audio from ZIP",
        description="Exclude video and audio files (e.g. mp4, avi, mov, wav, mp3) from the ZIP pack",
        default=False,
    )

    project_size_limit_gb: bpy.props.IntProperty(
        name="Project Size Limit (GB)",
        description="Maximum project/ZIP/blend size in GB (0 = no limit)",
        default=2,
        min=0,
        max=(1 << 31) - 1,
    )

    pack_output_path: bpy.props.StringProperty(
        name="Pack Output Path",
        description="Path to the packed output directory",
        default="",
    )

    output_file_path: bpy.props.StringProperty(
        name="Output File Path",
        description="Path where the packed file (ZIP or blend) will be saved",
        default="",
        subtype='FILE_PATH',
    )

    output_path: bpy.props.StringProperty(
        name="Output Path",
        description="Directory path where packed files will be saved",
        default="",
        subtype='DIR_PATH',
        update=_update_output_path,
    )

    is_packing: bpy.props.BoolProperty(
        name="Is Packing",
        description="Whether a packing operation is currently in progress",
        default=False,
    )

    pack_progress: bpy.props.FloatProperty(
        name="Pack Progress",
        description="Progress percentage for packing operations",
        default=0.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE',
    )

    pack_status_message: bpy.props.StringProperty(
        name="Pack Status Message",
        description="Current status message for packing operations",
        default="",
    )


def register():
    """Register the addon."""
    from .utils import compat

    compat.safe_register_class(BBP_PG_pack_settings)
    bpy.types.Scene.bbp_pack = bpy.props.PointerProperty(type=BBP_PG_pack_settings)

    ops.register()
    ui.register()
    rainys_repo_bootstrap.register()


def unregister():
    """Unregister the addon."""
    from .utils import compat

    rainys_repo_bootstrap.unregister()
    ui.unregister()
    ops.unregister()

    compat.safe_unregister_class(BBP_PG_pack_settings)
    if hasattr(bpy.types.Scene, 'bbp_pack'):
        del bpy.types.Scene.bbp_pack
