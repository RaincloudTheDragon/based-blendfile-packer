## [v0.0.6] - 2026-01-27

### Fixed
- Config import in `utils.compat`: use `from .. import config` (config is at addon root)
- Output panel: no longer write to scene in draw(); Blender 5.0 forbids ID writes in draw; operators already fall back to prefs when output_path empty

---

## [v0.0.5] - 2026-01-30

### Added
- Project size limit (GB) in Output panel: per-pack int (0 = no limit, default 2), max 32-bit int

### Fixed
- USD/cache file paths remapped: `bpy.data.cache_files[].filepath` remapped to packed location; .usd/.usdc/.usda added to copy_map

---

## [v0.0.4] - 2026-01-27

### Added
- ZIP pack: option to exclude video and audio files from archive
- Default output path in preferences
- NLA enable for animation layers (moved to UI panel; only runs on objects with anim layers)

### Fixed
- Physics/point cache included in ZIP pack (robocopy fallback when Python copy fails on network paths)
- Cache truncated to frame range (Blender bphys `name_frame_index` naming; safeguard if no files match)
- External cache paths remapped to relative (cache dirs in copy_map; prefix matching in remap script)
- Frame range applied only to top-level target blend, not dependent blends
- Recursion issue in all three pack ops; send-current-blend path handling
- Removed packed-suffix behavior

---

## [v0.0.3] - 2026-01-22

### Changed
- **Removed all website functionality** per SheepIt developer request
- Operators now save packed files to user-specified locations instead of uploading
- All authentication and website interaction code has been removed
- Users must manually upload and configure projects on the SheepIt website

---

## [v0.0.2] - 2026-01-22

### Fixed
- Fixed Blender extension policy violations related to `batter.asset_usage` module import
- Removed `sys.path` manipulation to comply with Blender extension policies
- Changed from top-level module import to submodule import (registered as `ops._asset_usage`)
- Fixed `dataclasses` `__module__` resolution issue when loading modules via `importlib`

### Internal
- Refactored `batter.asset_usage` import to use `importlib` without violating extension policies
- Module now properly registered in `sys.modules` as a submodule before execution

---

## [v0.0.1] - 2026-01-21

### Features
- Initial release of SheepIt Project Submitter
- Three submission workflows:
  - Submit Current: Direct submission of current blend file
  - Submit as ZIP: Automatic asset packing with ZIP archive creation
  - Submit as Packed Blend: Automatic asset packing directly into blend file
- Frame range configuration (full range or custom)
- Automatic asset packing for linked blend files, textures, images, and videos
- Cache truncation to match selected frame range
- Real-time progress tracking with cancellable operations
- File size validation (2GB limit) with optimization suggestions
- Automatic path remapping for all asset types
- Missing file detection and reporting
- Oversized file detection (>2GB linked files)
- Automatic backup file cleanup (`.blend1` through `.blend32`)
- Compressed blend file saves
- Username/password authentication
- Browser redirect to project configuration page after submission
- Works with unsaved blend files (operates on in-memory state)

### Internal
- Based on asset usage detection from Batter project
- Modal operator architecture for responsive UI
- Incremental packing system for large projects
- Subprocess-based asset processing for stability
- Comprehensive error handling and user feedback