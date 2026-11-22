"""Synthetic fixtures only; run: python3 -m unittest discover -s tests -v."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "cinematic-memory-reel" / "scripts"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def run_tool(self, script, *arguments, env=None):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, arguments)],
                              capture_output=True, text=True, env=env)

    def fixture(self, name="clip with spaces; [test].mp4", audio=True):
        path = self.root / name
        command = [FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                   "testsrc2=size=180x320:rate=24:duration=1"]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1"]
        command += ["-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac"]
        command += ["-t", "1", str(path)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe required")
    def test_good_export_full_decode(self):
        clip = self.fixture()
        report = self.root / "report.json"
        result = self.run_tool("verify_export.py", clip, "--width", 180, "--height", 320,
                               "--fps", "24/1", "--duration", 1, "--frames", 24,
                               "--require-audio", "--full-decode", "--report", report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text())
        self.assertEqual(data["status"], "passed")
        self.assertEqual(data["source"], clip.name)
        self.assertNotIn(str(self.root), report.read_text())

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe required")
    def test_wrong_dimensions(self):
        report = self.root / "report.json"
        result = self.run_tool("verify_export.py", self.fixture(), "--width", 320, "--report", report)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(report.read_text())["status"], "failed")

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe required")
    def test_frame_count_fallback(self):
        clip = self.fixture(name="counted.mkv")
        metadata = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(clip)],
                                  capture_output=True, text=True, check=True)
        video = next(s for s in json.loads(metadata.stdout)["streams"] if s["codec_type"] == "video")
        self.assertNotIn("nb_frames", video)
        report = self.root / "counted.json"
        result = self.run_tool("verify_export.py", clip, "--frames", 24, "--report", report)
        self.assertEqual(result.returncode, 0, result.stderr)
        check = next(c for c in json.loads(report.read_text())["checks"] if c["name"] == "frames")
        self.assertEqual(check["actual"], 24)

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe required")
    def test_truncated_file(self):
        clip = self.fixture()
        clip.write_bytes(clip.read_bytes()[:256])
        report = self.root / "report.json"
        result = self.run_tool("verify_export.py", clip, "--full-decode", "--report", report)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(report.read_text())["status"], "failed")

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe required")
    def test_require_audio_on_mute(self):
        report = self.root / "report.json"
        result = self.run_tool("verify_export.py", self.fixture(audio=False), "--require-audio", "--report", report)
        self.assertEqual(result.returncode, 1, result.stderr)
        checks = {c["name"]: c["status"] for c in json.loads(report.read_text())["checks"]}
        self.assertEqual(checks["audio_stream"], "failed")

    @unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg and ffprobe required")
    def test_inventory_hash_stability_symlinks_and_inputs_unchanged(self):
        source = self.root / "media"
        source.mkdir()
        clip = self.fixture()
        clip = clip.rename(source / clip.name)
        hidden = source / ".hidden"
        hidden.mkdir()
        (hidden / "hidden.mp4").write_bytes(b"hidden")
        (source / ".hidden.mp4").write_bytes(b"hidden")
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        (source / "linked.mp4").symlink_to(outside)
        (source / "linked-folder").symlink_to(self.root, target_is_directory=True)
        before = (clip.read_bytes(), clip.stat().st_mtime_ns)
        first = self.root / "first.json"
        result = self.run_tool("inventory_media.py", source, "--output", first)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = json.loads(first.read_text())["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["id"], "sha256:" + hashlib.sha256(before[0]).hexdigest())
        self.assertEqual(files[0]["probe_status"], "ok")
        self.assertEqual(files[0]["width"], 180)
        self.assertEqual(files[0]["fps"], "24/1")
        self.assertTrue(files[0]["has_audio"])
        self.assertNotIn(str(self.root), first.read_text())
        renamed = clip.rename(source / "renamed.mp4")
        duplicate = source / "duplicate.mp4"
        shutil.copyfile(renamed, duplicate)
        second = self.root / "second.json"
        result = self.run_tool("inventory_media.py", source, "--output", second)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({f["id"] for f in json.loads(second.read_text())["files"]}, {files[0]["id"]})
        self.assertEqual((renamed.read_bytes(), renamed.stat().st_mtime_ns), before)

    def test_inventory_without_probe_and_output_safety(self):
        source = self.root / "media"
        source.mkdir()
        media = source / "unprobed.mp4"
        media.write_bytes(b"unprobed content")
        report = self.root / "inventory.json"
        env = dict(os.environ, PATH="")
        result = self.run_tool("inventory_media.py", source, "--output", report, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(report.read_text())["files"][0]
        self.assertEqual(row["probe_status"], "unavailable")
        self.assertIsNone(row["has_audio"])
        self.assertIsNone(row["width"])
        original = report.read_bytes()
        result = self.run_tool("inventory_media.py", source, "--output", report, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report.read_bytes(), original)
        result = self.run_tool("inventory_media.py", source, "--output", report, "--overwrite", env=env)
        self.assertEqual(result.returncode, 0)
        link = self.root / "link.json"
        link.symlink_to(report)
        result = self.run_tool("inventory_media.py", source, "--output", link, "--overwrite", env=env)
        self.assertEqual(result.returncode, 2)
        before = media.read_bytes()
        result = self.run_tool("inventory_media.py", source, "--output", media, "--overwrite", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(media.read_bytes(), before)

    def test_verifier_missing_dependency_and_source_target_rejection(self):
        source = self.root / "clip.mp4"
        source.write_bytes(b"synthetic invalid bytes")
        report = self.root / "report.json"
        result = self.run_tool("verify_export.py", source, "--report", report, env=dict(os.environ, PATH=""))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(report.read_text())["status"], "unknown")
        before = source.read_bytes()
        result = self.run_tool("verify_export.py", source, "--report", source, "--overwrite",
                               env=dict(os.environ, PATH=""))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
