"""
Packing operations for BasedBlendfilePacker.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Callable, Optional, Tuple

import bpy
from bpy.types import Operator
from bpy.props import EnumProperty

from .. import config
from ..utils import bat_asset_usage as au
from ..utils import wm_progress
from .export_ops import _MEDIA_EXTENSIONS

# UDIM tile filenames (e.g. foo.1001.png) and Blender's <UDIM> token.
_UDIM_TILE_NAME_RE = re.compile(r"\.(10\d{2})\.[A-Za-z0-9]+$")


class WorkflowMode:
    """Workflow mode constants."""
    COPY_ONLY = "copy-only"
    PACK_AND_SAVE = "pack-and-save"


def _verbose_pack_log_enabled() -> bool:
    """True when Preferences → Verbose Pack Log is on (default True)."""
    try:
        from ..ui.preferences_ui import BBP_AddonPreferences
        addon = bpy.context.preferences.addons.get(BBP_AddonPreferences.bl_idname)
        if addon and addon.preferences:
            return bool(getattr(addon.preferences, "verbose_pack_log", True))
    except Exception:
        pass
    return True


def _pack_diag(message: str, *, verbose_only: bool = False) -> None:
    """Pack diagnostic line; verbose_only lines need the prefs toggle."""
    if verbose_only and not _verbose_pack_log_enabled():
        return
    print(f"[BBP Pack] DIAG: {message}")


def _pack_debug(message: str) -> None:
    """Modal/operator chatter; only when Verbose Pack Log is on."""
    if not _verbose_pack_log_enabled():
        return
    print(f"[BBP Pack] DEBUG: {message}")


def _format_pack_duration(seconds: float) -> str:
    """Human-readable duration for pack timing logs / operator reports."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60.0)
    if m < 60:
        return f"{int(m)}m {s:.1f}s"
    h, m = divmod(int(m), 60)
    return f"{h}h {m}m {s:.0f}s"


def _report_pack_duration(t0: Optional[float], *, cancelled: bool = False) -> str:
    """Print total pack wall time; return the short duration string (or empty if no start)."""
    if t0 is None:
        return ""
    elapsed = time.perf_counter() - t0
    dur = _format_pack_duration(elapsed)
    label = "cancelled" if cancelled else "finished"
    print(f"[BBP Pack] Pack {label} in {dur} ({elapsed:.2f}s)")
    return dur


def _pack_indicator_from_dir(pack_dir: Optional[Path]) -> str:
    """Token from temp pack dir name (``bbp_pack_<token>`` → ``<token>``)."""
    if pack_dir is None:
        return ""
    name = Path(pack_dir).name
    if name.startswith("bbp_pack_"):
        return name[len("bbp_pack_"):]
    return name


def _unique_pack_output_path(output_dir: Path, stem: str, suffix: str, pack_dir: Optional[Path]) -> Path:
    """Prefer ``stem.suffix``; on name conflict use ``stem_<pack_indicator>.suffix`` (same token as the tmp pack dir)."""
    desired = Path(output_dir) / f"{stem}{suffix}"
    if not desired.exists():
        return desired
    indicator = _pack_indicator_from_dir(pack_dir) or f"{os.getpid():x}{time.time_ns() & 0xFFFFFF:x}"
    return Path(output_dir) / f"{stem}_{indicator}{suffix}"


def _path_looks_udim(path: Path) -> bool:
    """True when path is a UDIM token path or a concrete .10xx tile filename."""
    name = path.name if path else ""
    s = str(path)
    return "<UDIM>" in s.upper() or "<udim>" in s or bool(_UDIM_TILE_NAME_RE.search(name))


def _udim_family_key(path: Path) -> Optional[str]:
    """Stem family for foo.1001.png / foo.<UDIM>.png; None when not UDIM-shaped."""
    name = path.name if path else ""
    if not name:
        return None
    if "<UDIM>" in name.upper() or "<udim>" in name:
        return re.split(r"\.<UDIM>|\.<udim>", name, maxsplit=1, flags=re.IGNORECASE)[0].lower()
    m = _UDIM_TILE_NAME_RE.search(name)
    if m:
        return name[: m.start()].lower()
    return None


def _session_udim_families() -> set[str]:
    """UDIM stem families owned by live Image datablocks in this session."""
    fams: set[str] = set()
    for img in getattr(bpy.data, "images", []) or []:
        fp = getattr(img, "filepath", None) or ""
        if fp:
            try:
                p = Path(bpy.path.abspath(fp))
            except Exception:
                p = Path(fp)
            key = _udim_family_key(p)
            if key:
                fams.add(key)
            elif getattr(img, "source", "") == "TILED":
                fams.add(p.stem.lower())
        elif getattr(img, "source", "") == "TILED":
            fams.add((img.name or "").lower())
    return fams


def _is_ignorable_missing_asset(path: Path) -> bool:
    """False positives: pack-linked/archive origin libs, and BAT-only sparse/phantom UDIM tiles."""
    try:
        p = Path(path)
    except Exception:
        return False
    if not p.name:
        return False
    # Blender 5.0+ pack-linked origin .blend (any asset type) — bytes already in this file; path is only an origin marker.
    try:
        if au.path_is_embedded_library(p):
            return True
    except Exception:
        pass

    fam = _udim_family_key(p)
    if fam is None:
        return False
    session_fams = _session_udim_families()
    if fam not in session_fams:
        # Nested-blend DNA path with no live Image — BAT phantom.
        return True
    # Session owns this UDIM family: absent .10xx tiles are sparse (Report Missing Files stays clean).
    for img in getattr(bpy.data, "images", []) or []:
        fp = getattr(img, "filepath", None) or ""
        img_fam = None
        if fp:
            try:
                img_fam = _udim_family_key(Path(bpy.path.abspath(fp)))
            except Exception:
                img_fam = _udim_family_key(Path(fp))
        if img_fam != fam and (img.name or "").lower() != fam:
            continue
        if getattr(img, "packed_file", None) or getattr(img, "is_linked_packed", False):
            return True
        if getattr(img, "has_data", False) or (getattr(img, "size", [0]) or [0])[0]:
            return True
        if not getattr(img, "is_missing", False):
            return True
    return False


def _filter_ignorable_missing(missing: list) -> list:
    """Drop pack-linked origin-lib / sparse-UDIM false positives from a missing-path list."""
    if not missing:
        return missing
    ignored_udim_fams: set[str] = set()
    kept: list = []
    for item in missing:
        try:
            p = Path(item)
        except Exception:
            kept.append(item)
            continue
        if _is_ignorable_missing_asset(p):
            fam = _udim_family_key(p)
            if fam:
                ignored_udim_fams.add(fam)
            _pack_diag(f"Ignoring false-positive missing: {p.name}", verbose_only=True)
            continue
        kept.append(item)
    if not ignored_udim_fams:
        return kept
    # BAT often also lists the bare UDIM stem (no .10xx.ext) next to the tile paths.
    out: list = []
    for item in kept:
        try:
            p = Path(item)
        except Exception:
            out.append(item)
            continue
        if not p.suffix:
            low = p.name.lower()
            if any(low == fam or low.startswith(fam) or fam.startswith(low) for fam in ignored_udim_fams):
                _pack_diag(f"Ignoring false-positive missing UDIM stem: {p.name}", verbose_only=True)
                continue
        out.append(item)
    return out


def _diag_summarize_asset_usages(asset_usages: dict) -> None:
    """Log BAT discovery stats: per-lib counts, suffixes, UDIM-looking paths."""
    by_suffix: Counter = Counter()
    udim_paths: list[Path] = []
    total = 0
    missing_disk = 0
    for lib, links in asset_usages.items():
        lib_label = "MAIN" if lib is None else getattr(lib, "name", str(lib))
        n = len(links)
        total += n
        exist_n = sum(1 for a in links if a.abspath.exists())
        _pack_diag(f"BAT lib {lib_label}: {n} asset(s), {exist_n} on disk", verbose_only=True)
        for a in links:
            by_suffix[a.abspath.suffix.lower() or "(none)"] += 1
            if _path_looks_udim(a.abspath):
                udim_paths.append(a.abspath)
            if not a.abspath.exists():
                missing_disk += 1
    top_suffixes = ", ".join(f"{s}:{c}" for s, c in by_suffix.most_common(12))
    _pack_diag(f"BAT total: {total} path(s) across {len(asset_usages)} lib group(s); missing on disk at discover: {missing_disk}")
    _pack_diag(f"BAT suffixes: {top_suffixes}", verbose_only=True)
    _pack_diag(f"BAT UDIM-looking paths: {len(udim_paths)}")
    for p in udim_paths[:20]:
        _pack_diag(f"  UDIM/BAT: {p}", verbose_only=True)
    if len(udim_paths) > 20:
        _pack_diag(f"  ... and {len(udim_paths) - 20} more UDIM-looking BAT paths", verbose_only=True)


def _udim_tile_paths(abs_fp: Path, tile_numbers: list) -> list[Path]:
    """Expand a tokenized or concrete image path into per-tile file paths."""
    out: list[Path] = []
    name = abs_fp.name
    for n in tile_numbers:
        if n is None:
            continue
        if "<UDIM>" in name or "<udim>" in name:
            out.append(abs_fp.parent / name.replace("<UDIM>", str(n)).replace("<udim>", str(n)))
            continue
        m = re.match(r"^(.*)\.(10\d{2})(\.[^.]+)$", name)
        if m:
            out.append(abs_fp.parent / f"{m.group(1)}.{n}{m.group(3)}")
        else:
            # name.<UDIM>.ext style without angle brackets already handled; fallback stem.N.ext
            stem = abs_fp.stem
            out.append(abs_fp.parent / f"{stem}.{n}{abs_fp.suffix}")
    return out


def _diag_session_udim_gap(asset_usages: dict) -> None:
    """Compare session TILED/<UDIM> images vs what BAT listed (gap = pack_linked risk)."""
    bat_names = {a.abspath.name.lower() for links in asset_usages.values() for a in links}
    bat_resolved = set()
    for links in asset_usages.values():
        for a in links:
            try:
                bat_resolved.add(a.abspath.resolve())
            except OSError:
                bat_resolved.add(a.abspath)

    tiled = []
    for img in bpy.data.images:
        fp = getattr(img, "filepath", None) or ""
        if getattr(img, "source", "") != "TILED" and "<UDIM>" not in fp.upper():
            continue
        try:
            abs_fp = Path(bpy.path.abspath(fp)) if fp else Path()
        except Exception:
            abs_fp = Path(fp)
        tiles = [getattr(t, "number", None) for t in (getattr(img, "tiles", None) or [])]
        tile_files = _udim_tile_paths(abs_fp, tiles) if tiles and fp else []
        try:
            in_bat = abs_fp.resolve() in bat_resolved
        except OSError:
            in_bat = abs_fp.name.lower() in bat_names
        tiles_on_disk = sum(1 for t in tile_files if t.exists())
        tiles_in_bat = 0
        for t in tile_files:
            try:
                if t.name.lower() in bat_names or (t.exists() and t.resolve() in bat_resolved):
                    tiles_in_bat += 1
            except OSError:
                if t.name.lower() in bat_names:
                    tiles_in_bat += 1
        lib = img.library.filepath if img.library else "(local)"
        tiled.append({
            "name": img.name,
            "lib": lib,
            "tiles": tiles,
            "tiles_on_disk": tiles_on_disk,
            "tiles_in_bat": tiles_in_bat,
            "in_bat": in_bat,
            "tile_files": tile_files,
        })

    _pack_diag(f"Session TILED/<UDIM> images: {len(tiled)}")
    gap = [t for t in tiled if t["tiles_on_disk"] > t["tiles_in_bat"]]
    _pack_diag(f"UDIM gap (tiles on disk but not in BAT list): {len(gap)} image(s) — these break pack_linked/pack_all")
    for t in tiled:
        _pack_diag(
            f"  TILED '{t['name']}' lib={t['lib']} tiles={t['tiles']} "
            f"on_disk={t['tiles_on_disk']} in_bat={t['tiles_in_bat']} token_in_bat={t['in_bat']}",
            verbose_only=True,
        )
        if t["tiles_on_disk"] > t["tiles_in_bat"]:
            for tf in t["tile_files"][:8]:
                _pack_diag(f"    tile {tf.name}: exists={tf.exists()} in_bat={tf.name.lower() in bat_names}", verbose_only=True)


def _diag_copied_udim_tiles(target_path: Path) -> None:
    """Count concrete UDIM tile files that landed in the pack tree."""
    if not target_path or not target_path.exists():
        return
    n = 0
    samples = []
    try:
        for p in target_path.rglob("*"):
            if p.is_file() and _UDIM_TILE_NAME_RE.search(p.name):
                n += 1
                if len(samples) < 12:
                    samples.append(p)
    except OSError as e:
        _pack_diag(f"UDIM tile scan failed: {e}")
        return
    _pack_diag(f"Pack tree concrete UDIM tiles (.10xx.*): {n}")
    for p in samples:
        _pack_diag(f"  tile packed: {p.relative_to(target_path)}", verbose_only=True)


def _is_excluded_media_path(path: Path, exclude_av: bool) -> bool:
    """True when path is video/audio and exclude_av is enabled."""
    return bool(exclude_av) and path.suffix.lower() in _MEDIA_EXTENSIONS


def _unique_missing_names(missing: list) -> list[str]:
    """Dedupe missing paths to display names (preserve order)."""
    names = []
    seen = set()
    for p in missing:
        name = Path(p).name if p else str(p)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _blender_subprocess_timeout() -> int:
    """Hard cap for Blender --python / --python-expr pack subprocesses (config)."""
    return int(getattr(config, "BLENDER_SUBPROCESS_TIMEOUT_SEC", getattr(config, "PACK_LINKED_TIMEOUT_SEC", 15)))


def _log_missing_assets_summary(missing: list) -> str:
    """Print Flamenco-style offline-files summary; return a UI message with basenames (or "")."""
    missing = _filter_ignorable_missing(missing)
    names = _unique_missing_names(missing)
    if not names:
        return ""
    print(f"[BBP Pack] Pack completed with {len(names)} missing/offline file(s):")
    for name in names[:30]:
        print(f"[BBP Pack]   - {name}")
    if len(names) > 30:
        print(f"[BBP Pack]   ... and {len(names) - 30} more")
    print("[BBP Pack] Review the list — remap or remove in source blends if needed.")
    # Operator report: one basename per line so the Info log stays scannable.
    return f"{len(names)} missing/offline:\n" + "\n".join(names)


def _format_pack_linked_failure_report(failed_blends: list) -> str:
    """User-facing ERROR text when pack_linked timed out or exited non-zero."""
    names = []
    seen = set()
    for p in failed_blends or []:
        name = Path(p).name if p else str(p)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        return ""
    listed = ", ".join(names[:8])
    more = f" (+{len(names) - 8} more)" if len(names) > 8 else ""
    print(f"[BBP Pack] ERROR: Pack Linked FAILED for {len(names)} blend(s): {listed}{more}")
    print("[BBP Pack]   This is either a project setup issue (broken/absolute/offline libs) or a BBP gap — do not treat this pack as farm-ready.")
    return (
        f"Pack Linked FAILED ({len(names)}): {listed}{more}. "
        "Project setup issue or BBP gap — pack is not farm-ready."
    )


class DeadUncAssetError(RuntimeError):
    """Raised when an inaccessible network/UNC path blocks packing."""

    def __init__(self, path: str, kind: str = "", name: str = "", detail: str = ""):
        self.path = path
        self.kind = kind
        self.name = name
        self.detail = detail
        lines = ["Pack aborted: inaccessible network path (dead UNC)."]
        if path:
            lines.append(f"  Path: {path}")
        if kind or name:
            used = f"{kind} '{name}'".strip() if kind else f"'{name}'"
            lines.append(f"  Used by: {used}")
        lines.append("Fix or remove this link before packing.")
        if detail:
            lines.append(f"  ({detail})")
        super().__init__("\n".join(lines))


def find_dead_unc_assets() -> list[tuple[str, str, str, str]]:
    """Scan the open blend for filepaths that fail OS resolve (dead UNC/network).

    Packed datablocks are skipped — embedded data is self-contained even if the original filepath points at a dead share.

    Returns list of (kind, datablock_name, absolute_path, error_detail).
    """
    dead: list[tuple[str, str, str, str]] = []
    checks = [
        ("Image", bpy.data.images),
        ("Font", bpy.data.fonts),
        ("Sound", bpy.data.sounds),
        ("MovieClip", getattr(bpy.data, "movieclips", [])),
        ("Volume", getattr(bpy.data, "volumes", [])),
        ("Library", bpy.data.libraries),
    ]
    for kind, coll in checks:
        try:
            items = list(coll)
        except Exception:
            continue
        for item in items:
            # Packed = data is embedded; stale/dead filepath is not a pack blocker
            if getattr(item, "packed_file", None) is not None:
                continue
            fp = getattr(item, "filepath", None) or ""
            if not fp or fp in ("", "<builtin>", "<memory>"):
                continue
            try:
                abs_fp = bpy.path.abspath(fp)
            except Exception:
                abs_fp = fp
            try:
                Path(abs_fp).resolve()
            except OSError as e:
                dead.append((kind, item.name, abs_fp, str(e)))
    return dead


