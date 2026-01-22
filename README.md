# SheepIt Project Submitter

A Blender addon for submitting projects to the SheepIt render farm with automatic asset packing and intelligent workflow management.

## Features

| Automatic Asset Packing | Frame Range Control | Multiple Submission Methods |
|--|--|--|
| Automatically packs all linked blend files, textures, images, and external assets into your submission. Supports both ZIP and packed blend file workflows. | Configure custom frame ranges directly in Blender without saving your file. Frame ranges are automatically applied to submissions. | Submit as current blend file, packed ZIP archive, or packed blend file. Choose the method that best fits your project. |

| Cache Management | Size Validation | Progress Tracking |
|--|--|--|
| Automatically truncates cache files to match your selected frame range, reducing file sizes significantly. | Validates file sizes before submission (2GB limit) with helpful suggestions for optimization. | Real-time progress bars and status messages for all operations. All steps are cancellable. |

| Path Remapping | Missing File Detection | Error Reporting |
|--|--|--|
| Intelligently remaps all asset paths to work correctly on the render farm. Handles textures, images, videos, and linked blend files. | Detects and reports missing linked files and oversized files (>2GB) that cannot be packed. | Comprehensive error messages with actionable suggestions for resolving issues. |

### Additional Features:
- Works with unsaved blend files (operates on in-memory state)
- Automatic backup file cleanup (`.blend1` through `.blend32`)
- Compressed blend file saves for optimal file sizes
- Username/password authentication
- Automatic browser redirect to project configuration page after submission

## Installation

1. Download the latest release from [GitHub Releases](https://github.com/RaincloudTheDragon/sheepit-project-submitter/releases)
2. In Blender, go to `Edit > Preferences > Add-ons`
3. Click `Install...` and select the downloaded ZIP file
4. Enable the addon by checking the box next to "SheepIt Project Submitter"

## Usage

1. **Configure Settings**: Go to `Edit > Preferences > Add-ons > SheepIt Project Submitter` and enter your SheepIt username and password
2. **Set Frame Range**: In the Output properties panel, configure your frame range (full range or custom)
3. **Submit**: Choose your submission method:
   - **Submit Current**: Submits the current blend file as-is
   - **Submit as ZIP**: Packs all assets and creates a ZIP archive
   - **Submit as Packed Blend**: Packs all assets directly into the blend file
4. **Configure Project**: After submission, you'll be redirected to the SheepIt project configuration page to set up render settings

## Requirements

- Blender 3.0.0 or later
- Active SheepIt render farm account
- Internet connection for submission

## License

GPL-3.0-or-later

## Links

- **GitHub Repository**: [https://github.com/RaincloudTheDragon/sheepit-project-submitter](https://github.com/RaincloudTheDragon/sheepit-project-submitter)
- **SheepIt Render Farm**: [https://www.sheepit-renderfarm.com](https://www.sheepit-renderfarm.com)
