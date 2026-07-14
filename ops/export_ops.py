"""
Export operations for BasedBlendfilePacker.
"""

import os
import zipfile
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import bpy


def apply_frame_range_to_blend(blend_path: Path, frame_start: int, frame_end: int, frame_step: int) -> None:
    """
    Apply frame range settings to a blend file using subprocess.
    
    Args:
        blend_path: Path to the blend file to modify
        frame_start: Start frame value
        frame_end: End frame value
        frame_step: Frame step value
    """
    script = f"""
import bpy
for scene in bpy.data.scenes:
    scene.frame_start = {frame_start}
    scene.frame_end = {frame_end}
    scene.frame_step = {frame_step}
bpy.ops.wm.save_mainfile(compress=True)
print(f'Applied frame range {frame_start}-{frame_end} (step {frame_step}) to all scenes')
"""
    
    result = subprocess.run([
        "blender", "--factory-startup", "-b", str(blend_path), "--python-expr", script
    ], capture_output=True, text=True, check=False)
    
    if result.returncode != 0:
        print(f"[BBP Export] WARNING: Failed to apply frame range to {blend_path.name}")
        if result.stderr:
            print(f"[BBP Export]   Error: {result.stderr[:200]}")
    else:
        print(f"[BBP Export] Applied frame range {frame_start}-{frame_end} (step {frame_step}) to {blend_path.name}")


def save_current_blend_with_frame_range(pack_settings, temp_dir: Optional[Path] = None) -> Tuple[Path, int, int, int]:
    """
    Save current blend state to a temporary file and apply frame range from pack_settings.
    
    Args:
        pack_settings: Submit settings containing frame range configuration
        temp_dir: Optional temporary directory (if None, creates a new one)
    
    Returns:
        Tuple of (temp_blend_path, frame_start, frame_end, frame_step)
    """
    # Determine frame range from pack_settings
    if pack_settings.frame_range_mode == 'FULL':
        frame_start = bpy.context.scene.frame_start
        frame_end = bpy.context.scene.frame_end
        frame_step = bpy.context.scene.frame_step
    else:
        frame_start = pack_settings.frame_start
        frame_end = pack_settings.frame_end
        frame_step = pack_settings.frame_step
    
    # Create temp directory if not provided
    if temp_dir is None:
        temp_dir = Path(tempfile.mkdtemp(prefix="bbp_export_"))
    
    # Generate temp blend filename
    blend_name = bpy.data.filepath if bpy.data.filepath else "untitled"
    blend_name = Path(blend_name).stem if blend_name else "untitled"
    temp_blend = temp_dir / f"{blend_name}.blend"
    
    print(f"[BBP Export] Saving current blend state to: {temp_blend}")
    print(f"[BBP Export] Frame range: {frame_start} - {frame_end} (step: {frame_step})")
    
    # Save current blend state
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(temp_blend), copy=True, compress=True)
        print(f"[BBP Export] Saved current blend state to temp file")
    except Exception as e:
        error_msg = f"Failed to save current blend state: {type(e).__name__}: {str(e)}"
        print(f"[BBP Export] ERROR: {error_msg}")
        raise RuntimeError(error_msg) from e
    
    # Apply frame range to the saved file
    apply_frame_range_to_blend(temp_blend, frame_start, frame_end, frame_step)
    
    return temp_blend, frame_start, frame_end, frame_step


# Video and audio extensions to exclude when exclude_video=True
_MEDIA_EXTENSIONS = frozenset({
    '.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v', '.wmv', '.flv', '.ogv', '.mpg', '.mpeg', '.m2v',
    '.wav', '.mp3', '.ogg', '.flac', '.aac', '.m4a', '.wma', '.opus', '.aiff', '.aif',
})


