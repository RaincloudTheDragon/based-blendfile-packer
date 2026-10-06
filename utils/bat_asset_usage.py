"""Asset usage discovery via official Blender Asset Tracer (BAT).

Replaces the January 2026 fork of BAT v2 alpha (``batter/asset_usage.py``).

Supported Blender targets and BAT backends:

- **4.5 LTS** — BAT v1 wheel (``blender_asset_tracer-1.*.whl``; standalone blend parsing).
- **5.2 LTS** — BAT v2 wheel (``blender_asset_tracer-2.*.whl``; in-Blender ``file_usage`` API).

Both wheels ship under ``wheels/`` and are loaded at runtime (Flamenco-style), not via ``blender_manifest.toml``, so the same package name can resolve to different versions. When ``defer_to_flamenco_bat`` is on (default) and Flamenco is enabled, BBP loads BAT from Flamenco instead.

Packed datablocks are filtered locally so their filepaths are not copied or treated as missing:

- Classic ``packed_file`` (images/fonts/libs/…).
- Blender 5.0+ packed-linked IDs (``ID.is_linked_packed``) stored in archive libraries (``Library.is_archive`` / ``archive_libraries``) — Outliner “box” icon; source path may be dead while data lives in the current .blend.

Workaround until BAT v2 skips these in tracing (open upstream PR).
"""

from __future__ import annotations

import dataclasses
import functools
import importlib
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal

import bpy
from bpy.types import Library

from . import version, wheels

if TYPE_CHECKING:
    from collections.abc import Iterable

BatBackend = Literal["v1", "v2"]

# Cached module objects from load_wheel (kept after sys.modules restore).
_BAT_V1_TRACE: ModuleType | None = None
_BAT_V2_FILE_USAGE: ModuleType | None = None


@dataclasses.dataclass
class AssetUsage:
    """A single asset referenced by a blend file in the current session."""

    abspath: Path
    reference_path: str
    is_blendfile: bool

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


def get_bat_backend() -> BatBackend:
    """Return which official BAT backend is active for this Blender session."""
    return "v2" if uses_bat_v2() else "v1"


def uses_bat_v2() -> bool:
    """True when this Blender should use the BAT v2 wheel (5.1+ / 5.2 LTS)."""
    return version.uses_bat_v2_blender_version()


def uses_bat_v1() -> bool:
    """True when the BAT v1 wheel is the active backend (Blender 4.5 LTS)."""
    return not uses_bat_v2()


def _defer_to_flamenco_bat() -> bool:
    """True when the preference is on (default) so Flamenco owns BAT when present."""
    from .compat import get_addon_prefs

    prefs = get_addon_prefs()
    if prefs is None:
        return True
    return bool(getattr(prefs, "defer_to_flamenco_bat", True))


def _flamenco_root() -> Path | None:
    """Return Flamenco's addon package directory when the classic ``flamenco`` add-on is available."""
    mod = sys.modules.get("flamenco")
    if mod is None:
        try:
            if "flamenco" not in bpy.context.preferences.addons:
                return None
        except Exception:
            return None
        try:
            mod = importlib.import_module("flamenco")
        except ImportError:
            return None
    file_path = getattr(mod, "__file__", None)
    if not file_path:
        return None
    return Path(file_path).resolve().parent


def _flamenco_wheels_dir() -> Path | None:
    """Return Flamenco's ``wheels/`` dir when it contains BAT wheels."""
    root = _flamenco_root()
    if root is None:
        return None
    wheels_dir = root / "wheels"
    if not wheels_dir.is_dir():
        return None
    if not any(wheels_dir.glob("blender_asset_tracer-*.whl")):
        return None
    return wheels_dir


def _load_flamenco_v2_file_usage() -> ModuleType | None:
    """Reuse Flamenco's BAT v2 ``file_usage`` so Flamenco owns the wheel load."""
    if _flamenco_root() is None:
        return None
    try:
        # Importing submodules triggers Flamenco's loader (sets BAT_WHEEL when needed).
        submodules = importlib.import_module("flamenco.bat_v2.submodules")
    except ImportError:
        return None
    file_usage = getattr(submodules, "file_usage", None)
    if file_usage is None:
        return None
    return file_usage


