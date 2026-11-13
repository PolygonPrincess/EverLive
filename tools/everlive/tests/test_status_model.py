#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Unit tests for the shared Console Hub status snapshot."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from card_profile import GC570D_PROFILE, GC573_PROFILE  # noqa: E402
from status_model import (  # noqa: E402
    AudioSnapshot,
    EngineSnapshot,
    RecordingSnapshot,
    SignalSnapshot,
    StatusSnapshot,
    merge_signal,
)


class SignalTests(unittest.TestCase):
    def test_from_info(self) -> None:
        signal = SignalSnapshot.from_info({
            "locked": True,
            "resolution": "1920x1080p",
            "framerate": "60.00 FPS",
            "chroma": "YUV 4:2:2",
            "hdr": "SDR",
            "width": 1920,
            "height": 1080,
        })
        self.assertTrue(signal.locked)
        self.assertEqual(signal.resolution_label(), "1920×1080p")
        self.assertEqual(signal.fps_label(), "60.00 FPS")

    def test_interlaced_detection(self) -> None:
        signal = SignalSnapshot.from_info({"resolution": "1920x1080i"})
        self.assertTrue(signal.interlaced)

    def test_from_status(self) -> None:
        signal = SignalSnapshot.from_status({
            "signal": {"locked": True, "width": 2560, "height": 1440,
                       "interlaced": False},
            "video": {"fps": 120.0},
        })
        self.assertTrue(signal.locked)
        self.assertEqual(signal.resolution_label(), "2560×1440p")
        self.assertEqual(signal.fps_label(), "120.00 FPS")

    def test_merge_prefers_new_metadata(self) -> None:
        base = SignalSnapshot(locked=True, width=1920, height=1080,
                              fps=60.0, hdr="SDR")
        update = SignalSnapshot(locked=True, hdr="HDR10")
        merged = merge_signal(base, update)
        self.assertEqual(merged.width, 1920)
        self.assertEqual(merged.hdr, "HDR10")
        self.assertEqual(merged.fps, 60.0)


class SnapshotTests(unittest.TestCase):
    def test_locked_subtitle(self) -> None:
        snap = StatusSnapshot(
            card=GC570D_PROFILE,
            signal=SignalSnapshot(
                locked=True, width=1920, height=1080, fps=60.0,
                hdr="SDR"),
            audio=AudioSnapshot(label="L-PCM 48kHz (Stereo)"),
        )
        self.assertEqual(snap.card_name, "GC570D · Live Gamer DUO")
        self.assertEqual(
            snap.hero_subtitle(),
            "1920×1080p • 60.00 FPS • SDR • L-PCM 48kHz (Stereo)")

    def test_no_signal_subtitle(self) -> None:
        snap = StatusSnapshot(
            card=GC573_PROFILE,
            signal=SignalSnapshot(port=None),
        )
        self.assertEqual(
            snap.hero_subtitle(),
            "No HDMI Signal • Waiting for input • No Audio")

    def test_cached_subtitle(self) -> None:
        snap = StatusSnapshot(
            card=GC573_PROFILE,
            signal=SignalSnapshot(
                locked=False, cached=True, cached_resolution="3840x2160p",
                cached_fps="60.00 FPS"),
        )
        self.assertEqual(
            snap.hero_subtitle(),
            "Last Signal: 3840×2160p • 60.00 FPS • Waiting for input • No Audio")

    def test_starting_subtitle(self) -> None:
        snap = StatusSnapshot(
            card=GC573_PROFILE,
            signal=SignalSnapshot(starting=True),
        )
        self.assertEqual(
            snap.hero_subtitle(),
            "Starting Userspace Engine • Initializing capture • No Audio")

    def test_recording_snapshot_defaults(self) -> None:
        snap = StatusSnapshot(card=GC573_PROFILE)
        self.assertFalse(snap.recording.active)
        snap.recording = RecordingSnapshot(
            active=True, elapsed=12.5, frames=750, path="/tmp/out.mp4")
        self.assertTrue(snap.recording.active)
        self.assertEqual(snap.recording.frames, 750)

    def test_engine_labels(self) -> None:
        self.assertEqual(EngineSnapshot().label(), "○ Standby")
        self.assertEqual(
            EngineSnapshot(owner="local", state="live").label(),
            "● Userspace VFIO")
        self.assertEqual(
            EngineSnapshot(owner="service", state="live").label(),
            "● Background Service")
        self.assertEqual(
            EngineSnapshot(owner="remote", state="live").label(),
            "● Remote Session")
        self.assertEqual(
            EngineSnapshot(state="error").label(), "⚠ Engine Error")


if __name__ == "__main__":
    unittest.main()
