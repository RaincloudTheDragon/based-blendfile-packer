"""
SheepIt Project Submitter Addon

A Blender addon for submitting projects to SheepIt render farm with automatic asset packing.
Based on Dr. Sybren's Batter (Bat v2.0) project.
"""

import bpy
from bpy.utils import register_class
from .utils import compat
from . import ops
from . import ui
from . import rainys_repo_bootstrap
from .ui.preferences_ui import SHEEPIT_AddonPreferences


# SheepIt Submit Settings Property Group
class SHEEPIT_PG_submit_settings(bpy.types.PropertyGroup):
    """Property group for storing submit settings."""
    
    # Frame range mode
    frame_range_mode: bpy.props.EnumProperty(
        name="Frame Range Mode",
        description="Choose between full range or custom frame range",
        items=[
            ('FULL', "Full Range", "Use the full frame range from scene settings"),
            ('CUSTOM', "Custom", "Specify custom start, end, and step frames"),
        ],
        default='FULL',
    )
    
    # Custom frame range
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
    
    # Compute method
    compute_method: bpy.props.EnumProperty(
        name="Compute Method",
        description="Choose CPU or GPU rendering",
        items=[
            ('CPU', "CPU", "Use CPU for rendering"),
            ('GPU', "GPU", "Use GPU for rendering"),
        ],
        default='CPU',
    )
    
    # Checkboxes
    renderable_by_all: bpy.props.BoolProperty(
        name="Renderable by all members",
        description="Allow all SheepIt members to render this project",
        default=True,
    )
    
    generate_mp4: bpy.props.BoolProperty(
        name="Generate MP4 video",
        description="Generate MP4 video from rendered frames",
        default=False,
    )
    
    # Advanced options
    memory_used_mb: bpy.props.StringProperty(
        name="Memory used (MB)",
        description="Memory limit in MB (leave empty for default)",
        default="",
    )
    
    # Advanced options visibility
    show_advanced: bpy.props.BoolProperty(
        name="Show Advanced Options",
        description="Show advanced submission options",
        default=False,
    )
    
    # Pack output path (set by pack operators)
    pack_output_path: bpy.props.StringProperty(
        name="Pack Output Path",
        description="Path to the packed output directory",
        default="",
    )


def register():
    """Register the addon."""
    from .utils import compat
    
    # Register preferences first (needed for Blender to show preferences UI)
    compat.safe_register_class(SHEEPIT_AddonPreferences)
    
    compat.safe_register_class(SHEEPIT_PG_submit_settings)
    bpy.types.Scene.sheepit_submit = bpy.props.PointerProperty(type=SHEEPIT_PG_submit_settings)
    
    # Register operators and UI
    ops.register()
    ui.register()
    
    # Bootstrap Rainy's Extensions repository
    rainys_repo_bootstrap.register()


def unregister():
    """Unregister the addon."""
    from .utils import compat
    
    # Bootstrap unregistration
    rainys_repo_bootstrap.unregister()
    
    # Unregister operators and UI
    ui.unregister()
    ops.unregister()
    
    compat.safe_unregister_class(SHEEPIT_PG_submit_settings)
    compat.safe_unregister_class(SHEEPIT_AddonPreferences)
    if hasattr(bpy.types.Scene, 'sheepit_submit'):
        del bpy.types.Scene.sheepit_submit
