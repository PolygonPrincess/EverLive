#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Unit tests for card capability profiles."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from card_profile import (  # noqa: E402
    CAP_DUAL_INPUT,
    CAP_FRAMEGEN,
    CAP_HDR,
    GC570D_PROFILE,
    GC573_PROFILE,
    MODEL_GC570D,
    MODEL_GC573,
    _parse_ctl_detect,
    detect_card_profile,
)


class StaticProfileTests(unittest.TestCase):
    def test_gc573_capabilities(self) -> None:
        self.assertTrue(GC573_PROFILE.supports_framegen)
        self.assertTrue(GC573_PROFILE.supports_hdr)
        self.assertFalse(GC573_PROFILE.supports_dual_input)
        self.assertEqual(GC573_PROFILE.preload_nodes, ("/dev/video63",))

    def test_gc570d_capabilities(self) -> None:
        self.assertFalse(GC570D_PROFILE.supports_framegen)
        self.assertFalse(GC570D_PROFILE.supports_hdr)
        self.assertTrue(GC570D_PROFILE.supports_dual_input)
        self.assertEqual(
            GC570D_PROFILE.preload_nodes, ("/dev/video60", "/dev/video61"))

    def test_dev_override(self) -> None:
        with mock.patch.dict(os.environ, {"EVERLIVE_CARD_PROFILE": "gc573"}):
            profile = detect_card_profile()
        self.assertEqual(profile.model, MODEL_GC573)
        with mock.patch.dict(os.environ, {"EVERLIVE_CARD_PROFILE": "gc570d"}):
            profile = detect_card_profile()
        self.assertEqual(profile.model, MODEL_GC570D)

    def test_parse_ctl_detect(self) -> None:
        text = """
=== EverLive Capture Card Discovery ===
[1] AVerMedia Live Gamer DUO (GC570D)
    Model Slug:   gc570d
    Capabilities: Video-Capture, Audio-Capture, Passthrough, 1080p60, Dual-Input, RGB-LED
"""
        profile = _parse_ctl_detect(text)
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.model, MODEL_GC570D)
        self.assertTrue(profile.supports(CAP_DUAL_INPUT))
        self.assertFalse(profile.supports(CAP_FRAMEGEN))

    def test_parse_ctl_detect_framegen(self) -> None:
        text = """
[1] AVerMedia Live Gamer 4K (GC573)
    Model Slug:   gc573
    Capabilities: Video-Capture, Audio-Capture, HDR10, Lossless Scaling (lsfg-vk), RGB-LED
"""
        profile = _parse_ctl_detect(text)
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertTrue(profile.supports(CAP_HDR))
        self.assertTrue(profile.supports(CAP_FRAMEGEN))


if __name__ == "__main__":
    unittest.main()