def create_zip_from_directory(directory: Path, output_zip: Path, progress_callback=None, cancel_check=None, exclude_video: bool = False) -> None:
    """Create a ZIP file from a directory.
    
    Args:
        directory: Directory to zip
        output_zip: Output ZIP file path
        progress_callback: Optional callback(progress_pct, message) for progress updates
        cancel_check: Optional callback() -> bool to check for cancellation
        exclude_video: If True, skip common video and audio file extensions
    """
    import time
    
    print(f"[BBP Export] Starting ZIP creation...")
    print(f"[BBP Export]   Directory: {directory}")
    print(f"[BBP Export]   Output: {output_zip}")
    
    # Delete .blend1 through .blend32 backup files before zipping
    if progress_callback:
        progress_callback(0.0, "Removing backup files...")
    
    backup_files = []
    for i in range(1, 33):  # blend1 through blend32
        pattern = f"*.blend{i}"
        backup_files.extend(directory.rglob(pattern))
    
    if backup_files:
        print(f"[BBP Export]   Found {len(backup_files)} backup files (.blend1-.blend32), deleting...")
        deleted_count = 0
        for backup_file in backup_files:
            try:
                backup_file.unlink()
                deleted_count += 1
            except Exception as e:
                print(f"[BBP Export]   WARNING: Could not delete {backup_file.name}: {e}")
        print(f"[BBP Export]   Deleted {deleted_count}/{len(backup_files)} backup files")
    else:
        print(f"[BBP Export]   No backup files (.blend1-.blend32) found")
    
    if progress_callback:
        progress_callback(0.0, "Counting files...")
    
    # Collect all dirs (for empty-dir entries) and files
    dir_arcs = set()
    file_list = []
    file_count = 0
    total_size = 0
    for root, dirs, files in os.walk(directory):
        root_path = Path(root)
        for d in dirs:
            dir_arcs.add(root_path.joinpath(d).relative_to(directory))
        for file in files:
            file_path = root_path / file
            if not file_path.exists():
                continue
            if exclude_video and file_path.suffix.lower() in _MEDIA_EXTENSIONS:
                continue
            file_count += 1
            total_size += file_path.stat().st_size
            file_list.append((file_path, file_path.relative_to(directory)))
    
    print(f"[BBP Export]   Found {file_count} files, total size: {total_size / (1024*1024):.2f} MB")
    print(f"[BBP Export]   Creating ZIP (this may take a while)...")
    
    if progress_callback:
        progress_callback(1.0, f"Creating ZIP archive ({file_count} files, {total_size / (1024*1024):.1f} MB)...")
    
    start_time = time.time()
    files_added = 0
    
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_STORED) as zipf:
        for dir_arc in sorted(dir_arcs):
            arcname = str(dir_arc).replace("\\", "/") + "/"
            zipf.writestr(arcname, "")
        for file_path, arcname in file_list:
            if cancel_check and cancel_check():
                raise InterruptedError("ZIP creation cancelled by user")
            
            if not file_path.exists():
                continue
            
            try:
                zipf.write(file_path, arcname)
                files_added += 1
                
                # Progress updates - more frequent for large files
                if files_added == 1:
                    print(f"[BBP Export]   Adding files to ZIP...")
                    if progress_callback:
                        progress_callback(2.0, f"Adding files to ZIP... (1/{file_count})")
                elif files_added % 10 == 0 or (file_count > 0 and files_added % max(1, file_count // 100) == 0):
                    elapsed = time.time() - start_time
                    rate = files_added / elapsed if elapsed > 0 else 0
                    progress_pct = 2.0 + (files_added / file_count * 93.0) if file_count > 0 else 2.0
                    print(f"[BBP Export]   Progress: {files_added}/{file_count} files ({files_added*100//file_count}%), {rate:.1f} files/sec")
                    if progress_callback:
                        progress_callback(progress_pct, f"Creating ZIP... ({files_added}/{file_count} files, {rate:.1f} files/sec)")
            except Exception as e:
                print(f"[BBP Export]   WARNING: Failed to add {arcname}: {type(e).__name__}: {str(e)}")
    
    elapsed = time.time() - start_time
    print(f"[BBP Export] ZIP creation completed!")
    print(f"[BBP Export]   Files added: {files_added}/{file_count}")
    print(f"[BBP Export]   Time taken: {elapsed:.2f} seconds")
    if elapsed > 0:
        print(f"[BBP Export]   Average rate: {files_added/elapsed:.1f} files/sec")
    
    if progress_callback:
        progress_callback(100.0, "ZIP archive created")


def register():
    """Register export helpers (no operators)."""
    pass


def unregister():
    """Unregister export helpers (no operators)."""
    pass
