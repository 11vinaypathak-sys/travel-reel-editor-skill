#!/usr/bin/env python3
"""Verify an export: verify_export.py FILE --report check.json [--full-decode].

Expectations: --width N --height N --fps 24 --duration 1 --frames 24 --require-audio.
Exit 0: passed; 1: failed/incomplete checks; 2: configuration/dependency error.
"""

import argparse
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess
import sys

from inventory_media import number, probe_file, write_report


def fraction(value):
    try:
        result = Fraction(str(value))
        return result if result > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    for name in ("width", "height", "frames"):
        parser.add_argument("--" + name, type=positive_int)
    parser.add_argument("--fps")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--require-audio", action="store_true")
    parser.add_argument("--full-decode", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if (args.fps is not None and fraction(args.fps) is None) or (
        args.duration is not None and (number(args.duration) is None or args.duration <= 0)
    ):
        parser.error("FPS and duration must be finite positive values")
    source = args.file.absolute()
    checks = []
    report = {"schema_version": 1, "source": source.name, "status": "unknown", "checks": checks,
              "note": "Technical checks do not assess perceptual quality or establish that audio is voice-free."}
    dependency_error = False

    def check(name, passed, actual=None, expected=None):
        checks.append({"name": name, "status": "unknown" if passed is None else ("passed" if passed else "failed"),
                       "actual": actual, "expected": expected})

    probe = shutil.which("ffprobe")
    decoder = shutil.which("ffmpeg")
    check("source_file", source.is_file())
    if not probe:
        check("probe", None)
        dependency_error = True
    elif source.is_file():
        status, data = probe_file(source, probe)
        check("probe", status == "ok")
        if data:
            streams = data.get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            check("video_stream", bool(video))
            duration = number(data.get("format", {}).get("duration"))
            check("positive_duration", duration > 0 if duration is not None else None, duration)
            fps = fraction(video.get("avg_frame_rate")) or fraction(video.get("r_frame_rate"))
            for name in ("width", "height"):
                expected = getattr(args, name)
                if expected is not None:
                    actual = video.get(name)
                    check(name, actual == expected if actual is not None else None, actual, expected)
            if args.fps is not None:
                check("fps", fps == fraction(args.fps) if fps else None, str(fps) if fps else None, str(fraction(args.fps)))
            if args.duration is not None:
                tolerance = max(0.05, float(1 / fps)) if fps else 0.05
                check("duration", abs(duration - args.duration) <= tolerance if duration is not None else None, duration, args.duration)
            if args.require_audio:
                check("audio_stream", any(s.get("codec_type") == "audio" for s in streams))
            if args.frames is not None:
                count = video.get("nb_frames")
                if not str(count).isdigit():
                    try:
                        result = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                                                 "-show_entries", "stream=nb_read_frames", "-of", "json", str(source)],
                                                capture_output=True, text=True, timeout=300)
                        counted = json.loads(result.stdout).get("streams", []) if result.returncode == 0 and not result.stderr.strip() else []
                        count = counted[0].get("nb_read_frames") if counted else None
                    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
                        count = None
                count = int(count) if str(count).isdigit() else None
                check("frames", count == args.frames if count is not None else None, count, args.frames)
    if args.full_decode:
        if not decoder:
            check("full_decode", None)
            dependency_error = True
        elif source.is_file():
            try:
                result = subprocess.run([decoder, "-nostdin", "-v", "error", "-xerror", "-i", str(source),
                                         "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"],
                                        capture_output=True, text=True, timeout=600)
                check("full_decode", result.returncode == 0 and not result.stderr.strip())
            except (OSError, subprocess.TimeoutExpired):
                check("full_decode", False)
        else:
            check("full_decode", None)
    statuses = {c["status"] for c in checks}
    report["status"] = "failed" if "failed" in statuses else ("unknown" if "unknown" in statuses else "passed")
    try:
        write_report(args.report, report, args.overwrite, source)
    except (OSError, ValueError):
        print("Report write failed; check paths and overwrite policy.", file=sys.stderr)
        return 2
    return 2 if dependency_error else (0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    sys.exit(main())
