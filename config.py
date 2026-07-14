"""
Configuration constants for BasedBlendfilePacker addon.
"""

# Addon metadata
ADDON_NAME = "BasedBlendfilePacker"
ADDON_ID = "based_blendfile_packer"

# Supported Blender LTS releases (see blender_manifest.toml blender_version_min).
MIN_BLENDER_VERSION = (4, 5, 0)
SUPPORTED_LTS_TARGETS = ("4.5", "5.2")

# BAT v2 requires Blender 5.1+; 4.5 LTS uses vendored BAT v1.
BAT_V2_MIN_BLENDER_VERSION = (5, 1, 0)

# Debug mode
DEBUG = False


def debug_print(message: str) -> None:
    """Print debug message if DEBUG is enabled."""
    if DEBUG:
        print(f"[{ADDON_NAME}] {message}")
