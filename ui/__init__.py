"""
UI modules for SheepIt Project Submitter addon.
"""

from . import output_panel
from . import preferences_ui

__all__ = ["output_panel", "preferences_ui"]


def register():
    """Register all UI classes."""
    # Register preferences first so we can access variables in config.py
    preferences_ui.register()
    
    # Register other UI components
    output_panel.register()


def unregister():
    """Unregister all UI classes."""
    output_panel.unregister()
    preferences_ui.unregister()
