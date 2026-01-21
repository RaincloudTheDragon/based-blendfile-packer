"""
Utility modules for SheepIt Project Submitter addon.
"""

from . import compat
from . import version

# Don't import auth at module level to avoid circular imports
# It will be imported lazily when needed
# Users should import it as: from ..utils.auth import <function>
# Or: from ..utils import auth (which will trigger lazy import)

__all__ = ["compat", "version"]


def __getattr__(name):
    """Lazy import for auth module to avoid circular imports."""
    if name == "auth":
        from . import auth
        return auth
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
