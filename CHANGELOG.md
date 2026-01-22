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
