from __future__ import annotations

import dataclasses
import functools
from collections import defaultdict
from pathlib import Path

import bpy
from bpy.types import Library, ID


def find() -> dict[Library | None, set[AssetUsage]]:
    """Return a mapping from each blend file to the assets it uses.

    The None key indicates the direct dependencies of the currently-open blend file.
    """

    _blend_asset_usage = find_blend_asset_usage()
    _nonblend_asset_usage = find_nonblend_asset_usage()
    return _merge_keys(_blend_asset_usage, _nonblend_asset_usage)


@dataclasses.dataclass
class AssetUsage:
    """The usage of an asset by a specific blend file."""

    abspath: Path
    """Absolute path to the asset."""

    # user: Path
    # """Absolute path of whatever blend file uses this Asset."""

    reference_path: str
    """The path by which this asset is referenced.

    This is tracked so that the search & replace operation for path rewriting
    knows what to search for.

    NOTE: the above may not be true, as paths to assets from a library blend
    file may already be rewritten by Blender upon loading; this process ensures
    that all relative paths are relative to the main blend file, and so they may
    not be the same in the library itself.
    """

    is_blendfile: bool
    """Whether this asset is a blend file or not.

    Blend files can refer to other assets.
    """

    def __hash__(self) -> int:
        return hash((self.abspath, self.reference_path, self.is_blendfile))

    def __eq__(self, value: object) -> bool:
        if not isinstance(value, AssetUsage):
            return False
        return (
            self.abspath,
            self.reference_path,
            self.is_blendfile,
        ) == (
            value.abspath,
            value.reference_path,
            value.is_blendfile,
        )


@functools.lru_cache
def library_abspath(lib: Library | None) -> Path:
    """Return the absolute path to the library.

    lib=None returns the absolute path of the current blend file.
    """
    if lib is None:
        filepath = bpy.data.filepath
    else:
        filepath = bpy.path.abspath(lib.filepath)
    return Path(filepath).resolve()


def find_blend_asset_usage() -> dict[Library | None, set[AssetUsage]]:
    """Return a mapping from each blend file to the blend files it uses as libraries."""

    # Find all dependencies between libraries.
    # Keys: Library ID (or `None` for current blendfile)
    # Values: Libraries used by the key one.
    libs_deps: dict[Library | None, set[AssetUsage]] = defaultdict(set)
    for id, id_users in bpy.data.user_map().items():
        id_lib = id.library
        libs_deps.setdefault(id_lib, set())
        for id_user in id_users:
            if id_user.library == id_lib:
                continue

            asset_usage = AssetUsage(
                abspath=library_abspath(id_lib),
                reference_path=id_lib.filepath,
                is_blendfile=True,
            )

            libs_deps[id_user.library].add(asset_usage)

    return dict(libs_deps)


def find_nonblend_asset_usage() -> dict[Library | None, set[AssetUsage]]:
    """Return a mapping from a blend file to the non-blend asset files it uses."""

    file_path_map = bpy.data.file_path_map(include_libraries=False)

    asset_usages: dict[Library | None, set[AssetUsage]] = defaultdict(set)

    for asset_user, filepaths in file_path_map.items():
        if not filepaths:
            continue
        if isinstance(asset_user, Library):
            continue

        assert isinstance(asset_user, ID)

        lib = asset_user.library
        if lib and lib.packed_file:
            raise RuntimeError(f"Batter does not support packed libraries (yet): {lib}")

        for filepath in filepaths:
            abspath = Path(bpy.path.abspath(filepath, library=lib)).resolve()
            asset_usage = AssetUsage(
                abspath=abspath,
                reference_path=filepath,
                is_blendfile=False,
            )
            asset_usages[lib].add(asset_usage)

    # Also check bpy.data.images directly, as file_path_map may miss some image references
    # (e.g., PNG, EXR files in certain node setups)
    for img in bpy.data.images:
        if img.packed_file:
            continue
        if not img.filepath:
            continue
        # Skip generated images and sequences/movies
        if getattr(img, 'source', 'FILE') not in {'FILE', 'TILED'}:
            continue
        
        lib = getattr(img, 'library', None)
        if lib and lib.packed_file:
            raise RuntimeError(f"Batter does not support packed libraries (yet): {lib}")
        
        try:
            abspath = Path(bpy.path.abspath(img.filepath, library=lib)).resolve()
            asset_usage = AssetUsage(
                abspath=abspath,
                reference_path=img.filepath,
                is_blendfile=False,
            )
            asset_usages[lib].add(asset_usage)
        except Exception:
            pass

    # Also check sounds and movie clips for completeness
    for snd in getattr(bpy.data, 'sounds', []):
        if snd.packed_file:
            continue
        if not snd.filepath:
            continue
        
        lib = getattr(snd, 'library', None)
        if lib and lib.packed_file:
            raise RuntimeError(f"Batter does not support packed libraries (yet): {lib}")
        
        try:
            abspath = Path(bpy.path.abspath(snd.filepath, library=lib)).resolve()
            asset_usage = AssetUsage(
                abspath=abspath,
                reference_path=snd.filepath,
                is_blendfile=False,
            )
            asset_usages[lib].add(asset_usage)
        except Exception:
            pass

    for mc in getattr(bpy.data, 'movieclips', []):
        if not mc.filepath:
            continue
        
        lib = getattr(mc, 'library', None)
        if lib and lib.packed_file:
            raise RuntimeError(f"Batter does not support packed libraries (yet): {lib}")
        
        try:
            abspath = Path(bpy.path.abspath(mc.filepath, library=lib)).resolve()
            asset_usage = AssetUsage(
                abspath=abspath,
                reference_path=mc.filepath,
                is_blendfile=False,
            )
            asset_usages[lib].add(asset_usage)
        except Exception:
            pass

    return dict(asset_usages)


def _merge_keys(
    a: dict[Library | None, set[AssetUsage]],
    b: dict[Library | None, set[AssetUsage]],
) -> dict[Library | None, set[AssetUsage]]:
    merged = defaultdict(set)
    for key, values in a.items():
        merged[key].update(values)
    for key, values in b.items():
        merged[key].update(values)
    return merged