def _ensure_flamenco_v1_loaded() -> None:
    """Import Flamenco's BAT v1 submodules so Flamenco owns the v1 wheel load first."""
    if _flamenco_root() is None:
        return
    try:
        importlib.import_module("flamenco.bat.submodules")
    except ImportError:
        pass


def _bat_v1_trace() -> ModuleType:
    """Return BAT v1 ``trace`` (Flamenco wheels when deferring, else BBP ``wheels/``)."""
    global _BAT_V1_TRACE
    if _BAT_V1_TRACE is None:
        wheels_dir: Path | None = None
        if _defer_to_flamenco_bat():
            wheels_dir = _flamenco_wheels_dir()
            if wheels_dir is not None:
                # Let Flamenco bind its v1 modules first; we still need ``trace`` from the same wheel set.
                _ensure_flamenco_v1_loaded()
        # Load toplevel + trace together so package-relative imports stay consistent.
        _toplevel, _BAT_V1_TRACE = wheels.load_wheel(
            "blender_asset_tracer",
            ("trace",),
            filename_prefix="blender_asset_tracer-1.",
            wheels_dir=wheels_dir,
        )
        del _toplevel
    return _BAT_V1_TRACE


def _bat_v2_file_usage() -> ModuleType:
    """Return BAT v2 ``file_usage`` (Flamenco modules when deferring, else BBP ``wheels/``)."""
    global _BAT_V2_FILE_USAGE
    if _BAT_V2_FILE_USAGE is None:
        if _defer_to_flamenco_bat():
            flamenco_fu = _load_flamenco_v2_file_usage()
            if flamenco_fu is not None:
                _BAT_V2_FILE_USAGE = flamenco_fu
                return _BAT_V2_FILE_USAGE
        _toplevel, _BAT_V2_FILE_USAGE = wheels.load_wheel(
            "blender_asset_tracer",
            ("file_usage",),
            filename_prefix="blender_asset_tracer-2.",
        )
        del _toplevel
    return _BAT_V2_FILE_USAGE


@functools.lru_cache(maxsize=1024)
def library_abspath(lib: Library | None) -> Path:
    """Absolute path of a linked library, or the current blend file when ``lib`` is None."""
    if lib is None:
        filepath = bpy.data.filepath
    else:
        filepath = bpy.path.abspath(lib.filepath)
    return Path(filepath).resolve()


def _bat_v2_project_root() -> Path:
    """Project root for BAT v2, aligned with ``library_abspath(None)`` (incl. temp override)."""
    blend_path = library_abspath(None)
    if blend_path.name:
        return blend_path.resolve().parent
    if bpy.data.filepath:
        return Path(bpy.data.filepath).resolve().parent
    return Path.cwd().resolve()


def _v2_dependency_repo():
    """Build a BAT v2 dependency repo for discovery only (skip pack-path clustering).

    ``dependencies_of_current_blendfile()`` also runs pack-path clustering, which BBP does not need for ``find()`` and which fails when the temp-blend override disagrees with ``bpy.data.filepath`` (``Could not shorten these paths: ['.']``).
    """
    bat_fu = _bat_v2_file_usage()
    root = _bat_v2_project_root()
    with bat_fu.cache_autoclear():
        deps_repo = bat_fu.FileDependencyRepository(root_path=root)
        bat_fu._determine_dependencies(deps_repo, bat_fu.Options())
    return deps_repo


def _library_for_blend_path(blend_path: Path) -> Library | None:
    """Map an on-disk blend path to a ``bpy.types.Library``, if loaded."""
    blend_path = blend_path.resolve()
    if blend_path == library_abspath(None).resolve():
        return None

    for lib in bpy.data.libraries:
        try:
            if library_abspath(lib).resolve() == blend_path:
                return lib
        except (OSError, ValueError):
            continue
    return None


def _library_is_packed(lib: Library | None) -> bool:
    """True when a library's data is already stored in the current .blend.

    Covers classic ``packed_file`` and Blender 5.0+ packed-linked archive libs (``is_archive`` / parent with ``archive_libraries``). Archive parents keep a stale source filepath even though IDs live in the archive child.
    """
    if lib is None:
        return False
    if getattr(lib, "packed_file", None) is not None:
        return True
    # Blender 5.0+: archive storage for Pack Linked Libraries
    if getattr(lib, "is_archive", False):
        return True
    archives = getattr(lib, "archive_libraries", None)
    try:
        if archives is not None and len(archives) > 0:
            return True
    except Exception:
        pass
    return False


