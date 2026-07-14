"""
Version detection and comparison utilities.

Supported targets: Blender 4.5 LTS (BAT v1) and Blender 5.2 LTS (BAT v2).
Minimum supported version: Blender 4.5.0.
"""

import bpy

from .. import config


def get_blender_version():
    """Return the current Blender version as ``(major, minor, patch)``."""
    return bpy.app.version


def get_version_string():
    """Return the current Blender version as a string (e.g. ``4.5.2``)."""
    version = get_blender_version()
    return f"{version[0]}.{version[1]}.{version[2]}"


def is_version_at_least(major, minor=0, patch=0):
    """Return whether the current Blender version is at least the given version."""
    current = get_blender_version()
    target = (major, minor, patch)

    if current[0] != target[0]:
        return current[0] > target[0]
    if current[1] != target[1]:
        return current[1] > target[1]
    return current[2] >= target[2]


def is_version_less_than(major, minor=0, patch=0):
    """Return whether the current Blender version is below the given version."""
    return not is_version_at_least(major, minor, patch)


def is_supported_version():
    """Return whether the running Blender version meets the addon minimum."""
    major, minor, patch = config.MIN_BLENDER_VERSION
    return is_version_at_least(major, minor, patch)


def is_version_4_5_lts():
    """Return whether the running Blender is in the 4.5 LTS line (BAT v1 path)."""
    return is_version_at_least(4, 5, 0) and is_version_less_than(5, 1, 0)


def is_version_5_2_lts():
    """Return whether the running Blender is 5.2+ (primary 5.x LTS target)."""
    return is_version_at_least(5, 2, 0)


def uses_bat_v2_blender_version():
    """Return whether this Blender version should load BAT v2 (5.1+)."""
    major, minor, patch = config.BAT_V2_MIN_BLENDER_VERSION
    return is_version_at_least(major, minor, patch)


def get_version_category():
    """
    Return a short version label for the current Blender build.

    Returns:
        ``4.5`` for the 4.5 LTS line, ``5.2+`` for 5.2 LTS and newer, or
        ``major.minor`` as a fallback.
    """
    version = get_blender_version()
    major, minor = version[0], version[1]

    if major == 4 and minor >= 5:
        return "4.5"
    if major >= 5:
        if minor >= 2:
            return "5.2+"
        if minor >= 1:
            return "5.1+"
        return "5.0"

    return f"{major}.{minor}"
