# BasedBlendfilePacker

A farm-agnostic Blender addon for packing projects with automatic asset discovery, path remapping, and intelligent workflow management.

## Features

| Automatic Asset Packing | Frame Range Control | Multiple Packing Methods |
|--|--|--|
| Discovers the hero blend’s linked libraries, textures, images, and external assets. ZIP keeps them as a remapped pack tree; Pack as Blend embeds them into the hero. | Configure custom frame ranges directly in Blender without saving your file. Frame ranges are applied to the hero in the pack output. | Pack as ZIP archive or packed blend file. Choose the method that best fits your project. |

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

1. Download the latest release from [GitHub Releases](https://github.com/RaincloudTheDragon/based-blendfile-packer/releases)
2. In Blender, go to `Edit > Preferences > Add-ons`
3. Click `Install...` and select the downloaded ZIP file
4. Enable the addon by checking the box next to "BasedBlendfilePacker"

## Terminology

| Term | Meaning |
|--|--|
| **Hero** / **hero blend** | The open scene `.blend` you are packing (BAT-style root of the dependency tree). In samples this is often `hero.blend`. |
| **Dependent blends** | Linked library `.blend` files the hero references (characters, props, rigs, materials, scenes, geonodes, etc.). |
| **Pack tree** | Temporary directory of remapped copies (hero + dependents + textures/caches) before ZIP or blend submit. |

## Usage

1. **Set Frame Range**: In the Output properties panel, configure your frame range (full range or custom)
2. **Pack Project**: Choose your packing method:
   - **Pack as ZIP**: Copies the hero and dependents into a pack tree with remapped paths, then zips it. Libraries stay as external `.blend` files in the archive (not packed into the hero). Recommended for scenes with caches.
   - **Pack as Blend**: Remaps paths, then packs assets and linked libraries into the hero blend for a single-file handoff.
3. **Select Output Location**: A file browser will open to select where to save the packed file
4. **Hand off**: Upload or transfer the packed output to your render farm or pipeline of choice

## Requirements

- **Blender 4.5 LTS** or **Blender 5.2 LTS** (minimum: Blender 4.5.0)
- Asset tracing uses [Blender Asset Tracer](https://projects.blender.org/blender/blender-asset-tracer):
  - **4.5 LTS** — BAT v1 wheel (loaded at runtime)
  - **5.2 LTS** — BAT v2 wheel (loaded at runtime)


## License

GPL-3.0-or-later

## Links

- **GitHub Repository**: [https://github.com/RaincloudTheDragon/based-blendfile-packer](https://github.com/RaincloudTheDragon/based-blendfile-packer)
