"""
Version detection and comparison utilities for multi-version Blender support.
Supports full SheepIt range: 3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.0, 4.1, 4.2, 4.3, 4.4, 4.5, 5.0+
Minimum supported: Blender 3.0
"""

import bpy


def get_blender_version():
    """
    Returns the current Blender version as a tuple (major, minor, patch).
    
    Returns:
        tuple: (major, minor, patch) version numbers
    """
    return bpy.app.version


def get_version_string():
    """
    Returns the current Blender version as a string (e.g., "4.2.0").
    
    Returns:
        str: Version string in format "major.minor.patch"
    """
    version = get_blender_version()
    return f"{version[0]}.{version[1]}.{version[2]}"


def is_version_at_least(major, minor=0, patch=0):
    """
    Check if the current Blender version is at least the specified version.
    
    Args:
        major (int): Major version number
        minor (int): Minor version number (default: 0)
        patch (int): Patch version number (default: 0)
    
    Returns:
        bool: True if current version >= specified version
    """
    current = get_blender_version()
    target = (major, minor, patch)
    
    if current[0] != target[0]:
        return current[0] > target[0]
    if current[1] != target[1]:
        return current[1] > target[1]
    return current[2] >= target[2]


def is_version_less_than(major, minor=0, patch=0):
    """
    Check if the current Blender version is less than the specified version.
    
    Args:
        major (int): Major version number
        minor (int): Minor version number (default: 0)
        patch (int): Patch version number (default: 0)
    
    Returns:
        bool: True if current version < specified version
    """
    return not is_version_at_least(major, minor, patch)


def is_version_3_x():
    """Check if running Blender 3.x."""
    version = get_blender_version()
    return version[0] == 3


def is_version_4_0():
    """Check if running Blender 4.0."""
    return is_version_at_least(4, 0, 0) and is_version_less_than(4, 1, 0)


def is_version_4_1():
    """Check if running Blender 4.1."""
    return is_version_at_least(4, 1, 0) and is_version_less_than(4, 2, 0)


def is_version_4_2():
    """Check if running Blender 4.2 LTS."""
    version = get_blender_version()
    return version[0] == 4 and version[1] == 2


def is_version_4_3():
    """Check if running Blender 4.3."""
    return is_version_at_least(4, 3, 0) and is_version_less_than(4, 4, 0)


def is_version_4_4():
    """Check if running Blender 4.4."""
    return is_version_at_least(4, 4, 0) and is_version_less_than(4, 5, 0)


def is_version_4_5():
    """Check if running Blender 4.5 LTS."""
    return is_version_at_least(4, 5, 0) and is_version_less_than(5, 0, 0)


def is_version_5_0():
    """Check if running Blender 5.0 or later."""
    return is_version_at_least(5, 0, 0)


def get_version_category():
    """
    Returns the version category string for the current Blender version.
    
    Returns:
        str: Version category like '3.0', '3.x', '4.0', '4.1', '4.2', '4.3', '4.4', '4.5', or '5.0+'
    """
    version = get_blender_version()
    major, minor = version[0], version[1]
    
    if major == 3:
        return '3.x'
    elif major == 4:
        if minor == 0:
            return '4.0'
        elif minor == 1:
            return '4.1'
        elif minor == 2:
            return '4.2'
        elif minor == 3:
            return '4.3'
        elif minor == 4:
            return '4.4'
        elif minor >= 5:
            return '4.5'
    elif major >= 5:
        return '5.0+'
    
    # Fallback
    return f"{major}.{minor}"
