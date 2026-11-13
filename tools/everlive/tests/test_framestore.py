#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Unit tests for the read-only EverLive frame-store reader."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framestore import (  # noqa: E402
    EVLS_ALIGN,
    EVLS_HEADER_SIZE,
    EVLS_MAGIC,
    EVLS_PORT_OFFSET,
    EVLS_PORT_SIZE,
    EVLS_VERSION,
    LEGACY_FRAME_BYTES,
    LEGACY_HEADER_SIZE,
    LEGACY_MAGIC,
    LEGACY_PORT_HEADER_SIZE,
    LEGACY_PORT_SIZE,
    FrameStore,
    FrameStoreError,
    open_store,
)


def _write_evls(path: str, width: int = 4, height: int = 2,
                frame_stride: int = 64, seq: int = 1,
                ready_idx: int = 0, frame_bytes: int | None = None,
                streaming: int = 1, no_signal: int = 0,
                port_count: int = 2) -> None:
    frame_bytes = width * height * 2 if frame_bytes is None else frame_bytes
    slot_count = 3
    payload_offset = (EVLS_HEADER_SIZE + EVLS_ALIGN - 1) & ~(EVLS_ALIGN - 1)
    size = payload_offset + port_count * slot_count * frame_stride
    buf = bytearray(size)
    struct.pack_into("<IIIIII", buf, 0, EVLS_MAGIC, EVLS_VERSION,
                     port_count, slot_count, frame_stride, 0)
    struct.pack_into("<Q", buf, 64, 1)  # heartbeat
    struct.pack_into("<QQ9I", buf, EVLS_PORT_OFFSET,
                     seq, 123456789, width, height, no_signal, streaming,
                     frame_bytes, 1, ready_idx, 0, 0)
    if 0 <= ready_idx < slot_count:
        payload = payload_offset + (0 * slot_count + ready_idx) * frame_stride
        for i in range(frame_bytes):
            buf[payload + i] = i & 0xFF
    with open(path, "wb") as f:
        f.write(buf)


def _write_legacy_gc570d(path: str, width: int = 4, height: int = 2,
                         seq: int = 1, ready_idx: int = 0) -> None:
    frame_bytes = width * height * 2
    port_size = LEGACY_PORT_HEADER_SIZE + 3 * frame_bytes
    size = LEGACY_HEADER_SIZE + 2 * port_size
    buf = bytearray(size)
    struct.pack_into("<I", buf, 0, LEGACY_MAGIC)
    port_off = LEGACY_HEADER_SIZE
    struct.pack_into("<Q", buf, port_off, seq)
    struct.pack_into("<IIII", buf, port_off + 16, width, height, 0, 1)
    struct.pack_into("<I", buf, port_off + 36, ready_idx)
    payload = port_off + LEGACY_PORT_HEADER_SIZE + ready_idx * frame_bytes
    for i in range(frame_bytes):
        buf[payload + i] = i & 0xFF
    with open(path, "wb") as f:
        f.write(buf)


def _write_legacy_gc573(path: str, width: int = 4, height: int = 2,
                        seq: int = 1) -> None:
    frame_bytes = width * height * 2
    size = LEGACY_HEADER_SIZE + 40 + frame_bytes
    buf = bytearray(size)
    struct.pack_into("<I", buf, 0, LEGACY_MAGIC)
    port_off = LEGACY_HEADER_SIZE
    struct.pack_into("<Q", buf, port_off, seq)
    struct.pack_into("<IIIIII", buf, port_off + 16,
                     width, height, 0, 1, 60000, 1000)
    payload = port_off + 40
    for i in range(frame_bytes):
        buf[payload + i] = i & 0xFF
    with open(path, "wb") as f:
        f.write(buf)


class EvlsTests(unittest.TestCase):
    def test_reads_current_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gc570d_v4l2_shm")
            _write_evls(path)
            with FrameStore(path) as store:
                self.assertFalse(store.legacy)
                self.assertEqual(store.port_count, 2)
                self.assertEqual(store.slot_count, 3)
                signal = store.signal(0)
                self.assertIsNotNone(signal)
                assert signal is not None
                self.assertTrue(signal.locked)
                self.assertEqual((signal.width, signal.height), (4, 2))
                frame = store.frame(0)
                self.assertIsNotNone(frame)
                assert frame is not None
                self.assertEqual(frame.frame_bytes, 16)
                self.assertEqual(bytes(frame.data), bytes(range(16)))
                self.assertTrue(store.validate(frame))

    def test_single_port_uses_full_header_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gc573_v4l2_shm")
            _write_evls(path, port_count=1)
            with FrameStore(path) as store:
                self.assertEqual(store.port_count, 1)
                frame = store.frame(0)
                self.assertIsNotNone(frame)
                assert frame is not None
                self.assertEqual(bytes(frame.data), bytes(range(16)))

    def test_sequence_change_invalidates_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gc570d_v4l2_shm")
            _write_evls(path)
            with FrameStore(path) as store:
                frame = store.frame(0)
                self.assertIsNotNone(frame)
                assert frame is not None
                # Simulate the producer recycling the slot.
                with open(path, "r+b") as f:
                    f.seek(EVLS_PORT_OFFSET)
                    f.write(struct.pack("<Q", 2))
                self.assertFalse(store.validate(frame))

    def test_rejects_inconsistent_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gc570d_v4l2_shm")
            _write_evls(path, width=4, height=2, frame_bytes=8)
            with FrameStore(path) as store:
                self.assertIsNone(store.signal(0))

    def test_rejects_bad_ready_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gc570d_v4l2_shm")
            _write_evls(path, ready_idx=7)
            with FrameStore(path) as store:
                self.assertIsNone(store.signal(0))

    def test_unknown_magic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad")
            with open(path, "wb") as f:
                f.write(b"\x00" * 64)
            with self.assertRaises(FrameStoreError):
                FrameStore(path)


class LegacyTests(unittest.TestCase):
    def test_gc570d_legacy_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gc570d_v4l2_shm")
            _write_legacy_gc570d(path)
            with FrameStore(path) as store:
                self.assertTrue(store.legacy)
                self.assertEqual(store.port_count, 2)
                signal = store.signal(0)
                self.assertIsNotNone(signal)
                assert signal is not None
                self.assertEqual(signal.width, 4)
                frame = store.frame(0)
                self.assertIsNotNone(frame)
                assert frame is not None
                self.assertEqual(bytes(frame.data), bytes(range(16)))

    def test_gc573_legacy_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gc573_v4l2_shm")
            _write_legacy_gc573(path)
            with FrameStore(path) as store:
                self.assertTrue(store.legacy)
                self.assertEqual(store.port_count, 1)
                signal = store.signal(0)
                self.assertIsNotNone(signal)
                assert signal is not None
                self.assertEqual(signal.fps, 60.0)
                frame = store.frame(0)
                self.assertIsNotNone(frame)
                assert frame is not None
                self.assertEqual(bytes(frame.data), bytes(range(16)))

    def test_open_store_missing(self) -> None:
        self.assertIsNone(open_store("/tmp/does-not-exist-everlive"))


if __name__ == "__main__":
    unittest.main()
