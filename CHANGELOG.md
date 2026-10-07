## [v0.3.0] - 2026-10-07

### Features

- Jump-to-prefs button in the UI.
- Prefs toggle to defer to Flamenco’s bundled BAT wheels.
- Verbose pack debug toggle.
- Blend pack: pack-token suffix on output name conflict.
- Report pack duration.
- More responsive pack modal (Atomic-style hang-threshold yields).
- Fonts copied and remapped (including Windows Fonts / linked VFonts).

### Fixes

- ZIP: sanitize unsupported special characters in paths.
- Log missing files on subsequent pack runs.
- Stale-path recovery; Blender remap/pack scripts moved to external entrypoints.
- Restore full recursive blend pack.
- Heal nested lib-path ghosts; report missing linked IDs.
- Ignore sparse/phantom UDIM tiles in missing reports.
- Account for pack-linked/archive libraries.
- ZIP: only existing blendcache dirs targeted by hero sims.
- ZIP no longer runs `pack_linked` (libraries stay external).
- Copy paths still needed on disk even when packed in another library.

### Internal/Dev

- BAT v2 → 2.2.0.
- Ship and choose BAT wheels like Flamenco.
- Strip in-pack NLA enable (UI operator only).
- Hero-blend terminology in code/docs.
- README: farm scanners may miss packed linked libraries — use ZIP if needed.

## [v0.2.0] - 2026-10-05

### Features

- Windows taskbar progress during pack (#9).
- Display-only `layout.progress()` bar in the Output panel (#10);
- Cancel button (Esc still works).

### Fixes

- Missing/offline assets no longer abort the pack; reported Flamenco-style instead. Exclude A/V option renamed to `exclude_av`.

## [v0.1.1] - 2026-08-07

### Fixes

- Pack aborts on dead UNC/network asset paths (skips already-packed datablocks) instead of failing mid-remap with incomplete output.
- Pack aborts when textures/fonts/media are missing and cannot be packed; asks user to remap or remove them in the source blends.

## [v0.1.0] - 2026-07-17

### Changed

- Rebranded to BasedBlendfilePacker; support target is Blender 4.5 LTS and 5.2 LTS (`blender_version_min` 4.5.0).
- Asset discovery uses official Blender Asset Tracer: BAT v1 (vendored) on 4.5 LTS, BAT v2 (bundled wheel) on 5.1+/5.2 LTS. Replaces the old `batter` fork.
- Dependabot tracks BAT v1/v2 pins and GitHub Actions; sync workflow refreshes wheels/vendor.

### Removed

- Pack Current Blend workflow (SheepIt-era leftover; #4).

### Fixes

- BAT v2 discovery during Pack as Blend with temp-file override (skip pack-path clustering; handle `Library | None` reference keys).

## [v0.0.8] - 2026-03-16

### Fixes

- Texture copy: dedupe assets by resolved path and use normalized copy_map keys to prevent mixed UNC/drive paths causing missing textures.
- Pack as Blend: report "Blend file saved" instead of "ZIP file saved".
- Pack linked: remove missing libraries before pack_libraries() to prevent failures when external blends are unavailable.

## [v0.0.7] - 2026-02-12

### Fixes

- Pack: enable autopack before pack_all; force-load images and run pack_all twice; pack remaining images so textures are embedded (fixes "Failed to create GPU texture from Blender image" when rendering headless).
- Remap: print actual paths in warnings (not placeholders); normalized path lookup and reverse copy_map so library blend image paths resolve.
- pack_linked: catch PermissionError on library path checks so inaccessible (e.g. NAS) libs don't abort; remove missing/inaccessible library refs from blend before save.

## [v0.0.6] - 2026-01-27

### Fixes

- Config import in `utils.compat`: use `from .. import config` (config is at addon root)
- Output panel: no longer write to scene in draw(); Blender 5.0 forbids ID writes in draw; operators already fall back to prefs when output_path empty

## [v0.0.5] - 2026-01-30

### Features

- Project size limit (GB) in Output panel: per-pack int (0 = no limit, default 2), max 32-bit int

### Fixes

- USD/cache file paths remapped: `bpy.data.cache_files[].filepath` remapped to packed location; .usd/.usdc/.usda added to copy_map

## [v0.0.4] - 2026-01-27

### Features

- ZIP pack: option to exclude video and audio files from archive
- Default output path in preferences
- NLA enable for animation layers (moved to UI panel; only runs on objects with anim layers)

### Fixes

- Physics/point cache included in ZIP pack (robocopy fallback when Python copy fails on network paths)
- Cache truncated to frame range (Blender bphys `name_frame_index` naming; safeguard if no files match)
- External cache paths remapped to relative (cache dirs in copy_map; prefix matching in remap script)
- Frame range applied only to top-level target blend, not dependent blends
- Recursion issue in all three pack ops; send-current-blend path handling
- Removed packed-suffix behavior

## [v0.0.3] - 2026-01-22

### Changed

- **Removed all website functionality** per SheepIt developer request
- Operators now save packed files to user-specified locations instead of uploading
- All authentication and website interaction code has been removed
- Users must manually upload and configure projects on the SheepIt website

## [v0.0.2] - 2026-01-22

### Fixes

- Fixed Blender extension policy violations related to `batter.asset_usage` module import
- Removed `sys.path` manipulation to comply with Blender extension policies
- Changed from top-level module import to submodule import (registered as `ops._asset_usage`)
- Fixed `dataclasses` `__module__` resolution issue when loading modules via `importlib`

### Internal

- Refactored `batter.asset_usage` import to use `importlib` without violating extension policies
- Module now properly registered in `sys.modules` as a submodule before execution

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
