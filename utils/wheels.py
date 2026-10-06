"""Load Python packages from bundled wheel files at runtime.

Same idea as Flamenco: ship multiple versions of a package (e.g. BAT v1 and v2) as ``.whl`` files and import the one matching the running Blender, without listing them in ``blender_manifest.toml`` (which installs wheels unconditionally under one package name).

Wheels are temporarily placed on ``sys.path`` for the import, then ``sys.path`` and ``sys.modules`` are restored so other add-ons do not keep seeing our copies.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable, Iterator

_log = logging.getLogger(__name__)

# Bundled wheels live next to the extension root (sibling of utils/).
_WHEELS_DIR = Path(__file__).resolve().parent.parent / "wheels"


def filename(
    module_name: str,
    *,
    filename_prefix: str = "",
    wheels_dir: Path | None = None,
) -> Path:
    """Return the path of the wheel file for ``module_name`` (optional prefix / directory)."""
    if not filename_prefix:
        filename_prefix = _fname_prefix_from_module_name(module_name)
    return _wheel_filename(filename_prefix, wheels_dir=wheels_dir)


def load_wheel(
    module_name: str,
    submodules: Iterable[str] = (),
    *,
    filename_prefix: str = "",
    wheels_dir: Path | None = None,
) -> list[ModuleType]:
    """Import ``module_name`` (and optional submodules) from a matching ``*.whl``.

    All requested modules are loaded in one session so inter-submodule references resolve against the same wheel. Returns ``[toplevel, *submodules]``.
    """
    wheel = filename(
        module_name, filename_prefix=filename_prefix, wheels_dir=wheels_dir
    )
    to_load = [module_name] + [f"{module_name}.{sub}" for sub in submodules]
    loaded: list[ModuleType] = []

    # Isolate the import: restore path/modules afterward so our wheel does not stay importable (or leak into other add-ons' dependency resolution).
    with _sys_path_mod_backup(wheel):
        for modname in to_load:
            try:
                module = importlib.import_module(modname)
            except ImportError as ex:
                raise ImportError(
                    f"Unable to load {modname!r} from {wheel}: {ex}"
                ) from None
            loaded.append(module)
            _log.debug("Loaded %s from %s", modname, getattr(module, "__file__", wheel))

    if len(loaded) != len(to_load):
        raise RuntimeError(
            f"expected to load {len(to_load)} modules from {wheel}, got {len(loaded)}"
        )
    return loaded


@contextlib.contextmanager
def _sys_path_mod_backup(wheel_file: Path) -> Iterator[None]:
    """Insert ``wheel_file`` onto ``sys.path`` for the duration of the context."""
    old_syspath = sys.path[:]
    old_sysmod = sys.modules.copy()
    try:
        sys.path.insert(0, str(wheel_file))
        yield
    finally:
        # Mutate in place so any external references to sys.path stay valid.
        sys.path[:] = old_syspath
        sys.modules.clear()
        sys.modules.update(old_sysmod)


def _wheel_filename(fname_prefix: str, *, wheels_dir: Path | None = None) -> Path:
    """Pick the newest wheel matching ``fname_prefix*.whl`` under ``wheels_dir`` (or BBP ``wheels/``)."""
    root = Path(wheels_dir) if wheels_dir is not None else _WHEELS_DIR
    path_pattern = f"{fname_prefix}*.whl"
    found = list(root.glob(path_pattern))
    if not found:
        raise RuntimeError(f"Unable to find wheel at {root / path_pattern}")

    # Prefer newest mtime when multiple matches exist (e.g. during local bumps).
    found.sort(key=lambda path: path.stat().st_mtime)
    return found[-1]


def _fname_prefix_from_module_name(module_name: str) -> str:
    return module_name.split(".", 1)[0]
