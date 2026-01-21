"""
Operators for SheepIt Project Submitter addon.
"""

from . import pack_ops
from . import submit_ops

__all__ = ["pack_ops", "submit_ops"]


def register():
    """Register all operators."""
    pack_ops.register()
    submit_ops.register()


def unregister():
    """Unregister all operators."""
    pack_ops.unregister()
    submit_ops.unregister()
