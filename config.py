"""
Configuration constants for BasedBlendfilePacker addon.
"""

# Addon metadata
ADDON_NAME = "BasedBlendfilePacker"
ADDON_ID = "based_blendfile_packer"

# Supported Blender LTS releases (see blender_manifest.toml blender_version_min).
MIN_BLENDER_VERSION = (4, 5, 0)
SUPPORTED_LTS_TARGETS = ("4.5", "5.2")

# BAT v2 requires Blender 5.1+; 4.5 LTS uses the BAT v1 wheel.
BAT_V2_MIN_BLENDER_VERSION = (5, 1, 0)

# Debug mode
DEBUG = False

# Pack modal: process as much as possible per timer tick; yield after a unit that exceeds this (seconds) so the UI can redraw / name the slow item — same sweet spot as Atomic's scanner (benchmarked 0.1s for responsiveness vs throughput).
PACK_HANG_THRESHOLD_SEC = 0.1
PACK_BATCH_UNBOUNDED = 10**9

# Pack-linked: parallel Blender subprocesses (deps in parallel, hero last). Timeout kills the worker; that blend is marked failed and the chain continues.
PACK_LINKED_WORKERS = 4
PACK_LINKED_TIMEOUT_SEC = 15


def note_pack_hang(state, phase: str, name: str, elapsed: float) -> None:
    """Record a slow pack unit on *state* for status-bar hang suffix."""
    if state is None:
        return
    try:
        state.hang_phase = phase
        state.hang_name = name
        state.hang_elapsed = float(elapsed)
    except Exception:
        pass


def hang_status_suffix(state) -> str:
    """Status-bar fragment when the last batch ended on a slow unit."""
    if not state:
        return ""
    name = getattr(state, "hang_name", None)
    if not name:
        return ""
    elapsed = getattr(state, "hang_elapsed", None)
    if elapsed is None:
        return f" — slow: {name}"
    return f" — slow: {name} ({elapsed:.1f}s)"


def debug_print(message: str) -> None:
    """Print debug message if DEBUG is enabled."""
    if DEBUG:
        print(f"[{ADDON_NAME}] {message}", flush=True)
