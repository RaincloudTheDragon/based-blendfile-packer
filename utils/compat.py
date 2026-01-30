"""
Compatibility layer for handling differences across Blender versions.
Supports full SheepIt range: 3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.0, 4.1, 4.2, 4.3, 4.4, 4.5, 5.0+
Minimum supported: Blender 3.0
"""

import bpy
from bpy.utils import register_class, unregister_class
from . import version


def safe_register_class(cls):
    """
    Safely register a class, handling any version-specific registration issues.
    
    Args:
        cls: The class to register
    
    Returns:
        bool: True if registration succeeded, False otherwise
    """
    try:
        register_class(cls)
        return True
    except Exception as e:
        print(f"Warning: Failed to register {cls.__name__}: {e}")
        return False


def safe_unregister_class(cls):
    """
    Safely unregister a class, handling any version-specific unregistration issues.
    
    Args:
        cls: The class to unregister
    
    Returns:
        bool: True if unregistration succeeded, False otherwise
    """
    try:
        unregister_class(cls)
        return True
    except Exception as e:
        print(f"Warning: Failed to unregister {cls.__name__}: {e}")
        return False


def get_addon_prefs():
    """
    Get the addon preferences instance, compatible across versions.
    
    Returns:
        AddonPreferences or None: The addon preferences instance if found
    """
    from .. import config
    prefs = bpy.context.preferences
    addon = prefs.addons.get(config.ADDON_ID, None)
    if addon and getattr(addon, "preferences", None):
        return addon.preferences
    for addon in prefs.addons.values():
        ap = getattr(addon, "preferences", None)
        if ap and hasattr(ap, "default_output_path"):
            return ap
    return None


def is_library_or_override(datablock):
    """
    Check if a datablock is library-linked or an override.
    
    Args:
        datablock: The datablock to check
    
    Returns:
        bool: True if the datablock is library-linked or an override, False otherwise
    """
    # Check if datablock is linked from a library
    if hasattr(datablock, 'library') and datablock.library:
        return True
    
    # Check if datablock is an override (Blender 3.0+)
    if hasattr(datablock, 'override_library') and datablock.override_library:
        return True
    
    return False


def get_file_path_map(include_libraries=False):
    """
    Get file path map, handling version differences.
    
    Args:
        include_libraries (bool): Whether to include library paths
    
    Returns:
        dict: File path map
    """
    try:
        return bpy.data.file_path_map(include_libraries=include_libraries)
    except Exception:
        # Fallback for older versions
        return {}


def get_user_map():
    """
    Get user map, handling version differences.
    
    Returns:
        dict: User map
    """
    try:
        return bpy.data.user_map()
    except Exception:
        # Fallback for older versions
        return {}
