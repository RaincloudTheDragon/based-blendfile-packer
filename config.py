"""
Configuration constants for BasedBlendfilePacker addon.
"""

# Addon metadata
ADDON_NAME = "BasedBlendfilePacker"
ADDON_ID = "based_blendfile_packer"

# Debug mode
DEBUG = False


def debug_print(message: str) -> None:
    """Print debug message if DEBUG is enabled."""
    if DEBUG:
        print(f"[{ADDON_NAME}] {message}")
