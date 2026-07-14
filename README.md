# BasedBlendfilePacker

A farm-agnostic Blender addon for packing projects with automatic asset discovery, path remapping, and intelligent workflow management.

## Features

| Automatic Asset Packing | Frame Range Control | Multiple Packing Methods |
|--|--|--|
| Automatically packs all linked blend files, textures, images, and external assets into your project. Supports both ZIP and packed blend file workflows. | Configure custom frame ranges directly in Blender without saving your file. Frame ranges are automatically applied to packed files. | Pack as current blend file, packed ZIP archive, or packed blend file. Choose the method that best fits your project. |

| Cache Management | Size Validation | Progress Tracking |
|--|--|--|
| Automatically truncates cache files to match your selected frame range, reducing file sizes significantly. | Validates file sizes before packing with a configurable size limit and helpful suggestions for optimization. | Real-time progress bars and status messages for all operations. All steps are cancellable. |

| Path Remapping | Missing File Detection | Error Reporting |
|--|--|--|
| Intelligently remaps all asset paths for portable handoff to any render farm or pipeline. Handles textures, images, videos, and linked blend files. | Detects and reports missing linked files and oversized files that cannot be packed. | Comprehensive error messages with actionable suggestions for resolving issues. |

### Additional Features:
- Works with unsaved blend files (operates on in-memory state)
- Automatic backup file cleanup (`.blend1` through `.blend32`)
- Compressed blend file saves for optimal file sizes
- File browser for selecting output location

## Installation

1. Download the latest release from [GitHub Releases](https://github.com/RaincloudTheDragon/sheepit_project_submitter/releases)
2. In Blender, go to `Edit > Preferences > Add-ons`
3. Click `Install...` and select the downloaded ZIP file
4. Enable the addon by checking the box next to "BasedBlendfilePacker"

## Usage

1. **Set Frame Range**: In the Output properties panel, configure your frame range (full range or custom)
2. **Pack Project**: Choose your packing method:
   - **Pack Current Blend**: Saves the current blend file with frame range applied
   - **Pack as ZIP**: Packs all assets and creates a ZIP archive (recommended for scenes with caches)
   - **Pack as Blend**: Packs all assets directly into the blend file
3. **Select Output Location**: A file browser will open to select where to save the packed file
4. **Hand off**: Upload or transfer the packed output to your render farm or pipeline of choice

## Requirements

- Blender 3.0.0 or later

## License

GPL-3.0-or-later

## Links

- **GitHub Repository**: [https://github.com/RaincloudTheDragon/sheepit_project_submitter](https://github.com/RaincloudTheDragon/sheepit_project_submitter)
