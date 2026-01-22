"""
Output panel UI for SheepIt Project Submitter.
Located in the Output tab, similar to Flamenco addon.
"""

import bpy
from bpy.types import Panel
from ..utils import compat


class SHEEPIT_PT_output_panel(Panel):
    """SheepIt submission panel in Output properties."""
    bl_label = "SheepIt Render Farm"
    bl_idname = "SHEEPIT_PT_output_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "output"
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        submit_settings = scene.sheepit_submit
        
        # Initialize output path from preferences if empty (only check once per draw)
        if not submit_settings.output_path:
            from ..utils.compat import get_addon_prefs
            prefs = get_addon_prefs()
            if prefs and prefs.default_output_path:
                submit_settings.output_path = prefs.default_output_path
        
        # Frame Range Section
        box = layout.box()
        box.label(text="Frame Range:", icon='RENDER_ANIMATION')
        row = box.row()
        row.prop(submit_settings, "frame_range_mode", expand=True)
        
        if submit_settings.frame_range_mode == 'CUSTOM':
            col = box.column(align=True)
            col.prop(submit_settings, "frame_start")
            col.prop(submit_settings, "frame_end")
            col.prop(submit_settings, "frame_step")
        else:
            # Show current scene frame range
            row = box.row()
            row.label(text=f"Scene Range: {scene.frame_start} - {scene.frame_end} (Step: {scene.frame_step})")
        
        layout.separator()
        
        # Progress Bar Section (when submitting)
        if submit_settings.is_submitting:
            box = layout.box()
            box.label(text=submit_settings.submit_status_message, icon='TIME')
            box.prop(submit_settings, "submit_progress", text="Progress", slider=True)
            layout.separator()
        
        # Packing Buttons
        col = layout.column()
        col.scale_y = 1.5
        
        # Pack Current Blend button (first)
        op = col.operator("sheepit.submit_current", text="Pack Current Blend", icon='EXPORT')
        
        # Pack as ZIP button
        op = col.operator("sheepit.pack_zip", text="Pack as ZIP (for scenes with caches)", icon='PACKAGE')
        
        # Pack as Blend button
        op = col.operator("sheepit.pack_blend", text="Pack as Blend", icon='FILE_BLEND')
        
        layout.separator()
        
        # Output Path Section
        box = layout.box()
        box.label(text="Output Path:", icon='FILE_FOLDER')
        row = box.row()
        row.prop(submit_settings, "output_path", text="")


def register():
    """Register panel."""
    compat.safe_register_class(SHEEPIT_PT_output_panel)


def unregister():
    """Unregister panel."""
    compat.safe_unregister_class(SHEEPIT_PT_output_panel)
