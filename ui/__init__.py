"""
UI modules for SheepIt Project Submitter addon.
"""

from . import output_panel
from . import preferences_ui

__all__ = ["output_panel", "preferences_ui"]


def register():
    """Register all UI classes."""
    output_panel.register()
    preferences_ui.register()


def unregister():
    """Unregister all UI classes."""
    output_panel.unregister()
    preferences_ui.unregister()
