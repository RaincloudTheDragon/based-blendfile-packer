"""
Output panel UI for BasedBlendfilePacker. Located in the Output tab, similar to Flamenco addon.
"""

import bpy
from bpy.types import Panel
from ..utils import compat


class BBP_PT_output_panel(Panel):
    """Blend packing panel in Output properties."""
    bl_label = "BasedBlendfilePacker"
    bl_idname = "BBP_PT_output_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "output"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        pack_settings = scene.bbp_pack

        box = layout.box()
        box.label(text="Animation Setup:", icon='ACTION')
        col = box.column()
        col.operator("bbp.enable_nla", text="Disable Animation Layers", icon='ACTION')

        layout.separator()

        box = layout.box()
        box.label(text="Frame Range:", icon='RENDER_ANIMATION')
        row = box.row()
        row.prop(pack_settings, "frame_range_mode", expand=True)

        if pack_settings.frame_range_mode == 'CUSTOM':
            col = box.column(align=True)
            col.prop(pack_settings, "frame_start")
            col.prop(pack_settings, "frame_end")
            col.prop(pack_settings, "frame_step")
        else:
            row = box.row()
            row.label(text=f"Scene Range: {scene.frame_start} - {scene.frame_end} (Step: {scene.frame_step})")

        layout.separator()

        if pack_settings.is_packing:
            box = layout.box()
            box.label(text=pack_settings.pack_status_message, icon='TIME')
            # Display-only loading bar (not an editable slider).
            factor = max(0.0, min(1.0, float(pack_settings.pack_progress) / 100.0))
            progress_row = box.row(align=True)
            progress_row.scale_y = 1.5
            progress_row.progress(
                factor=factor, type='BAR', text=f"{int(round(factor * 100.0))}%"
            )
            row = box.row()
            row.scale_y = 1.5
            row.operator("bbp.cancel_pack", text="Cancel", icon='X')
            layout.separator()

        col = layout.column()
        col.scale_y = 1.5
        col.enabled = not pack_settings.is_packing

        col.operator("bbp.pack_zip", text="Pack as ZIP (for scenes with caches)", icon='PACKAGE')
        row = layout.row()
        row.enabled = not pack_settings.is_packing
        row.prop(pack_settings, "exclude_av", text="Exclude video/audio")
        col.operator("bbp.pack_blend", text="Pack as Blend", icon='FILE_BLEND')

        layout.separator()

        box = layout.box()
        box.label(text="Output Path:", icon='FILE_FOLDER')
        row = box.row()
        row.prop(pack_settings, "output_path", text="")
        row = box.row()
        row.prop(pack_settings, "project_size_limit_gb", text="Size limit (GB)")


def register():
    """Register panel."""
    compat.safe_register_class(BBP_PT_output_panel)


def unregister():
    """Unregister panel."""
    compat.safe_unregister_class(BBP_PT_output_panel)