def _extract_dead_unc_from_output(stdout: str, stderr: str) -> Optional[tuple[str, str, str, str]]:
    """Parse BBP_FATAL_DEAD_UNC markers (or WinError 1272) from Blender script output."""
    import re
    combined = (stdout or "") + "\n" + (stderr or "")
    path = kind = name = detail = ""
    for line in combined.splitlines():
        if line.startswith("BBP_FATAL_DEAD_UNC:"):
            path = line.split(":", 1)[1].strip()
        elif line.startswith("BBP_FATAL_DEAD_UNC_KIND:"):
            kind = line.split(":", 1)[1].strip()
        elif line.startswith("BBP_FATAL_DEAD_UNC_NAME:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("BBP_FATAL_DEAD_UNC_ERR:"):
            detail = line.split(":", 1)[1].strip()
    if path:
        return path, kind, name, detail
    # Fallback: WinError 1272 / guest-access block embeds the UNC in the message
    m = re.search(
        r"OSError:\s*\[WinError\s+1272\].*?:\s*'([^']+)'",
        combined,
        re.DOTALL,
    )
    if m:
        return m.group(1), "", "", "WinError 1272"
    if "WinError 1272" in combined or "unauthenticated guest access" in combined:
        m2 = re.search(r"'(\\\\[^']+)'", combined)
        return (m2.group(1) if m2 else "(unknown UNC path)"), "", "", "WinError 1272"
    return None


def compute_target_relpath(abs_path: Path, base_root: Path) -> Path:
    """Return a stable relative path under the target, even if outside root."""
    try:
        return abs_path.relative_to(base_root)
    except Exception:
        anchor = abs_path.anchor
        if os.name == "nt":
            if anchor.startswith("\\\\"):
                parts = anchor.strip("\\").split("\\")
                label = "UNC_" + "_".join(parts[:2]) if len(parts) >= 2 else "UNC"
            elif len(anchor) >= 2 and anchor[1] == ":":
                label = f"DRIVE_{anchor[0].upper()}"
            else:
                label = "ROOT"
        else:
            label = "ROOT"
        rel_after_anchor = str(abs_path)[len(anchor):].lstrip("\\/")
        return Path(label) / Path(rel_after_anchor)


def _norm_copy_map_key(path) -> str:
    """Normalize path for copy_map key; must match remap script norm_key."""
    try:
        s = str(Path(path).resolve())
    except OSError:
        s = os.path.normpath(str(path))
    if os.name == "nt":
        return s.lower().replace("\\", "/")
    return s


def _copy_map_register(copy_map: dict, stale_path: Path, resolved_src: Path, dest: Path) -> None:
    """Map both the stale stored path and the live source path to the packed destination."""
    dest_s = str(Path(dest).resolve())
    copy_map[_norm_copy_map_key(resolved_src)] = dest_s
    if Path(stale_path) != Path(resolved_src):
        copy_map[_norm_copy_map_key(stale_path)] = dest_s


# Bound stale-path walks so remap/copy cannot hang on huge studio trees (remap 300s timeout).
_RECOVERY_MAX_UP = 6
_RECOVERY_MAX_DEPTH = 5
_RECOVERY_MAX_DIRS = 800
_RECOVERY_MAX_HITS = 12


def collect_search_roots(common_root: Optional[Path], existing_paths: list, blend_parent: Optional[Path] = None) -> list[Path]:
    """Build session-derived roots for stale-path recovery (no studio-hardcoded aliases).

    Uses ancestors of live assets (capped) instead of a drive-wide commonpath — unbounded common roots made rglob hang until remap timed out.
    """
    roots: list[Path] = []
    seen: set[str] = set()

    def _add(p: Optional[Path]) -> None:
        if p is None:
            return
        try:
            rp = Path(p)
            if not rp.exists():
                return
            root = rp if rp.is_dir() else rp.parent
            # Skip drive / UNC share roots (too large to walk).
            if root == root.parent or (len(root.parts) <= 1):
                return
            if os.name == "nt" and len(root.parts) <= 2 and str(root).startswith("\\\\"):
                return
            key = _norm_copy_map_key(root)
            if key in seen:
                return
            seen.add(key)
            roots.append(root)
        except OSError:
            return

    _add(common_root)
    _add(blend_parent)
    for p in existing_paths:
        try:
            cur = Path(p).resolve()
            if not cur.exists():
                continue
            cur = cur.parent
            for _ in range(_RECOVERY_MAX_UP):
                _add(cur)
                parent = cur.parent
                if parent == cur:
                    break
                cur = parent
        except OSError:
            continue
    return roots


def _path_suffix_score(stale: Path, hit: Path) -> int:
    """Count matching trailing path parts (case-insensitive)."""
    sp = [x.lower() for x in Path(stale).parts]
    hp = [x.lower() for x in Path(hit).parts]
    score = 0
    for a, b in zip(reversed(sp), reversed(hp)):
        if a != b:
            break
        score += 1
    return score


def _resolve_blender_datafiles_asset(stale: Path) -> Optional[Path]:
    """Map stale Blender-install datafiles/assets paths to the running Blender's DATAFILES tree."""
    name = stale.name
    if not name.lower().endswith(".blend"):
        return None
    parts = [p.lower() for p in Path(stale).parts]
    if "datafiles" not in parts or "assets" not in parts:
        return None
    try:
        df = Path(bpy.utils.system_resource("DATAFILES"))
        # Prefer the same relative layout under assets/ (nodes/, brushes/, …).
        try:
            idx = parts.index("assets")
            rel = Path(*Path(stale).parts[idx + 1 :])
            cand = df / "assets" / rel
            if cand.is_file():
                return cand.resolve()
        except (ValueError, OSError):
            pass
        # Fallback: basename search one level under assets/*
        assets = df / "assets"
        if assets.is_dir():
            for sub in assets.iterdir():
                if not sub.is_dir():
                    continue
                cand = sub / name
                if cand.is_file():
                    return cand.resolve()
    except Exception:
        pass
    return None


def _suffix_graft_hits(stale: Path, search_roots: list[Path]) -> list[Path]:
    """Try root / trailing-suffix candidates (no directory walk)."""
    parts = Path(stale).parts
    if not parts:
        return []
    hits: list[Path] = []
    seen: set[str] = set()
    # Longest suffix first (skip empty / single-drive prefixes).
    for i in range(len(parts)):
        suffix_parts = parts[i:]
        if not suffix_parts:
            continue
        # Need at least basename; prefer basename+parent when possible.
        for root in search_roots:
            try:
                cand = root.joinpath(*suffix_parts)
                if cand.is_file():
                    key = _norm_copy_map_key(cand)
                    if key not in seen:
                        seen.add(key)
                        hits.append(cand.resolve())
            except OSError:
                continue
    return hits


def _bounded_basename_hits(name: str, search_roots: list[Path]) -> list[Path]:
    """Find basename under search roots with hard caps (avoids remap timeout)."""
    hits: list[Path] = []
    seen: set[str] = set()
    for root in search_roots:
        try:
            if not root.is_dir():
                continue
            root_depth = len(root.parts)
            dirs_seen = 0
            for dirpath, dirnames, filenames in os.walk(root):
                dirs_seen += 1
                if dirs_seen > _RECOVERY_MAX_DIRS:
                    dirnames.clear()
                    break
                depth = len(Path(dirpath).parts) - root_depth
                if depth >= _RECOVERY_MAX_DEPTH:
                    dirnames.clear()
                    continue
                if name in filenames:
                    hit = Path(dirpath) / name
                    try:
                        if hit.is_file():
                            key = _norm_copy_map_key(hit)
                            if key not in seen:
                                seen.add(key)
                                hits.append(hit.resolve())
                                if len(hits) >= _RECOVERY_MAX_HITS:
                                    return hits
                    except OSError:
                        continue
        except OSError:
            continue
    return hits


def recover_stale_path(
    stale: Path,
    search_roots: list[Path],
    cache: Optional[dict] = None,
) -> Optional[Path]:
    """Resolve a dead stored filepath to a live twin under session search roots.

    Prefers suffix graft, then longest shared trailing suffix among bounded basename hits; otherwise a unique basename hit.
    """
    try:
        stale_p = Path(stale)
        if stale_p.is_file():
            return stale_p.resolve()
    except OSError:
        stale_p = Path(stale)

    df_hit = _resolve_blender_datafiles_asset(stale_p)
    if df_hit is not None:
        print(f"[BBP Pack]   Recovered stale path: {stale_p} -> {df_hit}")
        return df_hit

    name = stale_p.name
    if not name or not search_roots:
        return None

    # Instant: graft trailing suffixes onto roots (handles same relative layout).
    grafted = _suffix_graft_hits(stale_p, search_roots)
    if len(grafted) == 1:
        print(f"[BBP Pack]   Recovered stale path: {stale_p} -> {grafted[0]}")
        return grafted[0]
    if grafted:
        scored_g = sorted(((_path_suffix_score(stale_p, h), h) for h in grafted), key=lambda t: (-t[0], str(t[1]).lower()))
        if scored_g[0][0] >= 2:
            tied = [h for s, h in scored_g if s == scored_g[0][0]]
            if len(tied) == 1:
                print(f"[BBP Pack]   Recovered stale path: {stale_p} -> {tied[0]}")
                return tied[0]

    cache = cache if cache is not None else {}
    if name not in cache:
        cache[name] = _bounded_basename_hits(name, search_roots)
    hits = cache[name]
    if not hits:
        return None

    scored = sorted(((_path_suffix_score(stale_p, h), h) for h in hits), key=lambda t: (-t[0], str(t[1]).lower()))
    best_score, best = scored[0]
    # Prefer a clear suffix match (basename + at least one parent), unique at that score.
    if best_score >= 2:
        tied = [h for s, h in scored if s == best_score]
        if len(tied) == 1:
            print(f"[BBP Pack]   Recovered stale path: {stale_p} -> {best}")
            return best
    if len(hits) == 1:
        print(f"[BBP Pack]   Recovered stale path: {stale_p} -> {hits[0]}")
        return hits[0]
    return None


def _iter_session_point_caches():
    """Yield PointCache blocks from the open session (cloth, soft body, particles, dynamic paint, rigid body)."""
    for obj in bpy.data.objects:
        for mod in getattr(obj, "modifiers", []) or []:
            ps = getattr(mod, "particle_system", None)
            if ps is not None:
                pc = getattr(ps, "point_cache", None)
                if pc is not None:
                    yield pc
            pc = getattr(mod, "point_cache", None)
            if pc is not None:
                yield pc
            if getattr(mod, "type", "") == "DYNAMIC_PAINT":
                canvas = getattr(mod, "canvas_settings", None)
                for surf in getattr(canvas, "canvas_surfaces", None) or []:
                    pc = getattr(surf, "point_cache", None)
                    if pc is not None:
                        yield pc
    for scene in bpy.data.scenes:
        rbw = getattr(scene, "rigidbody_world", None)
        if rbw is None:
            continue
        pc = getattr(rbw, "point_cache", None)
        if pc is not None:
            yield pc


def _session_expects_blendcache(src_blend: Path) -> bool:
    """True when open-session sims use a disk cache that targets ``blendcache_<stem>`` for *src_blend*.

    Default disk caches (empty filepath + ``use_disk_cache``) live next to the .blend. External filepaths are matched when they resolve to that folder. Only consulted when the open file stem matches *src_blend* (hero / temp export); closed nested blends are not probed.
    """
    try:
        stem = src_blend.stem
        expected_name = f"blendcache_{stem}"
        expected = (src_blend.parent / expected_name)
    except Exception:
        return False
    try:
        open_fp = bpy.data.filepath or ""
        if not open_fp or Path(open_fp).stem != stem:
            return False
    except Exception:
        return False
    try:
        expected_resolved = expected.resolve()
    except Exception:
        expected_resolved = expected
    for pc in _iter_session_point_caches():
        use_disk = bool(getattr(pc, "use_disk_cache", False))
        use_ext = bool(getattr(pc, "use_external", False))
        if not use_disk and not use_ext:
            continue
        fp = getattr(pc, "filepath", None) or ""
        if not fp or fp == "//":
            # Blender's default disk-cache dir for this .blend.
            if use_disk:
                return True
            continue
        try:
            abs_fp = Path(bpy.path.abspath(fp))
            try:
                resolved = abs_fp.resolve()
            except Exception:
                resolved = abs_fp
            if resolved == expected_resolved or resolved.name == expected_name:
                return True
            if expected_name in {p.name for p in resolved.parents}:
                return True
        except Exception:
            continue
    return False


def copy_blend_caches(src_blend: Path, dst_blend: Path, missing_on_copy: list, 
                      frame_start: Optional[int] = None, frame_end: Optional[int] = None, 
                      frame_step: Optional[int] = None,
                      copy_map_out: Optional[dict] = None) -> list[Path]:
    """Copy common cache folders for a given .blend next to its target copy.

    If frame range parameters are provided, only copies cache files within that range. Otherwise, copies all cache files. On Windows we use robocopy when frame filtering; source path is kept as given (e.g. P:\\) so mapped drives work instead of resolving to UNC.
    """
    import re
    import subprocess as _sub
    copied = []
    # Keep source path as-is on Windows so P:\\ stays P:\\ (resolve can turn it into UNC and break robocopy)
    if os.name != "nt":
        src_blend = src_blend.resolve()
    dst_blend = dst_blend.resolve()
    filter_by_frame = frame_start is not None and frame_end is not None and frame_step is not None
    valid_frames = None
    if filter_by_frame:
        valid_frames = set(range(frame_start, frame_end + 1, frame_step))

    def _frame_from_stem(stem: str) -> Optional[int]:
        """Extract frame number from cache filename (matches truncate patterns)."""
        match = re.search(r'_(\d+)_\d+$', stem)  # Blender bphys: name_frame_index
        if match:
            return int(match.group(1))
        match = re.search(r'(?:frame_|cache[^_]*_)(\d+)', stem, re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.search(r'(?:fluid_|cloth_|softbody_|particles_|pointcache_|sim_)(\d+)', stem, re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.search(r'(\d+)$', stem)
        if match:
            return int(match.group(1))
        return None

    def should_copy_file(file_path: Path) -> bool:
        if not filter_by_frame:
            return True
        if not file_path.is_file():
            return True
        frame_num = _frame_from_stem(file_path.stem)
        if frame_num is None:
            return True
        return frame_num in valid_frames

    def copy_tree_filtered(src: Path, dst: Path):
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            src_item = src / item.name
            dst_item = dst / item.name
            if src_item.is_dir():
                copy_tree_filtered(src_item, dst_item)
            elif src_item.is_file() and should_copy_file(src_item):
                shutil.copy2(src_item, dst_item)

    def _dst_has_files(p: Path) -> bool:
        """True if directory exists and contains at least one file (quick check)."""
        try:
            if not p.exists() or not p.is_dir():
                return False
            for _ in p.rglob("*"):
                return True  # at least one entry
            return False
        except Exception:
            return False

    try:
        src_parent = src_blend.parent
        dst_parent = dst_blend.parent
        blendname = src_blend.stem
        # Existing cache dirs only. Missing blendcache_ is reported separately when hero sims expect it.
        candidates: list[tuple[Path, Path]] = []
        blendcache_src = src_parent / f"blendcache_{blendname}"
        if blendcache_src.exists() and blendcache_src.is_dir():
            candidates.append((blendcache_src, dst_parent / f"blendcache_{blendname}"))
        elif _session_expects_blendcache(src_blend):
            missing_on_copy.append(blendcache_src)
            print(f"[BBP Pack]   WARNING: Expected blendcache missing (disk-cache sims present): {blendcache_src}")
        bakes_src = src_parent / "bakes" / blendname
        if bakes_src.exists() and bakes_src.is_dir():
            candidates.append((bakes_src, dst_parent / "bakes" / blendname))
        try:
            for entry in src_parent.iterdir():
                if entry.is_dir() and entry.name.startswith("cache_"):
                    candidates.append((entry, dst_parent / entry.name))
        except Exception:
            pass

        def _add_cache_dir_to_map(sdir: Path, ddir: Path):
            if copy_map_out is not None:
                copy_map_out[_norm_copy_map_key(sdir)] = str(ddir.resolve())

        for src_dir, dst_dir in candidates:
            # Keep Windows drive letters (e.g. A:\) — resolve can turn them into UNC and break robocopy.
            if os.name != "nt":
                src_dir = src_dir.resolve()
            dst_dir = dst_dir.resolve()
            try:
                # Candidates are existence-gated above; skip if the dir vanished mid-pack.
                if not src_dir.exists() or not src_dir.is_dir():
                    continue
                # On Windows with frame filter: try Python copy first; if 0 files or PermissionError, use robocopy
                if filter_by_frame and os.name == "nt":
                    def _try_robocopy():
                        robocopy_exe = (getattr(shutil, "which", lambda x: None)("robocopy")
                            or os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "robocopy.exe")
                            or "robocopy")
                        src_str = str(src_dir)
                        dst_str = str(dst_dir)
                        print(f"[BBP Pack]   robocopy: {src_str} -> {dst_str}")
                        cmd = f'"{robocopy_exe}" "{src_str}" "{dst_str}" /E /R:2 /W:1 /NFL /NDL /NJH /NJS'
                        rc = _sub.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
                        print(f"[BBP Pack]   robocopy exit code: {rc.returncode}")
                        return rc.returncode
                    dst_dir.parent.mkdir(parents=True, exist_ok=True)
                    if dst_dir.exists():
                        try:
                            shutil.rmtree(dst_dir)
                        except Exception:
                            pass
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    used_robocopy = False
                    try:
                        try:
                            src_count = sum(1 for _ in src_dir.rglob("*"))
                        except Exception:
                            src_count = "?"
                        print(f"[BBP Pack]   {src_dir.name}: exists=True, items={src_count}")
                        copy_tree_filtered(src_dir, dst_dir)
                    except PermissionError:
                        used_robocopy = True
                        rc = _try_robocopy()
                        if rc >= 8:
                            missing_on_copy.append(src_dir)
                            if dst_dir.exists() and not _dst_has_files(dst_dir):
                                try:
                                    shutil.rmtree(dst_dir)
                                except Exception:
                                    pass
                            continue
                    except Exception as e:
                        print(f"[BBP Pack]   WARNING: cache copy failed for {src_dir.name}: {e}")
                        used_robocopy = True
                        rc = _try_robocopy()
                        if rc >= 8 or not _dst_has_files(dst_dir):
                            missing_on_copy.append(src_dir)
                            if dst_dir.exists() and not _dst_has_files(dst_dir):
                                try:
                                    shutil.rmtree(dst_dir)
                                except Exception:
                                    pass
                            continue
                    if not _dst_has_files(dst_dir) and not used_robocopy:
                        print(f"[BBP Pack]   {src_dir.name}: Python copy produced 0 files, trying robocopy")
                        used_robocopy = True
                        rc = _try_robocopy()
                        if rc >= 8 or not _dst_has_files(dst_dir):
                            if dst_dir.exists() and not _dst_has_files(dst_dir):
                                try:
                                    shutil.rmtree(dst_dir)
                                except Exception:
                                    pass
                            continue
                    if _dst_has_files(dst_dir):
                        n_before = sum(1 for _ in dst_dir.rglob("*") if _.is_file())
                        truncate_caches_to_frame_range(dst_dir, frame_start, frame_end, frame_step)
                        if _dst_has_files(dst_dir):
                            n_after = sum(1 for _ in dst_dir.rglob("*") if _.is_file())
                            print(f"[BBP Pack]   {dst_dir.name}: {n_before} files before truncate, {n_after} after")
                            _add_cache_dir_to_map(src_dir, dst_dir)
                            copied.append(dst_dir)
                        else:
                            print(f"[BBP Pack]   {dst_dir.name}: empty after truncate, skipping")
                            try:
                                shutil.rmtree(dst_dir)
                            except Exception:
                                pass
                    continue
                # Don't pre-create dst_dir for non-Windows path; copy_tree/copytree will create it
                dst_dir.parent.mkdir(parents=True, exist_ok=True)
                if dst_dir.exists():
                    try:
                        shutil.rmtree(dst_dir)
                    except Exception:
                        pass
                dst_dir.mkdir(parents=True, exist_ok=True)
                if filter_by_frame:
                    try:
                        copy_tree_filtered(src_dir, dst_dir)
                        if _dst_has_files(dst_dir):
                            _add_cache_dir_to_map(src_dir, dst_dir)
                            copied.append(dst_dir)
                    except PermissionError:
                        if os.name == "nt":
                            rc = _sub.run(
                                ["robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"],
                                capture_output=True, text=True,
                            )
                            if rc.returncode < 8 and _dst_has_files(dst_dir):
                                truncate_caches_to_frame_range(dst_dir, frame_start, frame_end, frame_step)
                                _add_cache_dir_to_map(src_dir, dst_dir)
                                copied.append(dst_dir)
                        else:
                            missing_on_copy.append(src_dir)
                    except Exception as e:
                        print(f"[BBP Pack] WARNING: Error copying filtered cache {src_dir.name}: {e}")
                        missing_on_copy.append(src_dir)
                else:
                    try:
                        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
                        _add_cache_dir_to_map(src_dir, dst_dir)
                        copied.append(dst_dir)
                    except PermissionError:
                        if os.name == "nt":
                            rc = _sub.run(
                                ["robocopy", str(src_dir), str(dst_dir), "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"],
                                capture_output=True, text=True,
                            )
                            if rc.returncode < 8:
                                _add_cache_dir_to_map(src_dir, dst_dir)
                                copied.append(dst_dir)
                        else:
                            missing_on_copy.append(src_dir)
            except Exception as e:
                missing_on_copy.append(src_dir)
    except Exception:
        pass
    return copied


def truncate_caches_to_frame_range(cache_dir: Path, frame_start: int, frame_end: int, frame_step: int) -> int:
    """
    Remove cache files outside the specified frame range.

    This helps reduce ZIP size by only including cache files for frames that will be rendered.

    Handles common cache naming patterns:
    - Numbered sequences: frame_0001.vdb, frame_0002.vdb, cache_fluid_0042.bphys.gz, etc.
    - Physics/simulation: fluid_####, cloth_####, softbody_####, particles_####, pointcache_####
    - Only keep files where extracted frame number is within [frame_start, frame_end] and matches frame_step

    If no files would remain after truncation (e.g. naming not recognized or all outside range), no files are deleted so the cache is not emptied by mistake.

    Returns number of files removed.
    """
    import re
    valid_frames = set(range(frame_start, frame_end + 1, frame_step))
    to_remove = []
    would_keep_count = 0

    for cache_file in cache_dir.rglob("*"):
        if not cache_file.is_file():
            continue

        frame_num = None
        stem = cache_file.stem
        # Blender bphys: name_frame_index (frame is middle number, index is last)
        match = re.search(r'_(\d+)_\d+$', stem)
        if match:
            frame_num = int(match.group(1))
        if frame_num is None:
            match = re.search(r'(?:frame_|cache[^_]*_)(\d+)', stem, re.IGNORECASE)
            if match:
                frame_num = int(match.group(1))
        if frame_num is None:
            match = re.search(r'(?:fluid_|cloth_|softbody_|particles_|pointcache_|sim_)(\d+)', stem, re.IGNORECASE)
            if match:
                frame_num = int(match.group(1))
        if frame_num is None:
            match = re.search(r'(\d+)$', stem)
            if match:
                frame_num = int(match.group(1))

        if frame_num is None or frame_num in valid_frames:
            would_keep_count += 1
        else:
            to_remove.append(cache_file)

    if would_keep_count == 0 and to_remove:
        print(f"[BBP Pack]   {cache_dir.name}: no files in frame range {frame_start}-{frame_end} (naming may differ), keeping full cache")
        return 0
    files_removed = 0
    for cache_file in to_remove:
        try:
            cache_file.unlink()
            files_removed += 1
        except Exception as e:
            print(f"[BBP Pack] WARNING: Could not remove {cache_file.name}: {e}")
    return files_removed


def _log_blender_subprocess_output(stdout: str, stderr: str) -> None:
    """Print a short preview of Blender subprocess stdout/stderr."""
    if stdout:
        stdout_lines = stdout.strip().split("\n")
        print(f"[BBP Pack]   stdout ({len(stdout_lines)} lines):")
        for line in stdout_lines[:10]:
            print(f"[BBP Pack]     {line}")
        if len(stdout_lines) > 10:
            print(f"[BBP Pack]     ... ({len(stdout_lines) - 10} more lines)")
    if stderr:
        stderr_lines = stderr.strip().split("\n")
        print(f"[BBP Pack]   stderr ({len(stderr_lines)} lines):")
        for line in stderr_lines[:10]:
            print(f"[BBP Pack]     {line}")
        if len(stderr_lines) > 10:
            print(f"[BBP Pack]     ... ({len(stderr_lines) - 10} more lines)")


def _run_blender_script(script: str, blend_path: Path, timeout: Optional[int] = None) -> tuple[str, str, int]:
    """Run an inline Python expression in a Blender subprocess (--python-expr)."""
    import subprocess
    import time
    if timeout is None:
        timeout = _blender_subprocess_timeout()
    print(f"[BBP Pack] Running Blender script on: {blend_path.name}")
    print(f"[BBP Pack]   Full path: {blend_path}")
    print(f"[BBP Pack]   Timeout: {timeout}s")
    start_time = time.time()
    try:
        result = subprocess.run(
            ["blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        elapsed = time.time() - start_time
        print(f"[BBP Pack]   Script completed in {elapsed:.2f}s, return code: {result.returncode}")
        _log_blender_subprocess_output(result.stdout or "", result.stderr or "")
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        print(f"[BBP Pack]   ERROR: Script timed out after {elapsed:.2f}s (timeout: {timeout}s)")
        print(f"[BBP Pack]   This may indicate the blend file has issues or is very large")
        return "", f"Script timed out after {timeout} seconds", -1
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[BBP Pack]   ERROR: Script failed after {elapsed:.2f}s: {type(e).__name__}: {str(e)}")
        return "", str(e), -1


def _blender_python_cmd(
    script_path: Path,
    blend_path: Path,
    config_path: Optional[Path] = None,
) -> list[str]:
    """Build blender -b <blend> --python <script> [-- <config>] argv."""
    cmd = [
        "blender",
        "--factory-startup",
        "-b",
        str(blend_path),
        "--python",
        str(script_path),
    ]
    if config_path is not None:
        cmd.extend(["--", str(config_path)])
    return cmd


@dataclass
class _BlenderPythonJob:
    """Non-blocking Blender --python subprocess with temp stdout/stderr files."""
    blend_path: Path
    proc: subprocess.Popen
    t0: float
    stdout_path: Path
    stderr_path: Path
    config_path: Optional[Path] = None
    stdout_handle: Optional[object] = None
    stderr_handle: Optional[object] = None
    timed_out: bool = False
    cancelled: bool = False


def _kill_blender_job(job: _BlenderPythonJob) -> None:
    """Kill Blender subprocess (process tree on Windows)."""
    proc = job.proc
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.kill()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _close_job_handles(job: _BlenderPythonJob) -> None:
    """Close stdout/stderr file handles opened for the job."""
    for h in (job.stdout_handle, job.stderr_handle):
        if h is None:
            continue
        try:
            h.close()
        except Exception:
            pass
    job.stdout_handle = None
    job.stderr_handle = None


def _cleanup_job_temps(job: _BlenderPythonJob) -> None:
    """Remove temp config/stdout/stderr files for a finished job."""
    _close_job_handles(job)
    for p in (job.stdout_path, job.stderr_path, job.config_path):
        if p is None:
            continue
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


def _read_job_output(job: _BlenderPythonJob) -> tuple[str, str]:
    """Read captured stdout/stderr text from temp files."""
    _close_job_handles(job)
    stdout = ""
    stderr = ""
    try:
        stdout = job.stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    try:
        stderr = job.stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return stdout, stderr


def _start_blender_python_file(
    script_path: Path,
    blend_path: Path,
    config_path: Optional[Path] = None,
) -> _BlenderPythonJob:
    """Start blender -b --python as a Popen job (caller polls / kills)."""
    print(f"[BBP Pack] Running Blender script on: {blend_path.name}")
    print(f"[BBP Pack]   Full path: {blend_path}")
    print(f"[BBP Pack]   Script file: {script_path.name}")
    cmd = _blender_python_cmd(script_path, blend_path, config_path=config_path)
    out_f = tempfile.NamedTemporaryFile(mode="w", suffix=".bbp_out", delete=False, encoding="utf-8")
    err_f = tempfile.NamedTemporaryFile(mode="w", suffix=".bbp_err", delete=False, encoding="utf-8")
    stdout_path = Path(out_f.name)
    stderr_path = Path(err_f.name)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=out_f,
            stderr=err_f,
            text=True,
        )
    except Exception:
        out_f.close()
        err_f.close()
        try:
            stdout_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return _BlenderPythonJob(
        blend_path=Path(blend_path),
        proc=proc,
        t0=time.time(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        config_path=Path(config_path) if config_path is not None else None,
        stdout_handle=out_f,
        stderr_handle=err_f,
    )


def _poll_blender_python_job(
    job: _BlenderPythonJob,
    timeout: Optional[float] = None,
) -> Optional[tuple[str, str, int]]:
    """Poll a Popen job. Returns (stdout, stderr, returncode) when finished, else None. Kills on timeout."""
    rc = job.proc.poll()
    elapsed = time.time() - job.t0
    if rc is None:
        if timeout is not None and elapsed >= float(timeout):
            job.timed_out = True
            print(f"[BBP Pack]   ERROR: Script timed out after {elapsed:.2f}s (timeout: {timeout}s) — killing {job.blend_path.name}")
            print(f"[BBP Pack]   This may indicate the blend file has issues or is very large")
            _kill_blender_job(job)
            stdout, stderr = _read_job_output(job)
            _cleanup_job_temps(job)
            return stdout, (stderr or "") + f"\nScript timed out after {timeout} seconds", -1
        return None
    stdout, stderr = _read_job_output(job)
    _cleanup_job_temps(job)
    print(f"[BBP Pack]   Script completed in {elapsed:.2f}s, return code: {rc}")
    _log_blender_subprocess_output(stdout or "", stderr or "")
    return stdout, stderr, int(rc)


def _run_blender_python_file(
    script_path: Path,
    blend_path: Path,
    config_path: Optional[Path] = None,
    timeout: Optional[int] = None,
) -> tuple[str, str, int]:
    """Run a .py file in a Blender subprocess: blender -b <blend> --python <script> [-- <config>]."""
    if timeout is None:
        timeout = _blender_subprocess_timeout()
    print(f"[BBP Pack]   Timeout: {timeout}s")
    try:
        job = _start_blender_python_file(script_path, blend_path, config_path=config_path)
    except Exception as e:
        print(f"[BBP Pack]   ERROR: Script failed to start: {type(e).__name__}: {str(e)}")
        return "", str(e), -1
    while True:
        result = _poll_blender_python_job(job, timeout=timeout)
        if result is not None:
            return result
        time.sleep(0.05)


def remap_library_paths(
    blend_path: Path,
    copy_map: dict[str, str],
    common_root: Path,
    target_path: Path,
    ensure_autopack: bool = True,
    search_roots: Optional[list] = None,
) -> list[Path]:
    """Open a blend file and remap all library paths to be relative to the copied tree.

    Logic lives in ops/remap_blend.py (readable Blender --python entrypoint).
    """
    import json
    import re
    import tempfile

    remap_script_path = Path(__file__).resolve().parent / "remap_blend.py"
    payload = {
        "copy_map": copy_map,
        "search_roots": [str(p) for p in (search_roots or [])],
        "common_root": str(common_root),
        "target_path": str(target_path),
        "ensure_autopack": bool(ensure_autopack),
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f, indent=None)
        config_path = Path(f.name)

    stdout = stderr = ""
    returncode = -1
    try:
        stdout, stderr, returncode = _run_blender_python_file(
            remap_script_path, blend_path, config_path, timeout=_blender_subprocess_timeout()
        )
    finally:
        try:
            if config_path.exists():
                config_path.unlink()
        except Exception:
            pass

    # Dead UNC / inaccessible network path: abort packing (do not claim success)
    dead = _extract_dead_unc_from_output(stdout or "", stderr or "")
    if dead or returncode == 2:
        if dead:
            path, kind, name, detail = dead
        else:
            path, kind, name, detail = "(unknown)", "", blend_path.name, f"exit code {returncode}"
        err = DeadUncAssetError(path, kind, name or blend_path.name, detail)
        print(f"[BBP Pack] ERROR: {err}")
        raise err

    unresolved = []
    if stdout:
        for line in stdout.splitlines():
            if (
                "Unresolved paths:" in line
                or "WARNING: Target file does not exist:" in line
                or "WARNING: Could not determine new path for:" in line
            ):
                match = re.search(r":\s*(.+)$", line)
                if match:
                    unresolved_path = match.group(1).strip()
                    try:
                        unresolved.append(Path(unresolved_path))
                    except Exception:
                        pass

    if returncode != 0:
        print(f"[BBP Pack] WARNING: remap_library_paths returned non-zero exit code: {returncode}")
        if stderr:
            print(f"[BBP Pack]   Error details: {stderr[:500]}")

    if unresolved:
        print(f"[BBP Pack] WARNING: {len(unresolved)} library paths could not be remapped")
        for up in unresolved[:5]:
            print(f"[BBP Pack]   - {up}")
        if len(unresolved) > 5:
            print(f"[BBP Pack]   ... and {len(unresolved) - 5} more")

    return unresolved


def pack_all_in_blend(blend_path: Path, pack_root: Optional[Path] = None) -> list[Path]:
    """Open a blend and pack all external files into it so headless render has no missing images.

    Logic lives in ops/pack_all_blend.py (readable Blender --python entrypoint).
    """
    import json
    import tempfile

    script_path = Path(__file__).resolve().parent / "pack_all_blend.py"
    cfg_file = None
    try:
        if pack_root is not None:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
                json.dump({"pack_root": str(pack_root)}, f)
                cfg_file = Path(f.name)
        stdout, _stderr, _code = _run_blender_python_file(
            script_path, blend_path, config_path=cfg_file, timeout=_blender_subprocess_timeout()
        )
    finally:
        if cfg_file is not None:
            try:
                cfg_file.unlink()
            except OSError:
                pass
    missing: list[Path] = []
    if stdout:
        for line in stdout.splitlines():
            if line.startswith("UNPACKED_TEXT:") or line.startswith("UNPACKED_IMAGE:"):
                # Surface basename after the label for the end-of-pack report.
                rest = line.split(":", 1)[-1].strip().split("|", 1)[0].strip()
                if rest:
                    missing.append(Path(rest))
    return missing


def _collect_pack_tree_blends(
    target_path: Path,
    hero: Optional[Path],
    copy_map: dict,
) -> list[Path]:
    """Every .blend copied into the pack tree — deps first, hero last.

    BAT's find_blend_asset_usage() only sees session-visible links; prop/rig libs (and their textures) still need remap+pack_all before pack_libraries embeds them.
    """
    seen: set[str] = set()
    blends: list[Path] = []

    def _add(p: Path) -> None:
        try:
            if not p.is_file() or p.suffix.lower() != ".blend":
                return
            key = _norm_copy_map_key(p)
            if key in seen:
                return
            seen.add(key)
            blends.append(p.resolve())
        except OSError:
            return

    for dest in (copy_map or {}).values():
        _add(Path(dest))
    if hero:
        _add(Path(hero))
    try:
        for p in target_path.rglob("*.blend"):
            # Skip localized staging copies — real libs live under DRIVE_*/UNC_*.
            if "_bbp_linked" in p.parts:
                continue
            _add(p)
    except OSError as e:
        _pack_diag(f"pack-tree blend scan failed: {e}")

    hero_key = _norm_copy_map_key(hero) if hero else None
    deps = [p for p in blends if _norm_copy_map_key(p) != hero_key]
    # Deeper paths first (nested asset libs before scenes that link them).
    deps.sort(key=lambda p: (-len(p.parts), str(p).lower()))
    heroes = [p for p in blends if _norm_copy_map_key(p) == hero_key]
    return deps + heroes


def _get_project_size_limit_bytes(context=None):
    """Return project size limit in bytes from scene (per-pack). 0 = no limit (returns None)."""
    try:
        scene = context.scene if context else bpy.context.scene
        st = getattr(scene, "bbp_pack", None)
        if not st or not hasattr(st, "project_size_limit_gb"):
            return 2 * 1024 * 1024 * 1024
        gb = getattr(st, "project_size_limit_gb", 2)
        if gb <= 0:
            return None
        return int(gb * (1024 ** 3))
    except Exception:
        return 2 * 1024 * 1024 * 1024


def _write_pack_linked_config(
    blend_path: Path,
    max_size_bytes: int,
    pack_root: Optional[Path],
    search_roots: Optional[list],
) -> Path:
    """Write JSON config for pack_linked_blend.py; caller deletes when the job finishes."""
    import json

    payload = {
        "max_size_bytes": int(max_size_bytes),
        "pack_root": str(pack_root) if pack_root else str(blend_path.parent),
        "search_roots": [str(p) for p in (search_roots or [])],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(payload, f)
        return Path(f.name)


def _parse_pack_linked_output(
    blend_path: Path,
    stdout: str,
    stderr: str,
    returncode: int,
    *,
    timed_out: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Parse pack_linked_blend.py stdout/stderr into (missing_files, oversized_files)."""
    missing_files: list[Path] = []
    oversized_files: list[Path] = []
    unpacked_libs: list[str] = []
    pack_errors: list[str] = []

    def _add_missing(label: str) -> None:
        """Deduped missing entry; keep first path segment before '|' diagnostics."""
        text = (label or "").strip().split("|", 1)[0].strip()
        if not text:
            return
        path = Path(text)
        if path not in missing_files and text not in {str(p) for p in missing_files}:
            missing_files.append(path)

    if timed_out:
        _add_missing(f"pack_linked_timeout:{blend_path.name}")
        print(f"[BBP Pack]   FAILED (timeout): {blend_path.name}")

    if stdout:
        for line in stdout.splitlines():
            if line.startswith("MISSING_FILE:"):
                _add_missing(line.replace("MISSING_FILE:", "", 1))
            elif line.startswith("OVERSIZED_FILE:"):
                try:
                    oversized_files.append(Path(line.replace("OVERSIZED_FILE:", "").strip().split("|", 1)[0].strip()))
                except Exception:
                    pass
            elif line.startswith("UNPACKED_LIB:"):
                name = line.replace("UNPACKED_LIB:", "").strip().split("|", 1)[0].strip()
                unpacked_libs.append(name)
                _add_missing(name)
            elif line.startswith("UNPACKED_TEXT:"):
                name = line.replace("UNPACKED_TEXT:", "").strip().split("|", 1)[0].strip()
                unpacked_libs.append(name)
                _add_missing(name)
            elif line.startswith("MISSING_ID:"):
                body = line.replace("MISSING_ID:", "", 1).strip()
                id_part = body.split("|", 1)[0].strip()
                lib_part = ""
                for part in body.split("|"):
                    part = part.strip()
                    if part.startswith("lib="):
                        lib_part = part[4:].strip()
                        break
                label = f"{lib_part}: {id_part}" if lib_part else id_part
                unpacked_libs.append(label)
                _add_missing(label)
            elif line.startswith("PACK_ERROR:"):
                pack_errors.append(line.replace("PACK_ERROR:", "").strip())

    combined_output = (stdout or "") + "\n" + (stderr or "")
    for pattern in (
        r"Warning, files not found:\s*(.+)",
        r"Unable to pack file, source path '([^']+)' not found",
        r"Cannot pack absolute file:\s*'([^']+)'",
        r"File not found:\s*(.+)",
        r"missing from\s+'([^']+)'",
        r"(\d+)\s+libraries and\s+(\d+)\s+linked data-blocks are missing",
    ):
        for match in re.finditer(pattern, combined_output, re.IGNORECASE):
            if "libraries and" in pattern:
                marker = Path(f"{match.group(1)}_libs_{match.group(2)}_ids_missing")
                if marker not in missing_files:
                    missing_files.append(marker)
                    print(f"[BBP Pack]   WARNING: Blender reports {match.group(1)} libraries and {match.group(2)} linked data-blocks missing")
                continue
            missing_path_str = match.group(1).strip()
            if missing_path_str.startswith("//"):
                try:
                    missing_path = (blend_path.parent / missing_path_str[2:]).resolve()
                except Exception:
                    missing_path = Path(missing_path_str)
            else:
                missing_path = Path(missing_path_str)
            if missing_path not in missing_files:
                missing_files.append(missing_path)
                print(f"[BBP Pack]   WARNING: Blender reports missing library/ID path: {missing_path.name}")

    if missing_files and not timed_out:
        print(f"[BBP Pack]   WARNING: {len(missing_files)} linked files could not be packed (files not found):")
        for mf in missing_files[:5]:
            print(f"[BBP Pack]     - {mf.name if mf.name else mf}")
        if len(missing_files) > 5:
            print(f"[BBP Pack]     ... and {len(missing_files) - 5} more")

    if oversized_files:
        print(f"[BBP Pack]   WARNING: {len(oversized_files)} linked files could not be packed (over size limit):")
        for of in oversized_files[:5]:
            file_size_gb = of.stat().st_size / (1024 * 1024 * 1024) if of.exists() else 0
            print(f"[BBP Pack]     - {of.name if of.name else of} ({file_size_gb:.2f} GB)")
        if len(oversized_files) > 5:
            print(f"[BBP Pack]     ... and {len(oversized_files) - 5} more")

    if unpacked_libs:
        print(f"[BBP Pack]   ERROR: {len(unpacked_libs)} libraries still unpacked after pack_libraries:")
        for name in unpacked_libs[:8]:
            print(f"[BBP Pack]     - {name}")
        _pack_diag(f"pack_linked left unpacked: {unpacked_libs}")

    if pack_errors:
        for err in pack_errors:
            print(f"[BBP Pack]   PACK_ERROR: {err}")

    if returncode != 0 and not timed_out:
        print(f"[BBP Pack] WARNING: pack_linked_in_blend returned non-zero exit code: {returncode}")
        if stderr:
            print(f"[BBP Pack]   Error details: {stderr[:500]}")

    for name in unpacked_libs:
        _add_missing(name)

    return missing_files, oversized_files


def _prelocalize_pack_tree_libs(pack_root: Path) -> int:
    """Copy pack-tree .blend basenames into pack_root/_bbp_linked so parallel pack_linked workers reuse instead of racing copies.

    Cheap host-side pass before spawning workers; does not open Blender.
    """
    if not pack_root or not Path(pack_root).is_dir():
        return 0
    pack_root = Path(pack_root)
    dest_dir = pack_root / "_bbp_linked"
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in pack_root.rglob("*.blend"):
        try:
            if "_bbp_linked" in src.parts:
                continue
            if not src.is_file():
                continue
            dest = dest_dir / src.name
            if dest.is_file():
                try:
                    if dest.stat().st_size == src.stat().st_size:
                        continue
                except OSError:
                    pass
                # Collision with different size — leave existing; pack_linked_blend disambiguates.
                continue
            shutil.copy2(src, dest)
            copied += 1
        except OSError as e:
            print(f"[BBP Pack]   WARNING: pre-localize skipped {src.name}: {e}")
    if copied:
        print(f"[BBP Pack] Pre-localized {copied} blend(s) into {dest_dir.name}/ for parallel pack_linked")
    return copied


def _pack_linked_script_path() -> Path:
    """Path to ops/pack_linked_blend.py."""
    return Path(__file__).resolve().parent / "pack_linked_blend.py"


def _start_pack_linked_job(
    blend_path: Path,
    max_size_bytes: int,
    pack_root: Optional[Path],
    search_roots: Optional[list],
) -> _BlenderPythonJob:
    """Start one pack_linked Blender job (non-blocking)."""
    cfg = _write_pack_linked_config(blend_path, max_size_bytes, pack_root, search_roots)
    try:
        return _start_blender_python_file(_pack_linked_script_path(), blend_path, config_path=cfg)
    except Exception:
        try:
            cfg.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass
class PackLinkedParallelRunner:
    """Wave scheduler: non-hero blends in parallel (N workers), hero last; poll each tick."""

    blends: list[Path]
    hero_blend: Optional[Path]
    pack_root: Path
    search_roots: list
    max_size_bytes: int
    workers: int = 4
    timeout_sec: float = 15.0
    cancel_check: Optional[Callable[[], bool]] = None

    # Internal state
    _ready: list[Path] = field(default_factory=list, init=False)
    _hero_queue: list[Path] = field(default_factory=list, init=False)
    _active: list[_BlenderPythonJob] = field(default_factory=list, init=False)
    _finished: int = field(default=0, init=False)
    _total: int = field(default=0, init=False)
    _started: bool = field(default=False, init=False)
    _done: bool = field(default=False, init=False)
    missing_files: list[Path] = field(default_factory=list, init=False)
    oversized_files: list[Path] = field(default_factory=list, init=False)
    failed_blends: list[Path] = field(default_factory=list, init=False)

    def begin(self) -> None:
        """Pre-localize _bbp_linked and split hero vs deps."""
        if self._started:
            return
        self._started = True
        _prelocalize_pack_tree_libs(self.pack_root)
        hero_key = None
        if self.hero_blend:
            try:
                hero_key = Path(self.hero_blend).resolve()
            except OSError:
                hero_key = Path(self.hero_blend)
        deps: list[Path] = []
        heroes: list[Path] = []
        for p in self.blends:
            if not Path(p).exists():
                continue
            try:
                key = Path(p).resolve()
            except OSError:
                key = Path(p)
            if hero_key is not None and key == hero_key:
                heroes.append(Path(p))
            else:
                deps.append(Path(p))
        # Fallback thin graph: all non-hero ready in parallel, hero final wave.
        self._ready = deps
        self._hero_queue = heroes
        self._total = len(deps) + len(heroes)
        self.workers = max(1, int(self.workers or 1))
        print(
            f"[BBP Pack] Pack-linked parallel: {len(deps)} dep(s), {len(heroes)} hero, "
            f"workers={self.workers}, timeout={int(self.timeout_sec)}s"
        )

    def cancel_active(self) -> None:
        """Kill all running Blender children."""
        for job in list(self._active):
            job.cancelled = True
            _kill_blender_job(job)
            _cleanup_job_temps(job)
        self._active.clear()
        self._done = True

    def _reap(self, job: _BlenderPythonJob, stdout: str, stderr: str, returncode: int) -> None:
        """Record one finished job; never print Completed on timeout."""
        self._finished += 1
        timed_out = bool(job.timed_out)
        missing, oversized = _parse_pack_linked_output(
            job.blend_path, stdout or "", stderr or "", returncode, timed_out=timed_out
        )
        if timed_out or returncode != 0:
            self.failed_blends.append(job.blend_path)
            if timed_out:
                print(f"[BBP Pack]   [{self._finished}/{self._total}] Timed out (failed): {job.blend_path.name}")
            else:
                print(f"[BBP Pack]   [{self._finished}/{self._total}] Failed (exit {returncode}): {job.blend_path.name}")
        else:
            print(f"[BBP Pack]   [{self._finished}/{self._total}] Completed: {job.blend_path.name}")
        if missing:
            self.missing_files.extend(missing)
        if oversized:
            self.oversized_files.extend(oversized)
        issues = []
        if missing:
            issues.append(f"{len(missing)} missing")
        if oversized:
            issues.append(f"{len(oversized)} over size limit")
        if issues and not timed_out:
            print(f"[BBP Pack]     Note: {', '.join(issues)} linked files could not be packed")

    def _fill_slots(self) -> None:
        """Spawn jobs from ready queue up to worker cap."""
        while len(self._active) < self.workers and self._ready:
            blend = self._ready.pop(0)
            print(f"[BBP Pack]   Starting pack_linked: {blend.name} ({len(self._active) + 1}/{self.workers} slots)")
            try:
                job = _start_pack_linked_job(
                    blend,
                    self.max_size_bytes,
                    self.pack_root,
                    self.search_roots,
                )
            except Exception as e:
                self._finished += 1
                self.failed_blends.append(Path(blend))
                self.missing_files.append(Path(f"pack_linked_start_failed:{Path(blend).name}"))
                print(f"[BBP Pack]   [{self._finished}/{self._total}] Failed to start: {Path(blend).name}: {e}")
                continue
            self._active.append(job)

    def tick(self) -> tuple[bool, str]:
        """Poll jobs / fill slots. Returns (done, status_message)."""
        if not self._started:
            self.begin()
        if self._done:
            return True, "Packing linked… done"

        if self.cancel_check and self.cancel_check():
            self.cancel_active()
            raise InterruptedError("Packing cancelled by user")

        # Reap finished / timed-out jobs.
        still_active: list[_BlenderPythonJob] = []
        for job in self._active:
            result = _poll_blender_python_job(job, timeout=self.timeout_sec)
            if result is None:
                still_active.append(job)
                continue
            stdout, stderr, returncode = result
            self._reap(job, stdout, stderr, returncode)
        self._active = still_active

        self._fill_slots()

        # When deps drained, enqueue hero as final wave.
        if not self._ready and not self._active and self._hero_queue:
            self._ready = list(self._hero_queue)
            self._hero_queue.clear()
            print(f"[BBP Pack] Pack-linked hero wave: {[p.name for p in self._ready]}")
            self._fill_slots()

        if not self._ready and not self._active and not self._hero_queue:
            self._done = True
            print(f"[BBP Pack] Finished packing linked libraries ({self._finished}/{self._total})")
            if self.failed_blends:
                print(f"[BBP Pack]   {len(self.failed_blends)} blend(s) failed/timed out during pack_linked")
            return True, "Packing linked… done"

        running_names = [j.blend_path.name for j in self._active[:3]]
        more = len(self._active) - len(running_names)
        running = ", ".join(running_names) + (f", +{more}" if more > 0 else "")
        msg = (
            f"Packing linked… ({self._finished}/{self._total} done, "
            f"{len(self._active)} running: {running})"
        )
        return False, msg

    def run_blocking(
        self,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        progress_base: float = 80.0,
        progress_span: float = 15.0,
    ) -> tuple[list[Path], list[Path]]:
        """Drive tick() until done (sync pack_project path)."""
        self.begin()
        while True:
            done, msg = self.tick()
            if progress_callback and self._total:
                pct = progress_base + (self._finished / max(1, self._total)) * progress_span
                progress_callback(pct, msg)
            if done:
                break
            time.sleep(0.05)
        return self.missing_files, self.oversized_files


def pack_linked_in_blend(
    blend_path: Path,
    max_size_bytes: Optional[int] = None,
    pack_root: Optional[Path] = None,
    search_roots: Optional[list] = None,
) -> tuple[list[Path], list[Path]]:
    """Open a blend and run Pack Linked (pack libraries), then save with autopack on.

    Logic lives in ops/pack_linked_blend.py. Absolute library paths (different path anchors) are localized into _bbp_linked/ first — otherwise Blender aborts pack_libraries() and the hero blend stays hollow (~source size) while the ZIP still holds the trees.

    Returns:
        Tuple of (missing_files, oversized_files)
    """
    if max_size_bytes is None:
        max_size_bytes = 2 * 1024 * 1024 * 1024
    timeout = int(getattr(config, "PACK_LINKED_TIMEOUT_SEC", _blender_subprocess_timeout()))
    cfg_file = None
    try:
        cfg_file = _write_pack_linked_config(blend_path, int(max_size_bytes), pack_root, search_roots)
        stdout, stderr, returncode = _run_blender_python_file(
            _pack_linked_script_path(), blend_path, config_path=cfg_file, timeout=timeout
        )
        timed_out = returncode == -1 and "timed out" in (stderr or "").lower()
        return _parse_pack_linked_output(
            blend_path, stdout or "", stderr or "", returncode, timed_out=timed_out
        )
    finally:
        if cfg_file is not None:
            try:
                cfg_file.unlink(missing_ok=True)
            except OSError:
                pass


class IncrementalPacker:
    """Stateful incremental packer that processes files in batches across multiple timer events."""
    
    def __init__(self, workflow: str, target_path: Optional[Path],
                 progress_callback=None, cancel_check=None,
                 frame_start=None, frame_end=None, frame_step=None,
                 temp_blend_path: Optional[Path] = None,
                 original_blend_path: Optional[Path] = None,
                 max_size_bytes: Optional[int] = None,
                 exclude_av: bool = False):
        self.workflow = workflow
        self.target_path = target_path
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check
        self.frame_start = frame_start  # For cache truncation
        self.frame_end = frame_end
        self.frame_step = frame_step
        self.temp_blend_path = temp_blend_path  # Temp file used as source (should be copied directly to root)
        self.original_blend_path = original_blend_path  # Original blend file path (for cache lookup)
        self.max_size_bytes = max_size_bytes  # Project size limit in bytes (None = 2GB)
        # When True, skip video/audio entirely from the pack (copy + ZIP)
        self.exclude_av = exclude_av
        
        # State tracking
        self.phase = 'INIT'
        self.copy_only_mode = workflow == WorkflowMode.COPY_ONLY
        self.autopack_on_save = not self.copy_only_mode
        self.run_pack_linked = not self.copy_only_mode
        
        # Asset finding state
        self.asset_usages = None
        self.hero_abs = None  # Source hero path (open session / temp export)
        self.all_filepaths = []
        self.common_root = None
        
        # File copying state
        self.copied_paths = set()
        self.copy_map = {}
        self.missing_on_copy = []
        self.assets_to_copy = []  # List of (asset_usage, target_path, common_root) tuples
        self.assets_copied = 0
        self.hero_blend = None  # Hero copy inside the pack tree
        self.cache_dirs = []  # List of cache directories to truncate
        # Stale-path recovery: session-derived roots + basename→hits cache
        self.search_roots: list[Path] = []
        self._recovery_cache: dict = {}
        self._recovery_hits = 0
        self._recovery_misses = 0
        self._recovery_time_s = 0.0
        
        # Blend processing state
        self.blend_deps = None
        self.to_remap = []
        self.remap_index = 0
        self.pack_all_index = 0
        self.pack_linked_index = 0
        self.pack_linked_runner: Optional[PackLinkedParallelRunner] = None
        
        # Cache truncation state
        self.cache_truncate_index = 0
        
        # Pack linked issues tracking
        self.oversized_files_all = []  # Collect all oversized files from pack_linked operations
        self.missing_files_all = []  # Collect missing/offline files for end-of-pack report
        self.missing_summary = ""  # Short UI message after COMPLETE
        self.pack_linked_failed_blends: list[Path] = []  # Timed out / non-zero pack_linked
        self.pack_linked_failure_summary = ""  # ERROR-level UI message when pack_linked failed
        
        # Phase timing (for Verbose Pack Log optimization)
        self._session_t0 = time.perf_counter()
        self._timed_phase = None
        self._phase_t0 = self._session_t0
        # Hang-threshold UI yields (Atomic scanner pattern): last slow unit for status.
        self.hang_phase = None
        self.hang_name = None
        self.hang_elapsed = None
        
        # Results
        self.file_path = None
        self.error = None

    def cancel_pack_linked_jobs(self) -> None:
        """Kill any active pack_linked Blender children (modal cancel / ESC)."""
        runner = getattr(self, "pack_linked_runner", None)
        if runner is not None:
            runner.cancel_active()
            self.pack_linked_runner = None

    def _tick_phase_timing(self) -> None:
        """Log elapsed time when PackSession phase changes (verbose)."""
        if self._timed_phase is None:
            self._timed_phase = self.phase
            self._phase_t0 = time.perf_counter()
            return
        if self.phase == self._timed_phase:
            return
        elapsed = time.perf_counter() - self._phase_t0
        _pack_diag(f"Phase {self._timed_phase} took {elapsed:.2f}s", verbose_only=True)
        self._timed_phase = self.phase
        self._phase_t0 = time.perf_counter()

    def _progress(self, pct: float, message: str) -> None:
        """Progress callback with optional hang-status suffix."""
        if not self.progress_callback:
            return
        self.progress_callback(pct, f"{message}{config.hang_status_suffix(self)}")

    def _unit_took_too_long(self, phase: str, name: str, t0: float) -> bool:
        """True when this unit exceeded hang threshold — caller should end the timer tick."""
        elapsed = time.perf_counter() - t0
        if elapsed < config.PACK_HANG_THRESHOLD_SEC:
            return False
        config.note_pack_hang(self, phase, name, elapsed)
        if _verbose_pack_log_enabled():
            print(f"[BBP Pack] SLOW {phase}: '{name}' took {elapsed:.2f}s", flush=True)
        return True
    
    def process_batch(self, batch_size: Optional[int] = None) -> Tuple[str, bool]:
        """Process one timer-tick of work.

        Unbounded by default (Atomic-style): keep going until the phase ends or a single unit exceeds ``PACK_HANG_THRESHOLD_SEC``, then yield so the UI can redraw.
        *batch_size* caps units per tick when set.

        Returns:
            Tuple of (next_phase, is_complete)
        """
        if batch_size is None:
            batch_size = config.PACK_BATCH_UNBOUNDED
        if self.cancel_check and self.cancel_check():
            raise InterruptedError("Packing cancelled by user")

        self._tick_phase_timing()
        
        if self.phase == 'INIT':
            if self.target_path is None:
                self.target_path = Path(tempfile.mkdtemp(prefix="bbp_pack_"))
                print(f"[BBP Pack] Created temporary directory: {self.target_path}")
            else:
                print(f"[BBP Pack] Using provided target path: {self.target_path}")
            
            print(f"[BBP Pack] Mode: {'COPY_ONLY' if self.copy_only_mode else 'PACK_AND_SAVE'}")
            self.phase = 'FIND_ASSETS'
            return ('FIND_ASSETS', False)
        
        elif self.phase == 'FIND_ASSETS':
            print(f"[BBP Pack] Finding asset usages...")
            if self.progress_callback:
                self.progress_callback(5.0, "Finding asset usages...")
            # Abort early if any datablock points at a dead UNC / inaccessible network path
            dead_assets = find_dead_unc_assets()
            if dead_assets:
                kind, name, path, detail = dead_assets[0]
                if len(dead_assets) > 1:
                    detail = f"{detail}; (+{len(dead_assets) - 1} more)"
                err = DeadUncAssetError(path, kind, name, detail)
                print(f"[BBP Pack] ERROR: {err}")
                raise err
            t_find = time.perf_counter()
            self.asset_usages = au.find()
            _pack_diag(f"au.find() took {time.perf_counter() - t_find:.2f}s")
            self.hero_abs = au.library_abspath(None).resolve()
            print(f"[BBP Pack] Found {len(self.asset_usages)} libraries with assets")
            print(f"[BBP Pack] Hero blend: {self.hero_abs}")
            _diag_summarize_asset_usages(self.asset_usages)
            _diag_session_udim_gap(self.asset_usages)
            self.phase = 'COLLECT_PATHS'
            return ('COLLECT_PATHS', False)
        
        elif self.phase == 'COLLECT_PATHS':
            print(f"[BBP Pack] Collecting all file paths...")
            if self.progress_callback:
                self.progress_callback(10.0, "Collecting file paths...")
            self.all_filepaths = []
            self.all_filepaths.extend(au.library_abspath(lib).resolve() for lib in self.asset_usages.keys())
            self.all_filepaths.extend(
                asset_usage.abspath.resolve()
                for asset_usages in self.asset_usages.values()
                for asset_usage in asset_usages
            )
            # Exclude temp file from common root calculation (it's just a source, not part of the project)
            if self.temp_blend_path:
                temp_path_resolved = self.temp_blend_path.resolve()
                self.all_filepaths = [p for p in self.all_filepaths if p != temp_path_resolved]
                print(f"[BBP Pack]   Excluded temp file from common root calculation")
            # Use resolved paths only so common_root is deterministic (no A:\ vs UNC mix)
            self.all_filepaths = [Path(p).resolve() for p in self.all_filepaths]
            print(f"[BBP Pack] Collected {len(self.all_filepaths)} total file paths")
            self.phase = 'FIND_COMMON_ROOT'
            return ('FIND_COMMON_ROOT', False)
        
        elif self.phase == 'FIND_COMMON_ROOT':
            print(f"[BBP Pack] Determining common root directory...")
            try:
                common_root_str = os.path.commonpath(self.all_filepaths)
                print(f"[BBP Pack] Common root (method 1): {common_root_str}")
            except ValueError:
                print(f"[BBP Pack] Method 1 failed, trying drive-based approach...")
                blend_file_drive = Path(bpy.data.filepath).drive if hasattr(Path(bpy.data.filepath), 'drive') else ""
                project_filepaths = [p for p in self.all_filepaths if getattr(p, "drive", "") == blend_file_drive]
                if project_filepaths:
                    common_root_str = os.path.commonpath(project_filepaths)
                    print(f"[BBP Pack] Common root (method 2): {common_root_str}")
                else:
                    common_root_str = str(Path(bpy.data.filepath).parent)
                    print(f"[BBP Pack] Common root (fallback): {common_root_str}")
            
            if not common_root_str:
                raise ValueError("Could not find a common root directory for these assets.")
            
            self.common_root = Path(common_root_str)
            print(f"[BBP Pack] Using common root: {self.common_root}")
            self.phase = 'PREPARE_COPY_TOP_BLEND'
            return ('PREPARE_COPY_TOP_BLEND', False)
        
        elif self.phase == 'PREPARE_COPY_TOP_BLEND':
            print(f"[BBP Pack] Copying hero blend...")
            if self.progress_callback:
                self.progress_callback(15.0, "Copying hero blend...")
            
            current_blend_abspath = self.hero_abs
            
            # If this is a temp file, copy it directly to target root with just its filename. This avoids the DRIVE_C path structure issue
            is_temp_file = (self.temp_blend_path and 
                          current_blend_abspath.resolve() == self.temp_blend_path.resolve())
            
            if is_temp_file:
                # Copy temp file directly to target root
                target_path_file = self.target_path / current_blend_abspath.name
                print(f"[BBP Pack]   Temp file detected, copying directly to target root: {target_path_file.name}")
            else:
                try:
                    current_relpath = current_blend_abspath.relative_to(self.common_root)
                    print(f"[BBP Pack]   Relative path: {current_relpath}")
                except ValueError:
                    current_relpath = compute_target_relpath(current_blend_abspath, self.common_root)
                    print(f"[BBP Pack]   Computed relative path: {current_relpath}")
                target_path_file = self.target_path / current_relpath
            
            current_blend_resolved = current_blend_abspath.resolve()
            if current_blend_resolved not in self.copied_paths:
                print(f"[BBP Pack]   Copying: {current_blend_abspath} -> {target_path_file}")
                try:
                    target_path_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(current_blend_abspath, target_path_file)
                    self.copied_paths.add(current_blend_resolved)
                    if current_blend_abspath.suffix.lower() == ".blend":
                        self.copy_map[_norm_copy_map_key(current_blend_resolved)] = str(target_path_file.resolve())
                    self.hero_blend = target_path_file.resolve()
                    print(f"[BBP Pack]   Copied successfully, size: {target_path_file.stat().st_size} bytes")
                    # Copy caches - use original blend path for cache lookup if temp file
                    cache_source_blend = self.original_blend_path if (is_temp_file and self.original_blend_path) else current_blend_abspath
                    if cache_source_blend:
                        print(f"[BBP Pack]   Copying blend caches from: {cache_source_blend}")
                        # For COPY_ONLY workflow, filter caches during copy if frame range is specified
                        filter_during_copy = (self.copy_only_mode and 
                                             self.frame_start is not None and 
                                             self.frame_end is not None and 
                                             self.frame_step is not None)
                        if filter_during_copy:
                            print(f"[BBP Pack]   Filtering caches to frame range {self.frame_start}-{self.frame_end} (step: {self.frame_step}) during copy...")
                        copied_cache_dirs = copy_blend_caches(
                            cache_source_blend, target_path_file, self.missing_on_copy,
                            frame_start=self.frame_start if filter_during_copy else None,
                            frame_end=self.frame_end if filter_during_copy else None,
                            frame_step=self.frame_step if filter_during_copy else None,
                            copy_map_out=self.copy_map,
                        )
                        self.cache_dirs.extend(copied_cache_dirs)
                        print(f"[BBP Pack]   Copied {len(copied_cache_dirs)} cache directories")
                except Exception as e:
                    print(f"[BBP Pack]   ERROR copying hero blend: {type(e).__name__}: {str(e)}")
                    self.missing_on_copy.append(current_blend_abspath)
            
            # Prepare asset copy list (dedupe by resolved path so each file copied once)
            total_assets = sum(len(links) for links in self.asset_usages.values())
            print(f"[BBP Pack] Preparing to copy {total_assets} asset files...")
            seen_resolved = set()
            for lib, links_to in self.asset_usages.items():
                for asset_usage in links_to:
                    resolved = asset_usage.abspath.resolve()
                    if resolved in self.copied_paths or resolved in seen_resolved:
                        continue
                    # Skip cache directories: already copied in copy_blend_caches from blend dir; including them here would try UNC path and fail with PermissionError.
                    name = asset_usage.abspath.name
                    if name.startswith("blendcache_") or name.startswith("cache_") or (
                        len(asset_usage.abspath.parts) >= 2 and asset_usage.abspath.parts[-2] == "bakes"
                    ):
                        continue
                    # Pack-linked origin libs / BAT phantom UDIMs — not real pack targets.
                    if _is_ignorable_missing_asset(asset_usage.abspath):
                        _pack_diag(f"Skip copy (ignorable): {name}", verbose_only=True)
                        continue
                    seen_resolved.add(resolved)
                    try:
                        asset_relpath = resolved.relative_to(self.common_root)
                    except ValueError:
                        asset_relpath = compute_target_relpath(resolved, self.common_root)
                    self.assets_to_copy.append((asset_usage, asset_relpath))
            
            self.assets_copied = 0
            # Build search roots from live BAT paths so stale absolutes can be recovered later.
            existing_for_roots = [
                a.abspath
                for links in self.asset_usages.values()
                for a in links
                if a.abspath.exists()
            ]
            blend_parent = self.hero_abs.parent if self.hero_abs else None
            self.search_roots = collect_search_roots(self.common_root, existing_for_roots, blend_parent)
            self._recovery_cache = {}
            print(f"[BBP Pack] Stale-path search roots: {len(self.search_roots)}")
            _pack_diag(f"Search roots ({len(self.search_roots)}):")
            for r in self.search_roots[:24]:
                _pack_diag(f"  root: {r}", verbose_only=True)
            if len(self.search_roots) > 24:
                _pack_diag(f"  ... and {len(self.search_roots) - 24} more roots", verbose_only=True)
            self.phase = 'COPY_ASSETS'
            return ('COPY_ASSETS', False)
        
        elif self.phase == 'COPY_ASSETS':
            # Unbounded copy per tick; yield after a slow unit (hang threshold).
            total_assets = len(self.assets_to_copy)
            units_this_tick = 0
            while self.assets_copied < total_assets and units_this_tick < batch_size:
                if self.cancel_check and self.cancel_check():
                    raise InterruptedError("Packing cancelled by user")
                i = self.assets_copied
                asset_usage, _asset_relpath = self.assets_to_copy[i]
                stale_path = asset_usage.abspath
                t0 = time.perf_counter()
                try:
                    stale_resolved = stale_path.resolve()
                except OSError:
                    stale_resolved = Path(stale_path)
                if stale_resolved in self.copied_paths:
                    self.assets_copied = i + 1
                    units_this_tick += 1
                    continue
                if _is_excluded_media_path(stale_path, self.exclude_av):
                    print(f"[BBP Pack]   Skipping excluded media: {stale_path.name}")
                    self.assets_copied = i + 1
                    units_this_tick += 1
                    continue
                src_path = stale_path
                if not src_path.exists():
                    if _is_ignorable_missing_asset(stale_path):
                        _pack_diag(f"Skip missing (ignorable): {stale_path.name}", verbose_only=True)
                        self.assets_copied = i + 1
                        units_this_tick += 1
                        continue
                    t_rec = time.perf_counter()
                    recovered = recover_stale_path(stale_path, self.search_roots, self._recovery_cache)
                    self._recovery_time_s += time.perf_counter() - t_rec
                    if recovered is None:
                        self._recovery_misses += 1
                        print(f"[BBP Pack]   WARNING: Asset does not exist: {stale_path}")
                        self.missing_on_copy.append(stale_path)
                        self.assets_copied = i + 1
                        units_this_tick += 1
                        if self._unit_took_too_long("copy", stale_path.name, t0):
                            break
                        continue
                    self._recovery_hits += 1
                    _pack_diag(f"Recovered: {stale_path.name} -> {recovered}", verbose_only=True)
                    src_path = recovered
                try:
                    src_resolved = src_path.resolve()
                except OSError:
                    src_resolved = Path(src_path)
                if src_resolved in self.copied_paths:
                    dest = self.copy_map.get(_norm_copy_map_key(src_resolved))
                    if dest:
                        self.copy_map[_norm_copy_map_key(stale_path)] = dest
                    self.copied_paths.add(stale_resolved)
                    self.assets_copied = i + 1
                    units_this_tick += 1
                    continue
                try:
                    asset_relpath = src_resolved.relative_to(self.common_root)
                except ValueError:
                    asset_relpath = compute_target_relpath(src_resolved, self.common_root)
                target_asset_path = self.target_path / asset_relpath
                try:
                    target_asset_path.parent.mkdir(parents=True, exist_ok=True)
                    file_size = src_path.stat().st_size
                    shutil.copy2(src_path, target_asset_path)
                    self.copied_paths.add(src_resolved)
                    self.copied_paths.add(stale_resolved)
                    _copy_map_register(self.copy_map, stale_path, src_resolved, target_asset_path)
                    if (i < 5) or (i % 50 == 0):
                        print(f"[BBP Pack]   Copied: {src_path.name} ({file_size} bytes)")
                except Exception as e:
                    print(f"[BBP Pack]   ERROR copying asset {src_path.name}: {type(e).__name__}: {str(e)}")
                    if not _is_ignorable_missing_asset(stale_path):
                        self.missing_on_copy.append(stale_path)
                self.assets_copied = i + 1
                units_this_tick += 1
                if self._unit_took_too_long("copy", stale_path.name, t0):
                    break

            progress_pct = 15.0 + (self.assets_copied / total_assets * 30.0) if total_assets > 0 else 15.0
            self._progress(progress_pct, f"Copying assets... ({self.assets_copied}/{total_assets})")
            if self.assets_copied % 10 == 0 or self.assets_copied == total_assets or self.hang_name:
                print(f"[BBP Pack]   Copied {self.assets_copied}/{total_assets} assets ({progress_pct:.1f}%)...")

            if self.assets_copied < total_assets:
                return ('COPY_ASSETS', False)

            print(f"[BBP Pack] Finished copying assets. Total copied: {len(self.copied_paths)}, Missing: {len(self.missing_on_copy)}")
            _pack_diag(
                f"Stale recovery: hits={self._recovery_hits} misses={self._recovery_misses} "
                f"time={self._recovery_time_s:.2f}s cache_keys={len(self._recovery_cache)}"
            )
            _diag_copied_udim_tiles(self.target_path)
            if self.missing_on_copy:
                self.missing_on_copy = _filter_ignorable_missing(self.missing_on_copy)
                self.missing_files_all.extend(self.missing_on_copy)
                if self.missing_on_copy:
                    print(f"[BBP Pack]   Missing/offline (will report on complete): {[str(p) for p in self.missing_on_copy[:5]]}...")
            caches_filtered_during_copy = (self.copy_only_mode and
                                         self.frame_start is not None and
                                         self.frame_end is not None and
                                         self.frame_step is not None)
            if (self.frame_start is not None and self.frame_end is not None and
                self.frame_step is not None and self.cache_dirs and
                not caches_filtered_during_copy):
                self.cache_truncate_index = 0
                self.phase = 'TRUNCATING_CACHES'
                return ('TRUNCATING_CACHES', False)
            if caches_filtered_during_copy:
                print(f"[BBP Pack] Caches were filtered during copy, skipping truncation phase")
            self.phase = 'FIND_DEPENDENCIES'
            return ('FIND_DEPENDENCIES', False)
        
        elif self.phase == 'TRUNCATING_CACHES':
            if self.cache_truncate_index == 0:
                print(f"[BBP Pack] Truncating caches to frame range {self.frame_start}-{self.frame_end} (step: {self.frame_step})...")
                self._progress(45.0, "Truncating caches to frame range...")
            units_this_tick = 0
            while self.cache_truncate_index < len(self.cache_dirs) and units_this_tick < batch_size:
                if self.cancel_check and self.cancel_check():
                    raise InterruptedError("Packing cancelled by user")
                cache_dir = self.cache_dirs[self.cache_truncate_index]
                t0 = time.perf_counter()
                if cache_dir.exists() and cache_dir.is_dir():
                    progress_pct = 45.0 + ((self.cache_truncate_index + 1) / len(self.cache_dirs) * 0.5) if self.cache_dirs else 45.0
                    self._progress(
                        progress_pct,
                        f"Truncating caches... ({self.cache_truncate_index + 1}/{len(self.cache_dirs)} cache directories)",
                    )
                    print(f"[BBP Pack]   [{self.cache_truncate_index + 1}/{len(self.cache_dirs)}] Truncating cache: {cache_dir.name}")
                    files_removed = truncate_caches_to_frame_range(cache_dir, self.frame_start, self.frame_end, self.frame_step)
                    print(f"[BBP Pack]   Removed {files_removed} cache files outside frame range")
                self.cache_truncate_index += 1
                units_this_tick += 1
                if self._unit_took_too_long("truncate", cache_dir.name, t0):
                    break
            if self.cache_truncate_index < len(self.cache_dirs):
                return ('TRUNCATING_CACHES', False)
            print(f"[BBP Pack] Finished truncating caches to frame range {self.frame_start}-{self.frame_end}")
            self.phase = 'FIND_DEPENDENCIES'
            return ('FIND_DEPENDENCIES', False)
        
        elif self.phase == 'FIND_DEPENDENCIES':
            print(f"[BBP Pack] Finding blend dependencies...")
            if self.progress_callback:
                self.progress_callback(45.0, "Finding blend dependencies...")
            # Keep BAT map for diagnostics; process every .blend we copied (not just session-visible links).
            self.blend_deps = au.find_blend_asset_usage()
            self.to_remap = _collect_pack_tree_blends(
                self.target_path, self.hero_blend, self.copy_map
            )
            for p in self.to_remap:
                try:
                    label = p.relative_to(self.target_path)
                except ValueError:
                    label = p.name
                print(f"[BBP Pack]   Blend to process: {label}")
            print(f"[BBP Pack] Found {len(self.to_remap)} blend files to process (full pack tree)")
            _pack_diag(f"BAT blend_deps groups: {len(self.blend_deps)}; pack-tree blends: {len(self.to_remap)}")
            self.remap_index = 0
            self.phase = 'REMAP_PATHS'
            return ('REMAP_PATHS', False)
        
        elif self.phase == 'REMAP_PATHS':
            if self.remap_index == 0:
                print(f"[BBP Pack] Remapping library paths in blend files...")
                self._progress(55.0, "Remapping library paths...")
            units_this_tick = 0
            while self.remap_index < len(self.to_remap) and units_this_tick < batch_size:
                if self.cancel_check and self.cancel_check():
                    raise InterruptedError("Packing cancelled by user")
                blend_to_fix = self.to_remap[self.remap_index]
                t0 = time.perf_counter()
                if blend_to_fix.exists():
                    progress_pct = 55.0 + ((self.remap_index + 1) / len(self.to_remap) * 10.0) if self.to_remap else 55.0
                    self._progress(progress_pct, f"Remapping paths... ({self.remap_index + 1}/{len(self.to_remap)})")
                    print(f"[BBP Pack]   [{self.remap_index + 1}/{len(self.to_remap)}] Remapping paths in: {blend_to_fix.name}")
                    unresolved = remap_library_paths(
                        blend_to_fix,
                        self.copy_map,
                        self.common_root,
                        self.target_path,
                        ensure_autopack=self.autopack_on_save,
                        search_roots=self.search_roots,
                    )
                    _pack_diag(
                        f"remap {blend_to_fix.name}: {time.perf_counter() - t0:.2f}s unresolved={len(unresolved or [])}",
                        verbose_only=True,
                    )
                    if unresolved:
                        print(f"[BBP Pack]     WARNING: {len(unresolved)} paths could not be remapped in {blend_to_fix.name}")
                        for up in unresolved[:3]:
                            print(f"[BBP Pack]       - {up}")
                        if len(unresolved) > 3:
                            print(f"[BBP Pack]       ... and {len(unresolved) - 3} more")
                self.remap_index += 1
                units_this_tick += 1
                if self._unit_took_too_long("remap", blend_to_fix.name, t0):
                    break
            if self.remap_index < len(self.to_remap):
                return ('REMAP_PATHS', False)
            print(f"[BBP Pack] Finished remapping library paths")
            if not self.copy_only_mode:
                self.pack_all_index = 0
                self.phase = 'PACK_ALL'
                return ('PACK_ALL', False)
            self.phase = 'COMPLETE'
            return ('COMPLETE', False)
        
        elif self.phase == 'PACK_ALL':
            if self.pack_all_index == 0:
                print(f"[BBP Pack] Packing all assets into blend files...")
                self._progress(65.0, "Packing assets into blend files...")
            units_this_tick = 0
            while self.pack_all_index < len(self.to_remap) and units_this_tick < batch_size:
                if self.cancel_check and self.cancel_check():
                    raise InterruptedError("Packing cancelled by user")
                blend_to_fix = self.to_remap[self.pack_all_index]
                t0 = time.perf_counter()
                if blend_to_fix.exists():
                    progress_pct = 65.0 + ((self.pack_all_index + 1) / len(self.to_remap) * 15.0) if self.to_remap else 65.0
                    self._progress(progress_pct, f"Packing assets... ({self.pack_all_index + 1}/{len(self.to_remap)})")
                    print(f"[BBP Pack]   [{self.pack_all_index + 1}/{len(self.to_remap)}] Packing all in: {blend_to_fix.name}")
                    unpacked = pack_all_in_blend(blend_to_fix, pack_root=self.target_path)
                    if unpacked:
                        self.missing_files_all.extend(unpacked)
                    _pack_diag(
                        f"pack_all {blend_to_fix.name}: {time.perf_counter() - t0:.2f}s unpacked={len(unpacked or [])}",
                        verbose_only=True,
                    )
                self.pack_all_index += 1
                units_this_tick += 1
                if self._unit_took_too_long("pack_all", blend_to_fix.name, t0):
                    break
            if self.pack_all_index < len(self.to_remap):
                return ('PACK_ALL', False)
            print(f"[BBP Pack] Finished packing all assets")
            if self.run_pack_linked:
                self.pack_linked_index = 0
                self.phase = 'PACK_LINKED'
                return ('PACK_LINKED', False)
            self.phase = 'COMPLETE'
            return ('COMPLETE', False)
        
        elif self.phase == 'PACK_LINKED':
            # Parallel pack_linked: poll Popen jobs each tick (deps ||, hero last).
            if self.pack_linked_runner is None:
                print(f"[BBP Pack] Packing linked libraries...")
                self._progress(80.0, "Packing linked libraries...")
                self.pack_linked_runner = PackLinkedParallelRunner(
                    blends=list(self.to_remap or []),
                    hero_blend=self.hero_blend,
                    pack_root=self.target_path,
                    search_roots=self.search_roots or [],
                    max_size_bytes=int(self.max_size_bytes or (2 * 1024 * 1024 * 1024)),
                    workers=int(getattr(config, "PACK_LINKED_WORKERS", 4)),
                    timeout_sec=float(getattr(config, "PACK_LINKED_TIMEOUT_SEC", _blender_subprocess_timeout())),
                    cancel_check=self.cancel_check,
                )
                self.pack_linked_runner.begin()
            try:
                done, msg = self.pack_linked_runner.tick()
            except InterruptedError:
                self.cancel_pack_linked_jobs()
                raise
            total = max(1, self.pack_linked_runner._total)
            finished = self.pack_linked_runner._finished
            progress_pct = 80.0 + (finished / total) * 15.0
            self._progress(progress_pct, msg)
            if self.pack_linked_runner._active:
                # Hang status names the first running blend for the modal strip.
                lead = self.pack_linked_runner._active[0].blend_path.name
                self.hang_phase = "pack_linked"
                self.hang_name = lead
                self.hang_elapsed = time.time() - self.pack_linked_runner._active[0].t0
            if not done:
                return ('PACK_LINKED', False)
            self.missing_files_all.extend(self.pack_linked_runner.missing_files)
            self.oversized_files_all.extend(self.pack_linked_runner.oversized_files)
            self.pack_linked_failed_blends = list(self.pack_linked_runner.failed_blends)
            self.pack_linked_failure_summary = _format_pack_linked_failure_report(
                self.pack_linked_failed_blends
            )
            self.pack_linked_runner = None
            self.phase = 'COMPLETE'
            return ('COMPLETE', False)
        
        elif self.phase == 'COMPLETE':
            if self.pack_linked_failed_blends:
                print(f"[BBP Pack] Pack process finished WITH FAILURES (pack_linked).")
            else:
                print(f"[BBP Pack] Pack process completed successfully!")
            print(f"[BBP Pack] Output directory: {self.target_path}")
            # Flush last phase timing + session totals for optimization
            if self._timed_phase is not None:
                _pack_diag(f"Phase {self._timed_phase} took {time.perf_counter() - self._phase_t0:.2f}s", verbose_only=True)
            _pack_diag(f"PackSession total wall time: {time.perf_counter() - self._session_t0:.2f}s")
            self.missing_files_all = _filter_ignorable_missing(self.missing_files_all)
            _pack_diag(
                f"Final: copied_paths={len(self.copied_paths)} missing={len(self.missing_files_all)} "
                f"recovery_hits={self._recovery_hits} blends_to_process={len(self.to_remap or [])} "
                f"pack_linked_failed={len(self.pack_linked_failed_blends)}"
            )
            _diag_copied_udim_tiles(self.target_path)
            # Flamenco-style: finish OK even with offline files; surface the list for the user
            self.missing_summary = _log_missing_assets_summary(self.missing_files_all)
            if self.pack_linked_failed_blends and not self.pack_linked_failure_summary:
                self.pack_linked_failure_summary = _format_pack_linked_failure_report(
                    self.pack_linked_failed_blends
                )
            
            # Determine file path for submission
            if self.copy_only_mode:
                # For copy-only, we'll create ZIP later in the operator
                self.file_path = None
            else:
                # For pack-and-save, return the hero blend
                if self.hero_blend and self.hero_blend.exists():
                    self.file_path = self.hero_blend
                    print(f"[BBP Pack] Target blend file for submission: {self.file_path}")
                else:
                    # Fallback: find the first .blend file in target_path
                    blend_files = list(self.target_path.rglob("*.blend"))
                    if blend_files:
                        self.file_path = blend_files[0]
                        print(f"[BBP Pack] Found blend file for submission: {self.file_path}")
            
            return ('COMPLETE', True)
        
        return (self.phase, False)


def pack_project(workflow: str, target_path: Optional[Path] = None,
                 progress_callback=None, cancel_check=None) -> Tuple[Path, Optional[Path]]:
    """
    Main packing function.

    Args:
        workflow: Either 'copy-only' or 'pack-and-save'
        target_path: Target directory (if None, uses temp directory)

    Returns:
        Tuple of (target_path: Path, file_path: Optional[Path])
        - target_path: Path to the packed output directory
        - file_path: Path to the file to submit (ZIP for copy-only, blend for pack-and-save)
    """
    print(f"[BBP Pack] Starting pack process: workflow={workflow}")
    
    if target_path is None:
        target_path = Path(tempfile.mkdtemp(prefix="bbp_pack_"))
        print(f"[BBP Pack] Created temporary directory: {target_path}")
    else:
        print(f"[BBP Pack] Using provided target path: {target_path}")
    
    copy_only_mode = workflow == WorkflowMode.COPY_ONLY
    autopack_on_save = not copy_only_mode
    run_pack_linked = not copy_only_mode
    try:
        exclude_av = bool(getattr(bpy.context.scene.bbp_pack, "exclude_av", False))
    except Exception:
        exclude_av = False
    
    print(f"[BBP Pack] Mode: {'COPY_ONLY' if copy_only_mode else 'PACK_AND_SAVE'}")
    print(f"[BBP Pack] Autopack on save: {autopack_on_save}, Pack linked: {run_pack_linked}")
    if exclude_av:
        print(f"[BBP Pack] Exclude video/audio enabled (omitted from pack)")
    
    dead_assets = find_dead_unc_assets()
    if dead_assets:
        kind, name, path, detail = dead_assets[0]
        if len(dead_assets) > 1:
            detail = f"{detail}; (+{len(dead_assets) - 1} more)"
        err = DeadUncAssetError(path, kind, name, detail)
        print(f"[BBP Pack] ERROR: {err}")
        raise err

    # Find asset usages
    print(f"[BBP Pack] Finding asset usages...")
    if progress_callback:
        progress_callback(5.0, "Finding asset usages...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
    t_find = time.perf_counter()
    asset_usages = au.find()
    _pack_diag(f"au.find() took {time.perf_counter() - t_find:.2f}s")
    hero_abs = au.library_abspath(None).resolve()
    print(f"[BBP Pack] Found {len(asset_usages)} libraries with assets")
    print(f"[BBP Pack] Hero blend: {hero_abs}")
    _diag_summarize_asset_usages(asset_usages)
    _diag_session_udim_gap(asset_usages)
    
    # Collect all file paths
    print(f"[BBP Pack] Collecting all file paths...")
    if progress_callback:
        progress_callback(10.0, "Collecting file paths...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
    all_filepaths = []
    all_filepaths.extend(au.library_abspath(lib) for lib in asset_usages.keys())
    all_filepaths.extend(
        asset_usage.abspath
        for asset_usages in asset_usages.values()
        for asset_usage in asset_usages
    )
    print(f"[BBP Pack] Collected {len(all_filepaths)} total file paths")
    
    # Determine common root
    print(f"[BBP Pack] Determining common root directory...")
    try:
        common_root_str = os.path.commonpath(all_filepaths)
        print(f"[BBP Pack] Common root (method 1): {common_root_str}")
    except ValueError:
        print(f"[BBP Pack] Method 1 failed, trying drive-based approach...")
        blend_file_drive = Path(bpy.data.filepath).drive if hasattr(Path(bpy.data.filepath), 'drive') else ""
        project_filepaths = [p for p in all_filepaths if getattr(p, "drive", "") == blend_file_drive]
        if project_filepaths:
            common_root_str = os.path.commonpath(project_filepaths)
            print(f"[BBP Pack] Common root (method 2): {common_root_str}")
        else:
            common_root_str = str(Path(bpy.data.filepath).parent)
            print(f"[BBP Pack] Common root (fallback): {common_root_str}")
    
    if not common_root_str:
        raise ValueError("Could not find a common root directory for these assets.")
    
    common_root = Path(common_root_str)
    print(f"[BBP Pack] Using common root: {common_root}")
    
    # Copy files
    print(f"[BBP Pack] Starting file copy process...")
    if progress_callback:
        progress_callback(15.0, "Starting file copy process...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
    copied_paths = set()
    copy_map = {}
    missing_on_copy = []
    
    # Copy hero blend
    print(f"[BBP Pack] Copying hero blend...")
    current_blend_abspath = hero_abs
    try:
        current_relpath = current_blend_abspath.relative_to(common_root)
        print(f"[BBP Pack]   Relative path: {current_relpath}")
    except ValueError:
        current_relpath = compute_target_relpath(current_blend_abspath, common_root)
        print(f"[BBP Pack]   Computed relative path: {current_relpath}")
    
    hero_blend = None
    current_blend_resolved = current_blend_abspath.resolve()
    if current_blend_resolved not in copied_paths:
        target_path_file = target_path / current_relpath
        print(f"[BBP Pack]   Copying: {current_blend_abspath} -> {target_path_file}")
        try:
            target_path_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current_blend_abspath, target_path_file)
            copied_paths.add(current_blend_resolved)
            if current_blend_abspath.suffix.lower() == ".blend":
                copy_map[_norm_copy_map_key(current_blend_resolved)] = str(target_path_file.resolve())
            hero_blend = target_path_file.resolve()
            print(f"[BBP Pack]   Copied successfully, size: {target_path_file.stat().st_size} bytes")
            # Copy caches
            print(f"[BBP Pack]   Copying blend caches...")
            # Legacy function doesn't support frame range filtering - copy all caches
            cache_count = copy_blend_caches(current_blend_abspath, target_path_file, missing_on_copy,
                                          frame_start=None, frame_end=None, frame_step=None,
                                          copy_map_out=copy_map)
            print(f"[BBP Pack]   Copied {cache_count} cache directories")
        except Exception as e:
            print(f"[BBP Pack]   ERROR copying hero blend: {type(e).__name__}: {str(e)}")
            missing_on_copy.append(current_blend_abspath)
    
    # Copy other assets
    total_assets = sum(len(links) for links in asset_usages.values())
    print(f"[BBP Pack] Copying {total_assets} asset files...")
    existing_for_roots = [
        a.abspath
        for links in asset_usages.values()
        for a in links
        if a.abspath.exists()
    ]
    search_roots = collect_search_roots(common_root, existing_for_roots, hero_abs.parent if hero_abs else None)
    recovery_cache: dict = {}
    recovery_hits = 0
    recovery_misses = 0
    recovery_time_s = 0.0
    print(f"[BBP Pack] Stale-path search roots: {len(search_roots)}")
    for r in search_roots[:24]:
        _pack_diag(f"  root: {r}", verbose_only=True)
    asset_count = 0
    seen_resolved = set()
    for lib, links_to in asset_usages.items():
        for asset_usage in links_to:
            stale_path = asset_usage.abspath
            try:
                stale_resolved = stale_path.resolve()
            except OSError:
                stale_resolved = Path(stale_path)
            if stale_resolved in copied_paths or stale_resolved in seen_resolved:
                continue
            seen_resolved.add(stale_resolved)
            asset_count += 1
            # Update progress every 10 files or every 1% of total
            if asset_count % 10 == 0 or (total_assets > 0 and asset_count % max(1, total_assets // 100) == 0):
                progress_pct = 15.0 + (asset_count / total_assets * 30.0) if total_assets > 0 else 15.0
                if progress_callback:
                    progress_callback(progress_pct, f"Copying assets... ({asset_count}/{total_assets})")
                print(f"[BBP Pack]   Copied {asset_count}/{total_assets} assets ({progress_pct:.1f}%)...")
            if cancel_check and cancel_check():
                raise InterruptedError("Packing cancelled by user")
            
            if _is_excluded_media_path(stale_path, exclude_av):
                print(f"[BBP Pack]   Skipping excluded media: {stale_path.name}")
                continue
            if _is_ignorable_missing_asset(stale_path):
                _pack_diag(f"Skip copy (ignorable): {stale_path.name}", verbose_only=True)
                continue
            src_path = stale_path
            if not src_path.exists():
                t_rec = time.perf_counter()
                recovered = recover_stale_path(stale_path, search_roots, recovery_cache)
                recovery_time_s += time.perf_counter() - t_rec
                if recovered is None:
                    recovery_misses += 1
                    print(f"[BBP Pack]   WARNING: Asset does not exist: {stale_path}")
                    missing_on_copy.append(stale_path)
                    continue
                recovery_hits += 1
                _pack_diag(f"Recovered: {stale_path.name} -> {recovered}", verbose_only=True)
                src_path = recovered
            try:
                src_resolved = src_path.resolve()
            except OSError:
                src_resolved = Path(src_path)
            if src_resolved in copied_paths:
                dest = copy_map.get(_norm_copy_map_key(src_resolved))
                if dest:
                    copy_map[_norm_copy_map_key(stale_path)] = dest
                continue
            try:
                asset_relpath = src_resolved.relative_to(common_root)
            except ValueError:
                asset_relpath = compute_target_relpath(src_resolved, common_root)
            
            target_asset_path = target_path / asset_relpath
            try:
                target_asset_path.parent.mkdir(parents=True, exist_ok=True)
                file_size = src_path.stat().st_size
                shutil.copy2(src_path, target_asset_path)
                copied_paths.add(src_resolved)
                copied_paths.add(stale_resolved)
                _copy_map_register(copy_map, stale_path, src_resolved, target_asset_path)
                if asset_count <= 5 or asset_count % 50 == 0:  # Log first 5 and every 50th
                    print(f"[BBP Pack]   Copied: {src_path.name} ({file_size} bytes)")
            except Exception as e:
                print(f"[BBP Pack]   ERROR copying asset {src_path.name}: {type(e).__name__}: {str(e)}")
                if not _is_ignorable_missing_asset(stale_path):
                    missing_on_copy.append(stale_path)
    
    print(f"[BBP Pack] Finished copying assets. Total copied: {len(copied_paths)}, Missing: {len(missing_on_copy)}")
    _pack_diag(
        f"Stale recovery: hits={recovery_hits} misses={recovery_misses} "
        f"time={recovery_time_s:.2f}s cache_keys={len(recovery_cache)}"
    )
    _diag_copied_udim_tiles(target_path)
    missing_on_copy[:] = _filter_ignorable_missing(missing_on_copy)
    missing_files_report = list(missing_on_copy)
    if missing_on_copy:
        print(f"[BBP Pack]   Missing/offline (will report on complete): {[str(p) for p in missing_on_copy[:5]]}...")
    
    # Remap library paths
    print(f"[BBP Pack] Finding blend dependencies...")
    if progress_callback:
        progress_callback(45.0, "Finding blend dependencies...")
    if cancel_check and cancel_check():
        raise InterruptedError("Packing cancelled by user")
    blend_deps = au.find_blend_asset_usage()
    to_remap = _collect_pack_tree_blends(target_path, hero_blend, copy_map)
    print(f"[BBP Pack] Found {len(to_remap)} blend files to process (full pack tree; BAT groups={len(blend_deps)})")
    
    # Remap library paths
    print(f"[BBP Pack] Remapping library paths in blend files...")
    if progress_callback:
        progress_callback(55.0, "Remapping library paths...")
    for i, blend_to_fix in enumerate(to_remap, 1):
        if cancel_check and cancel_check():
            raise InterruptedError("Packing cancelled by user")
        if blend_to_fix.exists():
            progress_pct = 55.0 + (i / len(to_remap) * 10.0) if to_remap else 55.0
            if progress_callback:
                progress_callback(progress_pct, f"Remapping paths... ({i}/{len(to_remap)})")
            print(f"[BBP Pack]   [{i}/{len(to_remap)}] Remapping paths in: {blend_to_fix.name}")
            remap_library_paths(
                blend_to_fix,
                copy_map,
                common_root,
                target_path,
                ensure_autopack=autopack_on_save,
                search_roots=search_roots,
            )
    print(f"[BBP Pack] Finished remapping library paths")
    
    # Pack files if not copy-only
    if not copy_only_mode:
        print(f"[BBP Pack] Packing all assets into blend files...")
        if progress_callback:
            progress_callback(65.0, "Packing assets into blend files...")
        for i, blend_to_fix in enumerate(to_remap, 1):
            if cancel_check and cancel_check():
                raise InterruptedError("Packing cancelled by user")
            if blend_to_fix.exists():
                progress_pct = 65.0 + (i / len(to_remap) * 15.0) if to_remap else 65.0
                if progress_callback:
                    progress_callback(progress_pct, f"Packing assets... ({i}/{len(to_remap)})")
                print(f"[BBP Pack]   [{i}/{len(to_remap)}] Packing all in: {blend_to_fix.name}")
                unpacked = pack_all_in_blend(blend_to_fix, pack_root=target_path)
                if unpacked:
                    missing_files_report.extend(unpacked)
        print(f"[BBP Pack] Finished packing all assets")
        
        if run_pack_linked:
            print(f"[BBP Pack] Packing linked libraries...")
            if progress_callback:
                progress_callback(80.0, "Packing linked libraries...")
            max_size_bytes = _get_project_size_limit_bytes()
            runner = PackLinkedParallelRunner(
                blends=list(to_remap or []),
                hero_blend=hero_blend,
                pack_root=target_path,
                search_roots=search_roots or [],
                max_size_bytes=int(max_size_bytes or (2 * 1024 * 1024 * 1024)),
                workers=int(getattr(config, "PACK_LINKED_WORKERS", 4)),
                timeout_sec=float(getattr(config, "PACK_LINKED_TIMEOUT_SEC", _blender_subprocess_timeout())),
                cancel_check=cancel_check,
            )
            missing_files_all, _oversized_sync = runner.run_blocking(
                progress_callback=progress_callback,
                progress_base=80.0,
                progress_span=15.0,
            )
            missing_files_report.extend(missing_files_all)
            if runner.failed_blends:
                _format_pack_linked_failure_report(runner.failed_blends)
    
    if any(str(p).startswith("pack_linked_timeout:") or str(p).startswith("pack_linked_start_failed:") for p in missing_files_report):
        print(f"[BBP Pack] Pack process finished WITH FAILURES (pack_linked).")
    else:
        print(f"[BBP Pack] Pack process completed successfully!")
    _log_missing_assets_summary(missing_files_report)
    print(f"[BBP Pack] Output directory: {target_path}")
    
    # Determine file path for submission
    file_path = None
    if copy_only_mode:
        # For copy-only, we'll create ZIP later in the operator. Return None here, ZIP will be created in operator
        pass
    else:
        # For pack-and-save, return the hero blend
        if hero_blend and hero_blend.exists():
            file_path = hero_blend
            print(f"[BBP Pack] Target blend file for submission: {file_path}")
        else:
            # Fallback: find the first .blend file in target_path
            blend_files = list(target_path.rglob("*.blend"))
            if blend_files:
                file_path = blend_files[0]
                print(f"[BBP Pack] Found blend file for submission: {file_path}")
    
    return target_path, file_path


class BBP_OT_pack_zip(Operator):
    """Pack project as ZIP (for scenes with caches) - creates ZIP and saves to output location."""
    bl_idname = "bbp.pack_zip"
    bl_label = "Pack as ZIP"
    bl_description = "Copy assets without packing (for scenes with caches), create ZIP, and save to output location"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        """Start the packing operation."""
        pack_settings = context.scene.bbp_pack
        
        # Check if already packing
        if pack_settings.is_packing:
            self.report({'WARNING'}, "A packing operation is already in progress.")
            return {'CANCELLED'}
        
        # Get output path from settings or preferences
        output_dir = pack_settings.output_path
        if not output_dir:
            from ..utils.compat import get_addon_prefs
            prefs = get_addon_prefs()
            if prefs and prefs.default_output_path:
                output_dir = prefs.default_output_path
                pack_settings.output_path = output_dir
        
        if not output_dir:
            self.report({'ERROR'}, "Please specify an output path in the panel below.")
            return {'CANCELLED'}
        
        # Generate filename (will be set after ZIP creation with pack indicator)
        blend_name = bpy.data.filepath if bpy.data.filepath else "untitled"
        if blend_name:
            blend_name = Path(blend_name).stem
        else:
            blend_name = "untitled"
        # Output path will be set after ZIP creation with pack indicator
        self._output_dir = Path(output_dir)
        
        # Initialize progress properties
        pack_settings.is_packing = True
        pack_settings.cancel_requested = False
        pack_settings.pack_progress = 0.0
        pack_settings.pack_status_message = "Initializing..."
        
        # Initialize phase tracking
        self._phase = 'INIT'
        self._output_path = None  # Will be set after ZIP creation
        self._original_filepath = bpy.data.filepath
        self._temp_blend_path = None
        self._temp_dir = None
        self._target_path = None
        self._zip_path = None
        self._frame_start = None
        self._frame_end = None
        self._frame_step = None
        self._success = False
        self._message = ""
        self._error = None
        self._packer = None  # IncrementalPacker instance
        self._pack_t0 = time.perf_counter()  # End-to-end wall clock (invoke → finish/cancel)
        # OS taskbar via ITaskbarList3 (Atomic wm_progress pattern)
        wm_progress.begin()
        wm_progress.set_progress(0.0)
        
        # Create timer for modal updates
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
        
        # Start modal operation
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)
    
    def modal(self, context, event):
        """Handle modal events and update progress."""
        pack_settings = context.scene.bbp_pack
        
        # Debug: Log all events (but filter out noisy ones)
        if event.type not in ('TIMER', 'MOUSEMOVE', 'WINDOW_DEACTIVATE'):
            _pack_debug(f"Modal event received: type={event.type}, value={getattr(event, 'value', 'N/A')}")
        
        # Handle Esc / Cancel button
        if event.type == 'ESC' or pack_settings.cancel_requested:
            _pack_debug(f"Cancel requested, cancelling")
            self._cleanup(context, cancelled=True)
            dur = getattr(self, "_pack_duration", "") or ""
            self.report({'INFO'}, f"Packing cancelled ({dur})." if dur else "Packing cancelled.")
            return {'CANCELLED'}
        
        # Handle timer events
        if event.type == 'TIMER':
            try:
                _pack_debug(f"Modal timer event, current phase: {self._phase}")
                wm_progress.set_progress(pack_settings.pack_progress)
                
                if self._phase == 'INIT':
                    _pack_debug(f"Entering INIT phase")
                    pack_settings.pack_progress = 0.0
                    pack_settings.pack_status_message = "Initializing..."
                    self._phase = 'SAVING_BLEND'
                    _pack_debug(f"Transitioning to SAVING_BLEND phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_BLEND':
                    _pack_debug(f"Entering SAVING_BLEND phase")
                    pack_settings.pack_progress = 5.0
                    pack_settings.pack_status_message = "Saving current blend state..."
                    
                    from .export_ops import save_current_blend_with_frame_range, apply_frame_range_to_blend
                    
                    _pack_debug(f"About to call save_current_blend_with_frame_range")
                    try:
                        self._temp_blend_path, self._frame_start, self._frame_end, self._frame_step = save_current_blend_with_frame_range(pack_settings)
                        self._temp_dir = self._temp_blend_path.parent
                        _pack_debug(f"save_current_blend_with_frame_range completed")
                        print(f"[BBP Pack] Saved to temp file: {self._temp_blend_path}")
                        _pack_debug(f"Frame range: {self._frame_start}-{self._frame_end} (step: {self._frame_step})")
                    except Exception as e:
                        _pack_debug(f"ERROR in SAVING_BLEND: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = f"Failed to save current blend state: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'APPLYING_FRAME_RANGE'
                    _pack_debug(f"Transitioning to APPLYING_FRAME_RANGE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE':
                    _pack_debug(f"Entering APPLYING_FRAME_RANGE phase")
                    pack_settings.pack_progress = 10.0
                    pack_settings.pack_status_message = "Frame range applied."
                    # Frame range is already applied in save_current_blend_with_frame_range
                    self._phase = 'OVERRIDING_FILEPATH'
                    _pack_debug(f"Transitioning to OVERRIDING_FILEPATH phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'OVERRIDING_FILEPATH':
                    _pack_debug(f"Entering OVERRIDING_FILEPATH phase")
                    pack_settings.pack_progress = 12.0
                    pack_settings.pack_status_message = "Preparing for packing..."
                    
                    _pack_debug(f"Temp file exists: {self._temp_blend_path.exists() if self._temp_blend_path else 'N/A'}")
                    _pack_debug(f"Current bpy.data.filepath: {bpy.data.filepath}")
                    
                    # Temporarily override library_abspath to use temp file instead of opening it. This avoids invalidating the operator instance
                    import functools

                    self._original_library_abspath = au.library_abspath
                    temp_file_path = self._temp_blend_path.resolve()

                    def override_library_abspath(lib):
                        if lib is None:
                            return temp_file_path
                        return self._original_library_abspath(lib)

                    au.library_abspath.cache_clear()
                    au.library_abspath = override_library_abspath
                    # Re-apply lru_cache decorator behavior by wrapping
                    au.library_abspath = functools.lru_cache(maxsize=None)(override_library_abspath)
                    
                    _pack_debug(f"Overrode library_abspath to use temp file: {temp_file_path}")
                    
                    # Initialize IncrementalPacker
                    def progress_callback(progress_pct, message):
                        """Update progress during packing."""
                        # Map packer progress (0-100%) to operator progress (15-61%)
                        pack_settings.pack_progress = 15.0 + (progress_pct * 0.46)
                        pack_settings.pack_status_message = message
                        wm_progress.set_progress(pack_settings.pack_progress)
                        _pack_debug(f"Progress update: {pack_settings.pack_progress:.1f}% - {message}")
                        # Force UI redraw on every update
                        for area in context.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
                    
                    def cancel_check():
                        """True when Cancel / Esc requested."""
                        return bool(pack_settings.cancel_requested)
                    
                    max_size_bytes = _get_project_size_limit_bytes(context)
                    exclude_av = bool(getattr(pack_settings, 'exclude_av', False))
                    self._packer = IncrementalPacker(
                        WorkflowMode.COPY_ONLY,
                        target_path=None,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                        frame_start=self._frame_start,
                        frame_end=self._frame_end,
                        frame_step=self._frame_step,
                        temp_blend_path=self._temp_blend_path,
                        original_blend_path=Path(self._original_filepath) if self._original_filepath else None,
                        max_size_bytes=max_size_bytes,
                        exclude_av=exclude_av,
                    )
                    
                    self._phase = 'PACKING_INIT'
                    _pack_debug(f"Transitioning to PACKING_INIT phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'PACKING_INIT' or self._phase.startswith('PACKING_'):
                    # Handle all packing sub-phases using IncrementalPacker
                    try:
                        # Process one batch
                        # Unbounded tick + hang-threshold yield (Atomic scanner pattern).
                        next_phase, is_complete = self._packer.process_batch()
                        
                        if is_complete:
                            # Packing is complete
                            self._target_path = self._packer.target_path
                            _pack_debug(f"Incremental packing completed")
                            print(f"[BBP Pack] Packed to: {self._target_path}")
                            context.scene.bbp_pack.pack_output_path = str(self._target_path)
                            
                            # Check for oversized files that couldn't be packed
                            if self._packer.oversized_files_all:
                                oversized_list = "\n".join(f"  - {f.name if f.name else f} ({f.stat().st_size / (1024**3):.2f} GB)" 
                                                          for f in self._packer.oversized_files_all[:10])
                                if len(self._packer.oversized_files_all) > 10:
                                    oversized_list += f"\n  ... and {len(self._packer.oversized_files_all) - 10} more"
                                warning_msg = (
                                    f"Warning: {len(self._packer.oversized_files_all)} linked file(s) over size limit could not be packed:\n"
                                    f"{oversized_list}\n\n"
                                    "Blender cannot pack linked files over the project size limit. These files will remain as external references.\n"
                                    "To fix: Reduce the size of these files or split them into smaller files."
                                )
                                print(f"[BBP Pack] {warning_msg}")
                                # Report as warning (non-blocking)
                                self.report({'WARNING'}, f"{len(self._packer.oversized_files_all)} linked file(s) over size limit could not be packed")
                            
                            self._phase = 'APPLYING_FRAME_RANGE_TO_HERO'
                            _pack_debug(f"Transitioning to APPLYING_FRAME_RANGE_TO_HERO phase")
                        else:
                            # Continue with next phase from packer (prepend PACKING_ prefix)
                            self._phase = f'PACKING_{next_phase}'
                            _pack_debug(f"Packing phase: {self._phase}, continuing...")
                        
                        return {'RUNNING_MODAL'}
                    except InterruptedError as e:
                        _pack_debug(f"Packing cancelled by user")
                        self._cleanup(context, cancelled=True)
                        dur = getattr(self, "_pack_duration", "") or ""
                        self.report({'INFO'}, f"Packing cancelled ({dur})." if dur else "Packing cancelled.")
                        return {'CANCELLED'}
                    except Exception as e:
                        _pack_debug(f"ERROR in PACKING: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = str(e) if isinstance(e, DeadUncAssetError) else f"Packing failed: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error.split('\n')[0])
                        return {'CANCELLED'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE_TO_HERO':
                    _pack_debug(f"Entering APPLYING_FRAME_RANGE_TO_HERO phase")
                    pack_settings.pack_progress = 60.0
                    pack_settings.pack_status_message = "Applying frame range to hero blend..."
                    
                    from .export_ops import apply_frame_range_to_blend
                    
                    # Apply frame range only to the hero blend, not dependent blends.
                    # ZIP/copy-only must not seal via pack_linked — that embeds libraries into the hero.
                    hero_blend = self._packer.hero_blend if self._packer else None
                    if hero_blend and hero_blend.exists():
                        _pack_debug(f"Applying frame range to hero blend: {hero_blend.name}")
                        apply_frame_range_to_blend(hero_blend, self._frame_start, self._frame_end, self._frame_step)
                        for area in context.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
                    else:
                        _pack_debug(f"No hero blend to apply frame range to")
                    
                    self._phase = 'RESTORING_LIBRARY_ABSPATH'
                    _pack_debug(f"Transitioning to RESTORING_LIBRARY_ABSPATH phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'RESTORING_LIBRARY_ABSPATH':
                    _pack_debug(f"Entering RESTORING_LIBRARY_ABSPATH phase")
                    pack_settings.pack_progress = 62.0
                    pack_settings.pack_status_message = "Restoring file paths..."
                    
                    # Restore original library_abspath function
                    if hasattr(self, '_original_library_abspath'):
                        au.library_abspath.cache_clear()
                        au.library_abspath = self._original_library_abspath
                        _pack_debug(f"Restored original library_abspath function")
                    
                    self._phase = 'VALIDATING_FILE_SIZE'
                    _pack_debug(f"Transitioning to VALIDATING_FILE_SIZE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_FILE_SIZE':
                    _pack_debug(f"Entering VALIDATING_FILE_SIZE phase (before ZIP)")
                    pack_settings.pack_progress = 64.0
                    pack_settings.pack_status_message = "Validating file size..."
                    
                    # Estimate packed directory size
                    total_size = 0
                    file_count = 0
                    for root, dirs, files in os.walk(self._target_path):
                        for file in files:
                            file_path = Path(root) / file
                            if file_path.exists():
                                total_size += file_path.stat().st_size
                                file_count += 1
                    
                    total_size_gb = total_size / (1024 * 1024 * 1024)
                    print(f"[BBP Pack] Estimated packed directory size: {total_size_gb:.2f} GB ({file_count} files)")
                    max_bytes = _get_project_size_limit_bytes(context)
                    if max_bytes is not None and total_size > max_bytes:
                        limit_gb = max_bytes / (1024 * 1024 * 1024)
                        error_msg = (
                            f"Estimated packed size ({total_size_gb:.2f} GB) exceeds project limit ({limit_gb:.1f} GB). Cannot create ZIP.\n\n"
                            "To reduce file size, consider:\n"
                            "- Optimizing the scene (reduce geometry, simplify materials)\n"
                            "- Optimizing asset files (compress textures, reduce resolution)\n"
                            "- Splitting the frame range (render in smaller chunks)\n"
                            "- Truncating caches to match your selected frame range\n"
                            "  (Note: Caches are automatically truncated to your selected frame range during packing)"
                        )
                        print(f"[BBP Pack] ERROR: {error_msg}")
                        self._error = error_msg
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'CREATING_ZIP'
                    _pack_debug(f"Transitioning to CREATING_ZIP phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'CREATING_ZIP':
                    _pack_debug(f"Entering CREATING_ZIP phase")
                    pack_settings.pack_progress = 65.0
                    pack_settings.pack_status_message = "Creating ZIP archive..."
                    
                    from .export_ops import (
                        create_zip_from_directory,
                        prepare_pack_directory_for_farm_zip,
                    )
                    
                    self._zip_path = self._target_path.parent / f"{self._target_path.name}.zip"
                    _pack_debug(f"Creating ZIP: {self._zip_path}")
                    _pack_debug(f"Source directory: {self._target_path}")
                    
                    # Create progress callback for ZIP creation
                    def zip_progress_callback(progress_pct, message):
                        """Update progress during ZIP creation."""
                        # Map 0-100% to 65-80% range
                        pack_settings.pack_progress = 65.0 + (progress_pct * 0.15)
                        pack_settings.pack_status_message = message
                        _pack_debug(f"ZIP progress: {pack_settings.pack_progress:.1f}% - {message}")
                        # Force UI redraw on every update
                        for area in context.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
                    
                    def zip_cancel_check():
                        """True when Cancel / Esc requested."""
                        return bool(pack_settings.cancel_requested)
                    
                    try:
                        # SheepIt / farms reject ``#`` and non-ASCII in ZIP paths (e.g. ``#000000.png``).
                        pack_settings.pack_status_message = "Sanitizing farm-unsafe paths..."
                        renamed = prepare_pack_directory_for_farm_zip(self._target_path)
                        if renamed:
                            print(f"[BBP Pack] Farm-safe path sanitization renamed {renamed} path(s)")

                        exclude_av = getattr(context.scene.bbp_pack, 'exclude_av', False)
                        create_zip_from_directory(
                            self._target_path,
                            self._zip_path,
                            progress_callback=zip_progress_callback,
                            cancel_check=zip_cancel_check,
                            exclude_av=exclude_av,
                        )
                        
                        # Prefer {blend}.zip; on conflict use {blend}_{pack_indicator}.zip (conflict vs output_dir; token from tmp pack dir).
                        if self._original_filepath:
                            blend_name = Path(self._original_filepath).stem
                        elif self._temp_blend_path:
                            blend_name = self._temp_blend_path.stem
                        else:
                            blend_name = "untitled"
                        final_name = _unique_pack_output_path(
                            self._output_dir, blend_name, ".zip", self._target_path
                        ).name
                        staged_zip = self._zip_path.parent / final_name
                        if self._zip_path.exists() and self._zip_path != staged_zip:
                            self._zip_path.rename(staged_zip)
                            self._zip_path = staged_zip
                            print(f"[BBP Pack] Renamed ZIP to: {final_name}")
                        
                        pack_settings.pack_progress = 80.0
                        pack_settings.pack_status_message = "ZIP archive created"
                        _pack_debug(f"ZIP creation completed")
                        print(f"[BBP Pack] Creating ZIP: {self._zip_path}")
                    except InterruptedError as e:
                        _pack_debug(f"ZIP creation cancelled by user")
                        self._cleanup(context, cancelled=True)
                        dur = getattr(self, "_pack_duration", "") or ""
                        self.report({'INFO'}, f"ZIP creation cancelled ({dur})." if dur else "ZIP creation cancelled.")
                        return {'CANCELLED'}
                    except Exception as e:
                        _pack_debug(f"ERROR creating ZIP: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = f"ZIP creation failed: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'VALIDATING_ZIP_SIZE'
                    _pack_debug(f"Transitioning to VALIDATING_ZIP_SIZE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_ZIP_SIZE':
                    _pack_debug(f"Entering VALIDATING_ZIP_SIZE phase")
                    pack_settings.pack_progress = 80.5
                    pack_settings.pack_status_message = "Validating ZIP size..."
                    
                    # Check final ZIP size
                    if self._zip_path and self._zip_path.exists():
                        zip_size = self._zip_path.stat().st_size
                        zip_size_gb = zip_size / (1024 * 1024 * 1024)
                        print(f"[BBP Pack] Final ZIP size: {zip_size_gb:.2f} GB")
                        max_bytes = _get_project_size_limit_bytes(context)
                        if max_bytes is not None and zip_size > max_bytes:
                            limit_gb = max_bytes / (1024 * 1024 * 1024)
                            error_msg = (
                                f"ZIP size ({zip_size_gb:.2f} GB) exceeds project limit ({limit_gb:.1f} GB). Cannot submit.\n\n"
                                "To reduce file size, consider:\n"
                                "- Optimizing the scene (reduce geometry, simplify materials)\n"
                                "- Optimizing asset files (compress textures, reduce resolution)\n"
                                "- Splitting the frame range (render in smaller chunks)\n"
                                "- Truncating caches to match your selected frame range\n"
                                "  (Note: Caches are automatically truncated to your selected frame range during packing)"
                            )
                            print(f"[BBP Pack] ERROR: {error_msg}")
                            self._error = error_msg
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    
                    self._phase = 'SAVING_FILE'
                    _pack_debug(f"Transitioning to SAVING_FILE phase")
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_FILE':
                    pack_settings.pack_progress = 85.0
                    pack_settings.pack_status_message = "Saving ZIP to output location..."
                    
                    try:
                        # Ensure output directory exists
                        self._output_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Move ZIP file to output location (use the renamed ZIP path)
                        final_zip_path = self._output_dir / self._zip_path.name
                        import shutil
                        shutil.move(str(self._zip_path), str(final_zip_path))
                        self._zip_path = final_zip_path
                        self._output_path = final_zip_path
                        
                        print(f"[BBP Pack] Saved ZIP file to: {self._output_path}")
                        self._success = True
                        self._message = f"ZIP file saved to: {self._output_path}"
                        
                        pack_settings.pack_progress = 95.0
                        pack_settings.pack_status_message = "File saved successfully!"
                        self._phase = 'CLEANUP'
                    except Exception as e:
                        self._error = f"Failed to save file: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'CLEANUP':
                    pack_settings.pack_progress = 98.0
                    pack_settings.pack_status_message = "Cleaning up..."
                    
                    # Clean up temp file on success
                    if self._temp_blend_path and self._temp_blend_path.exists():
                        try:
                            self._temp_blend_path.unlink()
                            if self._temp_dir and self._temp_dir.exists():
                                try:
                                    self._temp_dir.rmdir()
                                except Exception:
                                    pass  # Directory may not be empty
                            print(f"[BBP Pack] Cleaned up temp file: {self._temp_blend_path}")
                        except Exception as e:
                            print(f"[BBP Pack] WARNING: Could not clean up temp file: {e}")
                    
                    self._phase = 'COMPLETE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'COMPLETE':
                    pack_settings.pack_progress = 100.0
                    wm_progress.set_progress(100)
                    missing_summary = getattr(self._packer, "missing_summary", "") if self._packer else ""
                    pack_linked_fail = getattr(self._packer, "pack_linked_failure_summary", "") if self._packer else ""
                    dur = _report_pack_duration(getattr(self, "_pack_t0", None))
                    if pack_linked_fail:
                        done_msg = f"Pack finished with Pack Linked failures in {dur}" if dur else "Pack finished with Pack Linked failures"
                    else:
                        done_msg = f"Packing complete in {dur}!" if dur else "Packing complete!"
                    status_extra = pack_linked_fail or missing_summary
                    pack_settings.pack_status_message = (
                        f"{done_msg.rstrip('!')} — {status_extra}" if status_extra else done_msg
                    )
                    
                    # Small delay to show completion
                    time.sleep(0.2)
                    
                    self._cleanup(context, cancelled=False)
                    saved_msg = self._message if self._message else f"File saved to: {self._output_path}"
                    if dur:
                        saved_msg = f"{saved_msg} ({dur})"
                    self.report({'INFO'}, saved_msg)
                    if pack_linked_fail:
                        self.report({'ERROR'}, pack_linked_fail)
                    if missing_summary:
                        self.report({'WARNING'}, missing_summary)
                    return {'FINISHED'}
                
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._error = str(e) if isinstance(e, DeadUncAssetError) else f"Packing failed: {type(e).__name__}: {str(e)}"
                self._cleanup(context, cancelled=True)
                self.report({'ERROR'}, self._error.split('\n')[0])
                return {'CANCELLED'}
        
        # Let UI clicks reach Cancel / other panels while the timer drives packing.
        return {'PASS_THROUGH'}
    
    def _cleanup(self, context, cancelled=False):
        """Clean up progress properties and timer."""
        pack_settings = context.scene.bbp_pack
        # One cancel/error timing report per run (clear t0 so cleanup can't double-print).
        if cancelled and getattr(self, "_pack_t0", None) is not None:
            self._pack_duration = _report_pack_duration(self._pack_t0, cancelled=True)
            self._pack_t0 = None

        # Kill parallel pack_linked Blender children if still running.
        packer = getattr(self, "_packer", None)
        if packer is not None and hasattr(packer, "cancel_pack_linked_jobs"):
            try:
                packer.cancel_pack_linked_jobs()
            except Exception as e:
                _pack_debug(f"WARNING: cancel_pack_linked_jobs failed: {e}")
        
        # Restore original library_abspath function if we overrode it
        if hasattr(self, '_original_library_abspath'):
            try:
                au.library_abspath.cache_clear()
                au.library_abspath = self._original_library_abspath
                _pack_debug(f"Restored original library_abspath in cleanup")
            except Exception as e:
                _pack_debug(f"WARNING: Could not restore library_abspath: {e}")
        
        # Remove timer
        if hasattr(self, '_timer') and self._timer:
            context.window_manager.event_timer_remove(self._timer)
        
        # Clear OS taskbar (ITaskbarList3)
        wm_progress.end()
        
        # Reset progress properties
        pack_settings.is_packing = False
        pack_settings.cancel_requested = False
        pack_settings.pack_progress = 0.0
        if cancelled and self._error:
            pack_settings.pack_status_message = self._error
        else:
            pack_settings.pack_status_message = ""
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)


class BBP_OT_pack_blend(Operator):
    """Pack project and save (pack all assets into blend files) - saves blend to output location."""
    bl_idname = "bbp.pack_blend"
    bl_label = "Pack as Blend"
    bl_description = "Pack all assets into blend files and save to output location"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        """Start the packing operation."""
        pack_settings = context.scene.bbp_pack
        
        # Check if already packing
        if pack_settings.is_packing:
            self.report({'WARNING'}, "A packing operation is already in progress.")
            return {'CANCELLED'}
        
        # Get output path from settings or preferences
        output_dir = pack_settings.output_path
        if not output_dir:
            from ..utils.compat import get_addon_prefs
            prefs = get_addon_prefs()
            if prefs and prefs.default_output_path:
                output_dir = prefs.default_output_path
                pack_settings.output_path = output_dir
        
        if not output_dir:
            self.report({'ERROR'}, "Please specify an output path in the panel below.")
            return {'CANCELLED'}
        
        # Generate filename
        blend_name = bpy.data.filepath if bpy.data.filepath else "untitled"
        if blend_name:
            blend_name = Path(blend_name).stem
        else:
            blend_name = "untitled"
        output_file = Path(output_dir) / f"{blend_name}.blend"
        
        # Initialize progress properties
        pack_settings.is_packing = True
        pack_settings.cancel_requested = False
        pack_settings.pack_progress = 0.0
        pack_settings.pack_status_message = "Initializing..."
        
        # Initialize phase tracking
        self._phase = 'INIT'
        self._output_path = output_file
        self._original_filepath = bpy.data.filepath
        self._temp_blend_path = None
        self._temp_dir = None
        self._target_path = None
        self._blend_path = None
        self._frame_start = None
        self._frame_end = None
        self._frame_step = None
        self._success = False
        self._message = ""
        self._error = None
        self._packer = None  # IncrementalPacker instance
        self._pack_t0 = time.perf_counter()  # End-to-end wall clock (invoke → finish/cancel)
        # OS taskbar via ITaskbarList3 (Atomic wm_progress pattern)
        wm_progress.begin()
        wm_progress.set_progress(0.0)
        
        # Create timer for modal updates
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
        
        # Start modal operation
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)
    
    def modal(self, context, event):
        """Handle modal events and update progress."""
        pack_settings = context.scene.bbp_pack
        
        # Handle Esc / Cancel button
        if event.type == 'ESC' or pack_settings.cancel_requested:
            self._cleanup(context, cancelled=True)
            dur = getattr(self, "_pack_duration", "") or ""
            self.report({'INFO'}, f"Packing cancelled ({dur})." if dur else "Packing cancelled.")
            return {'CANCELLED'}
        
        # Handle timer events
        if event.type == 'TIMER':
            try:
                wm_progress.set_progress(pack_settings.pack_progress)
                if self._phase == 'INIT':
                    pack_settings.pack_progress = 0.0
                    pack_settings.pack_status_message = "Initializing..."
                    self._phase = 'SAVING_BLEND'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_BLEND':
                    pack_settings.pack_progress = 5.0
                    pack_settings.pack_status_message = "Saving current blend state..."
                    
                    from .export_ops import save_current_blend_with_frame_range, apply_frame_range_to_blend
                    
                    try:
                        self._temp_blend_path, self._frame_start, self._frame_end, self._frame_step = save_current_blend_with_frame_range(pack_settings)
                        self._temp_dir = self._temp_blend_path.parent
                        print(f"[BBP Pack] Saved to temp file: {self._temp_blend_path}")
                    except Exception as e:
                        self._error = f"Failed to save current blend state: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    self._phase = 'APPLYING_FRAME_RANGE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE':
                    pack_settings.pack_progress = 10.0
                    pack_settings.pack_status_message = "Frame range applied."
                    # Frame range is already applied in save_current_blend_with_frame_range
                    self._phase = 'OVERRIDING_FILEPATH'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'OVERRIDING_FILEPATH':
                    pack_settings.pack_progress = 12.0
                    pack_settings.pack_status_message = "Preparing for packing..."
                    
                    # Temporarily override library_abspath to use temp file instead of opening it. This avoids invalidating the operator instance
                    import functools

                    self._original_library_abspath = au.library_abspath
                    temp_file_path = self._temp_blend_path.resolve()

                    def override_library_abspath(lib):
                        if lib is None:
                            return temp_file_path
                        return self._original_library_abspath(lib)

                    au.library_abspath.cache_clear()
                    au.library_abspath = override_library_abspath
                    # Re-apply lru_cache decorator behavior by wrapping
                    au.library_abspath = functools.lru_cache(maxsize=None)(override_library_abspath)
                    
                    _pack_debug(f"Overrode library_abspath to use temp file: {temp_file_path}")
                    
                    # Initialize IncrementalPacker
                    def progress_callback(progress_pct, message):
                        """Update progress during packing."""
                        # Map packer progress (0-100%) to operator progress (15-70%)
                        pack_settings.pack_progress = 15.0 + (progress_pct * 0.55)
                        pack_settings.pack_status_message = message
                        wm_progress.set_progress(pack_settings.pack_progress)
                        _pack_debug(f"Progress update: {pack_settings.pack_progress:.1f}% - {message}")
                        # Force UI redraw on every update
                        for area in context.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
                    
                    def cancel_check():
                        """True when Cancel / Esc requested."""
                        return bool(pack_settings.cancel_requested)
                    
                    max_size_bytes = _get_project_size_limit_bytes(context)
                    exclude_av = bool(getattr(pack_settings, 'exclude_av', False))
                    self._packer = IncrementalPacker(
                        WorkflowMode.PACK_AND_SAVE,
                        target_path=None,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                        frame_start=self._frame_start,
                        frame_end=self._frame_end,
                        frame_step=self._frame_step,
                        temp_blend_path=self._temp_blend_path,
                        original_blend_path=Path(self._original_filepath) if self._original_filepath else None,
                        max_size_bytes=max_size_bytes,
                        exclude_av=exclude_av,
                    )
                    
                    self._phase = 'PACKING_INIT'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'PACKING_INIT' or self._phase.startswith('PACKING_'):
                    # Handle all packing sub-phases using IncrementalPacker
                    try:
                        # Process one batch
                        # Unbounded tick + hang-threshold yield (Atomic scanner pattern).
                        next_phase, is_complete = self._packer.process_batch()
                        
                        if is_complete:
                            # Packing is complete
                            self._target_path = self._packer.target_path
                            self._blend_path = self._packer.file_path
                            _pack_debug(f"Incremental packing completed")
                            print(f"[BBP Pack] Packed to: {self._target_path}")
                            context.scene.bbp_pack.pack_output_path = str(self._target_path)
                            
                            # Check for oversized files that couldn't be packed
                            if self._packer.oversized_files_all:
                                oversized_list = "\n".join(f"  - {f.name if f.name else f} ({f.stat().st_size / (1024**3):.2f} GB)" 
                                                          for f in self._packer.oversized_files_all[:10])
                                if len(self._packer.oversized_files_all) > 10:
                                    oversized_list += f"\n  ... and {len(self._packer.oversized_files_all) - 10} more"
                                warning_msg = (
                                    f"Warning: {len(self._packer.oversized_files_all)} linked file(s) over size limit could not be packed:\n"
                                    f"{oversized_list}\n\n"
                                    "Blender cannot pack linked files over the project size limit. These files will remain as external references.\n"
                                    "To fix: Reduce the size of these files or split them into smaller files."
                                )
                                print(f"[BBP Pack] {warning_msg}")
                                # Report as warning (non-blocking)
                                self.report({'WARNING'}, f"{len(self._packer.oversized_files_all)} linked file(s) over size limit could not be packed")
                            
                            if not self._blend_path or not self._blend_path.exists():
                                self._error = "Could not find hero blend for submission."
                                self._cleanup(context, cancelled=True)
                                self.report({'ERROR'}, self._error)
                                return {'CANCELLED'}
                            
                            self._phase = 'APPLYING_FRAME_RANGE_TO_HERO'
                            _pack_debug(f"Transitioning to APPLYING_FRAME_RANGE_TO_HERO phase")
                        else:
                            # Continue with next phase from packer (prepend PACKING_ prefix)
                            self._phase = f'PACKING_{next_phase}'
                            _pack_debug(f"Packing phase: {self._phase}, continuing...")
                        
                        return {'RUNNING_MODAL'}
                    except InterruptedError as e:
                        _pack_debug(f"Packing cancelled by user")
                        self._cleanup(context, cancelled=True)
                        dur = getattr(self, "_pack_duration", "") or ""
                        self.report({'INFO'}, f"Packing cancelled ({dur})." if dur else "Packing cancelled.")
                        return {'CANCELLED'}
                    except Exception as e:
                        _pack_debug(f"ERROR in PACKING: {type(e).__name__}: {str(e)}")
                        import traceback
                        traceback.print_exc()
                        self._error = str(e) if isinstance(e, DeadUncAssetError) else f"Packing failed: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error.split('\n')[0])
                        return {'CANCELLED'}
                
                elif self._phase == 'APPLYING_FRAME_RANGE_TO_HERO':
                    pack_settings.pack_progress = 70.0
                    pack_settings.pack_status_message = "Applying frame range to hero blend..."
                    
                    from .export_ops import apply_frame_range_to_blend
                    
                    # Apply frame range to the hero blend before submission
                    print(f"[BBP Pack] Applying frame range to hero blend: {self._blend_path.name}")
                    apply_frame_range_to_blend(self._blend_path, self._frame_start, self._frame_end, self._frame_step)
                    # Frame-range save can resurrect ghost Library stubs; re-seal pack_libraries/texts.
                    # Skip seal when pack_linked already failed — another timeout won't fix a hollow/stuck hero.
                    pack_linked_fail = getattr(self._packer, "pack_linked_failure_summary", "") if self._packer else ""
                    if pack_linked_fail:
                        print(f"[BBP Pack] Skipping post-frame-range seal — pack_linked already failed")
                    else:
                        print(f"[BBP Pack] Sealing packed blend after frame range: {self._blend_path.name}")
                        seal_missing, _seal_over = pack_linked_in_blend(
                            self._blend_path,
                            max_size_bytes=_get_project_size_limit_bytes(context),
                            pack_root=self._target_path or self._blend_path.parent,
                            search_roots=getattr(self._packer, "search_roots", None) or [],
                        )
                        if seal_missing and self._packer:
                            self._packer.missing_files_all.extend(seal_missing)
                            # Seal timeout/failure must surface as Pack Linked failure too.
                            if any(str(p).startswith("pack_linked_timeout:") for p in seal_missing):
                                self._packer.pack_linked_failed_blends.append(self._blend_path)
                                self._packer.pack_linked_failure_summary = _format_pack_linked_failure_report(
                                    self._packer.pack_linked_failed_blends
                                )
                    
                    self._phase = 'RESTORING_LIBRARY_ABSPATH'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'RESTORING_LIBRARY_ABSPATH':
                    pack_settings.pack_progress = 72.0
                    pack_settings.pack_status_message = "Restoring file paths..."
                    
                    # Restore original library_abspath function
                    if hasattr(self, '_original_library_abspath'):
                        au.library_abspath.cache_clear()
                        au.library_abspath = self._original_library_abspath
                        _pack_debug(f"Restored original library_abspath function")
                    
                    self._phase = 'VALIDATING_FILE_SIZE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'VALIDATING_FILE_SIZE':
                    pack_settings.pack_progress = 72.5
                    pack_settings.pack_status_message = "Validating file size..."
                    
                    # Check blend file size
                    if self._blend_path and self._blend_path.exists():
                        blend_size = self._blend_path.stat().st_size
                        blend_size_gb = blend_size / (1024 * 1024 * 1024)
                        max_bytes = _get_project_size_limit_bytes(context)
                        if max_bytes is not None and blend_size > max_bytes:
                            limit_gb = max_bytes / (1024 * 1024 * 1024)
                            print(f"[BBP Pack] Blend file size: {blend_size_gb:.2f} GB")
                            error_msg = (
                                f"Blend file size ({blend_size_gb:.2f} GB) exceeds project limit ({limit_gb:.1f} GB).\n\n"
                                "To reduce file size, consider:\n"
                                "- Optimizing the scene (reduce geometry, simplify materials)\n"
                                "- Optimizing asset files (compress textures, reduce resolution)\n"
                                "- Splitting the frame range (render in smaller chunks)"
                            )
                            print(f"[BBP Pack] ERROR: {error_msg}")
                            self._error = error_msg
                            self._cleanup(context, cancelled=True)
                            self.report({'ERROR'}, self._error)
                            return {'CANCELLED'}
                    
                    self._phase = 'SAVING_FILE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'SAVING_FILE':
                    pack_settings.pack_progress = 75.0
                    pack_settings.pack_status_message = "Saving blend file to output location..."
                    
                    try:
                        # Name conflict: same random suffix as the tmp pack dir (bbp_pack_<token>).
                        self._output_path = _unique_pack_output_path(
                            self._output_path.parent,
                            self._output_path.stem,
                            self._output_path.suffix,
                            self._target_path,
                        )
                        self._output_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(self._blend_path, self._output_path)
                        
                        print(f"[BBP Pack] Saved blend file to: {self._output_path}")
                        self._success = True
                        self._message = f"Blend file saved to: {self._output_path}"
                        
                        pack_settings.pack_progress = 90.0
                        pack_settings.pack_status_message = "File saved successfully!"
                        self._phase = 'CLEANUP'
                    except Exception as e:
                        self._error = f"Failed to save file: {str(e)}"
                        self._cleanup(context, cancelled=True)
                        self.report({'ERROR'}, self._error)
                        return {'CANCELLED'}
                    
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'CLEANUP':
                    pack_settings.pack_progress = 98.0
                    pack_settings.pack_status_message = "Cleaning up..."
                    
                    # Clean up temp file on success
                    if self._temp_blend_path and self._temp_blend_path.exists():
                        try:
                            self._temp_blend_path.unlink()
                            if self._temp_dir and self._temp_dir.exists():
                                try:
                                    self._temp_dir.rmdir()
                                except Exception:
                                    pass  # Directory may not be empty
                            print(f"[BBP Pack] Cleaned up temp file: {self._temp_blend_path}")
                        except Exception as e:
                            print(f"[BBP Pack] WARNING: Could not clean up temp file: {e}")
                    
                    self._phase = 'COMPLETE'
                    return {'RUNNING_MODAL'}
                
                elif self._phase == 'COMPLETE':
                    pack_settings.pack_progress = 100.0
                    wm_progress.set_progress(100)
                    missing_summary = getattr(self._packer, "missing_summary", "") if self._packer else ""
                    pack_linked_fail = getattr(self._packer, "pack_linked_failure_summary", "") if self._packer else ""
                    dur = _report_pack_duration(getattr(self, "_pack_t0", None))
                    if pack_linked_fail:
                        done_msg = f"Pack finished with Pack Linked failures in {dur}" if dur else "Pack finished with Pack Linked failures"
                    else:
                        done_msg = f"Packing complete in {dur}!" if dur else "Packing complete!"
                    status_extra = pack_linked_fail or missing_summary
                    pack_settings.pack_status_message = (
                        f"{done_msg.rstrip('!')} — {status_extra}" if status_extra else done_msg
                    )
                    
                    # Small delay to show completion
                    time.sleep(0.2)
                    
                    self._cleanup(context, cancelled=False)
                    saved_msg = self._message if self._message else f"File saved to: {self._output_path}"
                    if dur:
                        saved_msg = f"{saved_msg} ({dur})"
                    self.report({'INFO'}, saved_msg)
                    if pack_linked_fail:
                        self.report({'ERROR'}, pack_linked_fail)
                    if missing_summary:
                        self.report({'WARNING'}, missing_summary)
                    return {'FINISHED'}
                
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._error = str(e) if isinstance(e, DeadUncAssetError) else f"Packing failed: {type(e).__name__}: {str(e)}"
                self._cleanup(context, cancelled=True)
                self.report({'ERROR'}, self._error.split('\n')[0])
                return {'CANCELLED'}
        
        # Let UI clicks reach Cancel / other panels while the timer drives packing.
        return {'PASS_THROUGH'}
    
    def _cleanup(self, context, cancelled=False):
        """Clean up progress properties and timer."""
        pack_settings = context.scene.bbp_pack
        # One cancel/error timing report per run (clear t0 so cleanup can't double-print).
        if cancelled and getattr(self, "_pack_t0", None) is not None:
            self._pack_duration = _report_pack_duration(self._pack_t0, cancelled=True)
            self._pack_t0 = None

        # Kill parallel pack_linked Blender children if still running.
        packer = getattr(self, "_packer", None)
        if packer is not None and hasattr(packer, "cancel_pack_linked_jobs"):
            try:
                packer.cancel_pack_linked_jobs()
            except Exception as e:
                _pack_debug(f"WARNING: cancel_pack_linked_jobs failed: {e}")
        
        # Restore original library_abspath function if we overrode it
        if hasattr(self, '_original_library_abspath'):
            try:
                au.library_abspath.cache_clear()
                au.library_abspath = self._original_library_abspath
                _pack_debug(f"Restored original library_abspath in cleanup")
            except Exception as e:
                _pack_debug(f"WARNING: Could not restore library_abspath: {e}")
        
        # Remove timer
        if hasattr(self, '_timer') and self._timer:
            context.window_manager.event_timer_remove(self._timer)
        
        # Clear OS taskbar (ITaskbarList3)
        wm_progress.end()
        
        # Reset progress properties
        pack_settings.is_packing = False
        pack_settings.cancel_requested = False
        pack_settings.pack_progress = 0.0
        if cancelled and self._error:
            pack_settings.pack_status_message = self._error
        else:
            pack_settings.pack_status_message = ""
        
        # Force UI redraw
        for area in context.screen.areas:
            if area.type == 'PROPERTIES':
                area.tag_redraw()
    
    def execute(self, context):
        """Legacy execute method - redirects to invoke for modal operation."""
        return self.invoke(context, None)


class BBP_OT_enable_nla(Operator):
    """Enable NLA only on objects/rigs that have Animation Layers turned on (Animation Layers addon)."""
    bl_idname = "bbp.enable_nla"
    bl_label = "Enable NLA"
    bl_description = "Disable animation layers, remove current action, and enable NLA on objects that have Animation Layers on"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        """Execute the NLA enable operation only on objects with Animation Layers on."""
        objects_processed = 0
        animation_layers_disabled = 0
        actions_removed = 0
        nla_enabled = 0
        
        for obj in bpy.data.objects:
            ad = getattr(obj, 'animation_data', None)
            if not ad:
                continue
            anim_layers = getattr(obj, 'AnimLayersSettings', None) or getattr(obj, 'als', None)
            turn_on = getattr(anim_layers, 'turn_on', None) if anim_layers else None
            if anim_layers is None or turn_on is not True:
                continue
            
            try:
                anim_layers.turn_on = False
                animation_layers_disabled += 1
                if ad.action is not None:
                    ad.action = None
                    actions_removed += 1
                if hasattr(ad, 'use_nla') and not ad.use_nla:
                    ad.use_nla = True
                    nla_enabled += 1
                objects_processed += 1
            except Exception as e:
                print(f"[BBP NLA] Warning: Could not process object '{obj.name}': {e}")
        
        if objects_processed > 0:
            self.report({'INFO'},
                f"Processed {objects_processed} objects (Animation Layers on): "
                f"{animation_layers_disabled} disabled, {actions_removed} actions removed, {nla_enabled} NLA enabled")
        else:
            self.report({'INFO'}, "No objects with Animation Layers on found.")
        
        return {'FINISHED'}


class BBP_OT_pack_zip_sync(Operator):
    """Pack project as ZIP synchronously (for scripting/MCP). Same as Pack as ZIP but runs to completion in one call."""
    bl_idname = "bbp.pack_zip_sync"
    bl_label = "Pack as ZIP (Sync)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from .export_ops import (
            save_current_blend_with_frame_range,
            apply_frame_range_to_blend,
            create_zip_from_directory,
        )
        try:
            from ..utils.compat import get_addon_prefs
        except Exception:
            get_addon_prefs = lambda: None
        pack_settings = context.scene.bbp_pack
        output_dir = pack_settings.output_path
        if not output_dir:
            prefs = get_addon_prefs()
            if prefs and prefs.default_output_path:
                output_dir = prefs.default_output_path
        if not output_dir:
            self.report({'ERROR'}, "Please specify an output path.")
            return {'CANCELLED'}
        pack_settings.is_packing = True
        output_dir = Path(output_dir)
        original_filepath = bpy.data.filepath
        blend_name = Path(original_filepath).stem if original_filepath else "untitled"
        try:
            temp_blend_path, frame_start, frame_end, frame_step = save_current_blend_with_frame_range(pack_settings)
        except Exception as e:
            pack_settings.is_packing = False
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        import functools
        _orig_lib_abspath = au.library_abspath
        temp_file_path = temp_blend_path.resolve()
        def _override(lib):
            return temp_file_path if lib is None else _orig_lib_abspath(lib)
        au.library_abspath.cache_clear()
        au.library_abspath = functools.lru_cache(maxsize=None)(_override)
        def _progress(pct, msg):
            pack_settings.pack_progress = 15.0 + (pct * 0.46)
            pack_settings.pack_status_message = msg
        max_size_bytes = _get_project_size_limit_bytes(context)
        exclude_av = bool(getattr(pack_settings, 'exclude_av', False))
        packer = IncrementalPacker(
            WorkflowMode.COPY_ONLY,
            target_path=None,
            progress_callback=_progress,
            cancel_check=lambda: False,
            frame_start=frame_start,
            frame_end=frame_end,
            frame_step=frame_step,
            temp_blend_path=temp_blend_path,
            original_blend_path=Path(original_filepath) if original_filepath else None,
            max_size_bytes=max_size_bytes,
            exclude_av=exclude_av,
        )
        try:
            while True:
                next_phase, is_complete = packer.process_batch()
                if is_complete:
                    break
            target_path = packer.target_path
        except Exception as e:
            au.library_abspath.cache_clear()
            au.library_abspath = _orig_lib_abspath
            pack_settings.is_packing = False
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        # Apply frame range only to the hero blend, not dependent blends
        hero_blend = packer.hero_blend if packer else None
        if hero_blend and hero_blend.exists():
            apply_frame_range_to_blend(hero_blend, frame_start, frame_end, frame_step)
        au.library_abspath.cache_clear()
        au.library_abspath = _orig_lib_abspath
        zip_path = target_path.parent / f"{target_path.name}.zip"
        exclude_av = bool(getattr(pack_settings, 'exclude_av', False))
        create_zip_from_directory(target_path, zip_path, cancel_check=lambda: False, exclude_av=exclude_av)
        final_zip_path = _unique_pack_output_path(output_dir, blend_name, ".zip", target_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(zip_path), str(final_zip_path))
        if temp_blend_path.exists():
            try:
                temp_blend_path.unlink()
            except Exception:
                pass
        pack_settings.is_packing = False
        pack_settings.pack_progress = 100.0
        pack_settings.pack_status_message = ""
        self.report({'INFO'}, f"ZIP saved to: {final_zip_path}")
        return {'FINISHED'}



class BBP_OT_cancel_pack(Operator):
    """Request cancel of the packing modal (Cancel button / Esc)."""
    bl_idname = "bbp.cancel_pack"
    bl_label = "Cancel Pack"
    bl_description = "Cancel the packing operation in progress"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pack = getattr(context.scene, "bbp_pack", None)
        return bool(pack and pack.is_packing)

    def execute(self, context):
        context.scene.bbp_pack.cancel_requested = True
        self.report({'INFO'}, "Cancelling pack…")
        return {'FINISHED'}


def register():
    """Register operators."""
    from ..utils import compat
    compat.safe_register_class(BBP_OT_pack_zip)
    compat.safe_register_class(BBP_OT_pack_zip_sync)
    compat.safe_register_class(BBP_OT_pack_blend)
    compat.safe_register_class(BBP_OT_cancel_pack)
    compat.safe_register_class(BBP_OT_enable_nla)


def unregister():
    """Unregister operators."""
    from ..utils import compat
    compat.safe_unregister_class(BBP_OT_enable_nla)
    compat.safe_unregister_class(BBP_OT_cancel_pack)
    compat.safe_unregister_class(BBP_OT_pack_blend)
    compat.safe_unregister_class(BBP_OT_pack_zip_sync)
    compat.safe_unregister_class(BBP_OT_pack_zip)