def _id_is_linked_packed(item) -> bool:
    """True when an ID is linked-but-packed into this .blend (Blender 5.0+)."""
    return bool(getattr(item, "is_linked_packed", False))


def _path_variants(filepath: str) -> set[Path]:
    """Absolute path forms for matching BAT-reported paths to packed datablocks."""
    if not filepath or filepath in ("", "<builtin>", "<memory>"):
        return set()
    variants: set[Path] = set()
    try:
        abs_fp = bpy.path.abspath(filepath)
    except Exception:
        return set()
    if not abs_fp:
        return set()
    raw = Path(abs_fp)
    variants.add(raw)
    try:
        variants.add(raw.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return variants


def _packed_external_paths() -> set[Path]:
    """Resolved filepaths belonging to packed / packed-linked datablocks.

    Local workaround: BAT v2 still reports these paths; they must not be treated as external copy/missing targets.
    """
    packed: set[Path] = set()
    collections = [
        bpy.data.images,
        bpy.data.fonts,
        bpy.data.sounds,
        getattr(bpy.data, "movieclips", []),
        getattr(bpy.data, "volumes", []),
        bpy.data.libraries,
        bpy.data.texts,
        bpy.data.node_groups,
    ]
    for coll in collections:
        try:
            items = list(coll)
        except Exception:
            continue
        for item in items:
            has_packed_file = getattr(item, "packed_file", None) is not None
            linked_packed = _id_is_linked_packed(item)
            is_embedded_lib = isinstance(item, Library) and _library_is_packed(item)
            if not (has_packed_file or linked_packed or is_embedded_lib):
                continue
            filepath = getattr(item, "filepath", None) or ""
            # Linked-packed IDs often inherit filepath via their library
            if not filepath and getattr(item, "library", None) is not None:
                filepath = getattr(item.library, "filepath", None) or ""
            packed.update(_path_variants(filepath))
    return packed


def _filter_packed_usages(
    usages: dict[Library | None, set[AssetUsage]],
) -> dict[Library | None, set[AssetUsage]]:
    """Drop usages whose abspath matches a packed / packed-linked datablock filepath."""
    packed = _packed_external_paths()
    if not packed:
        return usages

    filtered: dict[Library | None, set[AssetUsage]] = defaultdict(set)
    skipped = 0
    for lib, items in usages.items():
        # Skip entire groups owned by archive / packed libraries
        if _library_is_packed(lib):
            skipped += len(items)
            continue
        for item in items:
            path = item.abspath
            try:
                resolved = path.resolve()
            except (OSError, RuntimeError, ValueError):
                resolved = path
            if path in packed or resolved in packed:
                skipped += 1
                continue
            filtered[lib].add(item)
    if skipped:
        print(f"[BBP BAT] Skipped {skipped} packed/packed-linked path(s) (not traced as external)")
    return dict(filtered)


def _repo_to_asset_usages(repo) -> dict[Library | None, set[AssetUsage]]:
    """Convert BAT v2 ``FileDependencyRepository`` to BBP's legacy grouping."""
    usages: dict[Library | None, set[AssetUsage]] = defaultdict(set)

    for abs_path, info in repo.file_infoes.items():
        if abs_path == repo.packed_source_file:
            continue

        is_blendfile = abs_path.suffix.lower() == ".blend"
        reference_path = str(info.reported_path or abs_path)

        if not info.references:
            usages[None].add(
                AssetUsage(
                    abspath=abs_path,
                    reference_path=reference_path,
                    is_blendfile=is_blendfile,
                )
            )
            continue

        # BAT stores references as BlendFile keys: Library | None (None = current blend).
        for blend_ref in info.references:
            if blend_ref is None or isinstance(blend_ref, Library):
                lib = blend_ref
            else:
                lib = _library_for_blend_path(Path(blend_ref))
            # Packed libraries are self-contained; don't attribute external assets to them.
            if _library_is_packed(lib):
                continue
            usages[lib].add(
                AssetUsage(
                    abspath=abs_path,
                    reference_path=reference_path,
                    is_blendfile=is_blendfile,
                )
            )

    return _filter_packed_usages(dict(usages))


def _iter_session_blend_paths() -> Iterable[tuple[Library | None, Path]]:
    """Yield each loaded blend file in the session as ``(library, abspath)``."""
    yield None, library_abspath(None)
    for lib in bpy.data.libraries:
        # Packed libs are self-contained; skip disk tracing of their filepaths.
        if _library_is_packed(lib):
            continue
        yield lib, library_abspath(lib)


def _v1_nonblend_asset_usage() -> dict[Library | None, set[AssetUsage]]:
    """Discover non-blend assets with BAT v1 file tracing (4.5 LTS path)."""
    bat_trace = _bat_v1_trace()
    usages: dict[Library | None, set[AssetUsage]] = defaultdict(set)
    packed = _packed_external_paths()

    for lib, blend_path in _iter_session_blend_paths():
        if not blend_path.exists() or blend_path.suffix.lower() != ".blend":
            continue

        seen: set[Path] = set()
        for block_usage in bat_trace.deps(blend_path):
            for asset_path in block_usage.files():
                resolved = asset_path.resolve()
                if resolved in seen or resolved.suffix.lower() == ".blend":
                    continue
                if resolved in packed or asset_path in packed:
                    continue
                seen.add(resolved)
                usages[lib].add(
                    AssetUsage(
                        abspath=resolved,
                        reference_path=str(block_usage.asset_path),
                        is_blendfile=False,
                    )
                )

    return dict(usages)


def _v2_nonblend_asset_usage() -> dict[Library | None, set[AssetUsage]]:
    """Discover assets with BAT v2 in-Blender dependency tracing (5.2 LTS path)."""
    try:
        repo = _v2_dependency_repo()
    except RuntimeError as exc:
        print(f"[BBP BAT] v2 dependency discovery failed ({exc}); falling back to v1 trace")
        return _v1_nonblend_asset_usage()

    all_usages = _repo_to_asset_usages(repo)

    nonblend: dict[Library | None, set[AssetUsage]] = defaultdict(set)
    for lib, items in all_usages.items():
        for item in items:
            if not item.is_blendfile:
                nonblend[lib].add(item)
    return dict(nonblend)


def find_blend_asset_usage() -> dict[Library | None, set[AssetUsage]]:
    """Map each blend file to the library blend files it references."""
    libs_deps: dict[Library | None, set[AssetUsage]] = defaultdict(set)

    for _id, id_users in bpy.data.user_map().items():
        id_lib = _id.library
        # Blender 5.0+ packed-linked IDs are already stored in this .blend (archive libs).
        if _id_is_linked_packed(_id):
            continue
        # Packed library datablocks / archive storage — don't require their filepath.
        if _library_is_packed(id_lib):
            continue
        libs_deps.setdefault(id_lib, set())
        for id_user in id_users:
            if id_user.library == id_lib:
                continue
            # Skip when the referencing blend is itself a packed/archive library.
            if _library_is_packed(id_user.library):
                continue

            libs_deps[id_user.library].add(
                AssetUsage(
                    abspath=library_abspath(id_lib),
                    reference_path=id_lib.filepath,
                    is_blendfile=True,
                )
            )

    return _filter_packed_usages(dict(libs_deps))


def find_nonblend_asset_usage() -> dict[Library | None, set[AssetUsage]]:
    """Map each blend file to non-blend assets it references (excludes packed)."""
    if uses_bat_v2():
        return _v2_nonblend_asset_usage()
    return _v1_nonblend_asset_usage()


def find() -> dict[Library | None, set[AssetUsage]]:
    """Return all assets used by the current blend file and its linked libraries."""
    return _merge_keys(find_blend_asset_usage(), find_nonblend_asset_usage())


def _merge_keys(
    a: dict[Library | None, set[AssetUsage]],
    b: dict[Library | None, set[AssetUsage]],
) -> dict[Library | None, set[AssetUsage]]:
    merged: dict[Library | None, set[AssetUsage]] = defaultdict(set)
    for key, values in a.items():
        merged[key].update(values)
    for key, values in b.items():
        merged[key].update(values)
    return dict(merged)
