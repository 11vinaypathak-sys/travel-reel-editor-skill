#!/usr/bin/env python3
"""Read-only inventory: inventory_media.py INPUT_ROOT --output inventory.json.

Place the report outside INPUT_ROOT. Python 3.9+, standard library only.
ffprobe is optional; absent metadata stays null.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


EXTENSIONS = {
    "video": {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts"},
    "photo": {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"},
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"},
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def write_report(path, report, overwrite=False, source=None):
    """Create an explicit report; never follow a report symlink or replace input."""
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Report path must not contain symlinks")
    if source is not None:
        source = Path(source)
        if path.resolve() == source.resolve() or (
            path.exists() and source.exists() and os.path.samefile(path, source)
        ):
            raise ValueError("Report must differ from the source")
    payload = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    if overwrite:
        # Replacing the report avoids changing any other file hard-linked to it.
        descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(str(path), flags, 0o600), "w", encoding="utf-8") as handle:
            handle.write(payload)


def probe_file(path, executable):
    if not executable:
        return "unavailable", None
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode or result.stderr.strip():
            return "failed", None
        data = json.loads(result.stdout)
        return ("ok", data) if data.get("streams") else ("failed", None)
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return "failed", None


def metadata(data):
    streams = data.get("streams", []) if data else []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    rotation = number(video.get("tags", {}).get("rotate"))
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = number(side["rotation"])
    return {
        "duration_seconds": number(data.get("format", {}).get("duration")) if data else None,
        "width": video.get("width"), "height": video.get("height"),
        "fps": video.get("avg_frame_rate") if video.get("avg_frame_rate") != "0/0" else None,
        "rotation_degrees": rotation,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams) if data else None,
        "color": {key: video.get(key) for key in (
            "color_range", "color_space", "color_transfer", "color_primaries", "pix_fmt"
        )},
    }


def inventory(root):
    executable = shutil.which("ffprobe")
    records = []
    errors = []

    def walk_error(error):
        try:
            relative = Path(error.filename).relative_to(root).as_posix()
        except (TypeError, ValueError):
            relative = "."
        errors.append({"path": relative, "status": "unreadable_directory"})

    for folder, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            kind = next((k for k, extensions in EXTENSIONS.items() if path.suffix.lower() in extensions), None)
            if name.startswith(".") or path.is_symlink() or not kind:
                continue
            row = {"path": path.relative_to(root).as_posix(), "kind": kind,
                   "id": None, "size_bytes": None, "read_status": "ok"}
            try:
                row["size_bytes"] = path.stat().st_size
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                row["id"] = "sha256:" + digest.hexdigest()
            except OSError:
                row["read_status"] = "unreadable"
            status, data = probe_file(path, executable) if row["read_status"] == "ok" else ("not_attempted", None)
            row["probe_status"] = status
            row.update(metadata(data))
            records.append(row)
    return {"schema_version": 1, "ffprobe_available": bool(executable), "files": records,
            "errors": errors, "note": "Probe success is not a full decode or a perceptual quality assessment."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    root = args.input_root.absolute()
    try:
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Input must be a directory, not a symlink")
        if args.output.resolve() == root.resolve() or root.resolve() in args.output.resolve().parents:
            raise ValueError("Inventory report must be outside the input directory")
        report = inventory(root)
        write_report(args.output, report, args.overwrite, root)
    except (OSError, ValueError):
        print("Inventory configuration or report write failed; check paths and overwrite policy.", file=sys.stderr)
        return 2
    return 1 if report["errors"] or any(r["read_status"] != "ok" or r["probe_status"] == "failed" for r in report["files"]) else 0


if __name__ == "__main__":
    sys.exit(main())
