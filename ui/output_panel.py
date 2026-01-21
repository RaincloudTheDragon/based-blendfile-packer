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
        
        # Compute Method Section
        box = layout.box()
        box.label(text="Compute Method:", icon='SETTINGS')
        row = box.row()
        row.prop(submit_settings, "compute_method", expand=True)
        
        # Show queue info if available (placeholder for future API integration)
        if submit_settings.compute_method == 'CPU':
            row = box.row()
            row.label(text="Est. queue position: 1st", icon='INFO')
        else:
            row = box.row()
            row.label(text="Est. queue position: 1st", icon='INFO')
        
        layout.separator()
        
        # Options Section
        box = layout.box()
        box.label(text="Options:", icon='CHECKBOX_HLT')
        box.prop(submit_settings, "renderable_by_all")
        box.prop(submit_settings, "generate_mp4")
        
        layout.separator()
        
        # Advanced Options (collapsible)
        box = layout.box()
        row = box.row()
        row.prop(submit_settings, "show_advanced", toggle=True, icon='TRIA_DOWN' if submit_settings.show_advanced else 'TRIA_RIGHT')
        row.label(text="Advanced Options")
        
        if submit_settings.show_advanced:
            box.prop(submit_settings, "memory_used_mb")
        
        layout.separator()
        
        # Submission Buttons
        col = layout.column()
        col.scale_y = 1.5
        
        # Submit as ZIP button
        op = col.operator("sheepit.pack_copy_only", text="Submit as ZIP (for scenes with caches)", icon='PACKAGE')
        
        # Submit as Blend button
        op = col.operator("sheepit.pack_and_save", text="Submit as Blend", icon='FILE_BLEND')
        
        # Show pack output path if available
        if submit_settings.pack_output_path:
            layout.separator()
            box = layout.box()
            box.label(text="Packed Output:", icon='FILE_FOLDER')
            row = box.row()
            row.scale_x = 0.8
            row.label(text=submit_settings.pack_output_path)
            
            # Submit button (only shown after packing)
            layout.separator()
            submit_op = layout.operator("sheepit.submit_project", text="Submit to SheepIt", icon='EXPORT')
            submit_op.scale_y = 1.5


def register():
    """Register panel."""
    compat.safe_register_class(SHEEPIT_PT_output_panel)


def unregister():
    """Unregister panel."""
    compat.safe_unregister_class(SHEEPIT_PT_output_panel)
