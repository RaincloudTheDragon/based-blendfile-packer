"""
Configuration constants for SheepIt Project Submitter addon.
"""

# Addon metadata
ADDON_NAME = "SheepIt Project Submitter"
ADDON_ID = "sheepit_project_submitter"

# SheepIt API endpoints (to be researched and updated)
SHEEPIT_API_BASE = "https://www.sheepit-renderfarm.com"
SHEEPIT_CLIENT_BASE = "https://client.sheepit-renderfarm.com"

# Debug mode
DEBUG = False


def debug_print(message: str) -> None:
    """Print debug message if DEBUG is enabled."""
    if DEBUG:
        print(f"[{ADDON_NAME}] {message}")
