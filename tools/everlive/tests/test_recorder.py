#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Unit tests for the manual Hub-side recorder helpers."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder  # noqa: E402


class OutputPathTests(unittest.TestCase):
    def test_timestamped_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            when = datetime(2026, 11, 13, 12, 34, 56)
            path = recorder.build_output_path(tmp, when)
            self.assertEqual(
                Path(path).name, "EverLive_2026-11-13_12-34-56.mp4")
            self.assertTrue(Path(path).parent.is_dir())

    def test_collision_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            when = datetime(2026, 11, 13, 12, 34, 56)
            first = recorder.build_output_path(tmp, when)
            Path(first).write_bytes(b"")
            second = recorder.build_output_path(tmp, when)
            self.assertEqual(
                Path(second).name, "EverLive_2026-11-13_12-34-56_1.mp4")


class CommandTests(unittest.TestCase):
    def test_libx264_command(self) -> None:
        cmd = recorder.build_ffmpeg_command(
            "/tmp/out.mp4", 1920, 1080, 60.0, "balanced", "libx264")
        joined = " ".join(cmd)
        self.assertIn("-f rawvideo", joined)
        self.assertIn("-pix_fmt yuyv422", joined)
        self.assertIn("-s 1920x1080", joined)
        self.assertIn("-c:v libx264", joined)
        self.assertIn("-crf 23", joined)
        self.assertIn("/tmp/out.mp4", joined)

    def test_quality_mapping(self) -> None:
        self.assertEqual(recorder.QUALITY_CRF["high"], 18)
        self.assertEqual(recorder.QUALITY_CRF["balanced"], 23)
        self.assertEqual(recorder.QUALITY_CRF["small"], 28)

    def test_not_recording(self) -> None:
        rec = recorder.StreamRecorder()
        self.assertFalse(rec.active)
        ok, message = rec.stop()
        self.assertFalse(ok)
        self.assertIn("not recording", message.lower())
        info = rec.snapshot()
        self.assertFalse(info.active)
        self.assertEqual(info.frames, 0)


if __name__ == "__main__":
    unittest.main()
