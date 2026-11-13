#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Read-only access to the EverLive shared-memory frame store.

The capture engines publish YUY2 frames into a POSIX shared-memory object.
Version 3 of the store (``EVLS``) is used by current ``dev``; the legacy
``GC57`` layout is still accepted for one release so an older installed engine
can be inspected without restarting it.

The reader never writes to or unlinks the shared object.  Frames are exposed
as memoryviews into the mapping; callers that convert or copy a frame must
call :meth:`FrameStore.validate` afterwards to ensure the producer did not
recycle the slot while it was being read.
"""

from __future__ import annotations

import mmap
import os
import struct
import time
from dataclasses import dataclass
from typing import Optional


EVLS_MAGIC = 0x45564C53  # "EVLS"
EVLS_VERSION = 3
EVLS_HEADER_SIZE = 192
EVLS_PORT_OFFSET = 72
EVLS_PORT_SIZE = 56
EVLS_MAX_PORTS = 2
EVLS_MAX_SLOTS = 8
EVLS_ALIGN = 64

LEGACY_MAGIC = 0x47433537  # "GC57"
LEGACY_HEADER_SIZE = 40
LEGACY_PORT_HEADER_SIZE = 48
LEGACY_FRAME_BYTES = 1920 * 1080 * 2
LEGACY_SLOTS = 3
LEGACY_PORT_SIZE = LEGACY_PORT_HEADER_SIZE + LEGACY_SLOTS * LEGACY_FRAME_BYTES
LEGACY_GC573_PORT_HEADER_SIZE = 40


class FrameStoreError(RuntimeError):
    """Raised when a shared-memory object is missing or malformed."""


@dataclass(frozen=True)
class Signal:
    """Metadata for one published port, without a frame view."""

    path: str
    port: int
    version: int
    legacy: bool
    seq: int
    timestamp_ns: int
    width: int
    height: int
    no_signal: bool
    streaming: bool
    frame_bytes: int
    slot: int
    fps: Optional[float] = None

    @property
    def locked(self) -> bool:
        return bool(self.streaming and not self.no_signal and self.width > 0 and self.height > 0)


@dataclass(frozen=True)
class Frame:
    """A frame view into the mapping.

    ``data`` is only valid while the :class:`FrameStore` is open.  Call
    :meth:`FrameStore.validate` after converting/copying it.
    """

    path: str
    port: int
    version: int
    legacy: bool
    seq: int
    timestamp_ns: int
    width: int
    height: int
    no_signal: bool
    streaming: bool
    frame_bytes: int
    slot: int
    data: memoryview
    fps: Optional[float] = None

    def copy(self) -> bytes:
        return bytes(self.data)


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _frame_bytes_ok(width: int, height: int, frame_bytes: int, stride: int) -> bool:
    if width <= 0 or height <= 0 or frame_bytes <= 0:
        return False
    if frame_bytes > stride:
        return False
    return width * height * 2 == frame_bytes


class FrameStore:
    """Read-only attachment to one EverLive frame store."""

    def __init__(self, path: str):
        self.path = path
        self._fd = -1
        self._map: Optional[mmap.mmap] = None
        self._size = 0
        self._dev = 0
        self._ino = 0
        self.version = 0
        self.legacy = False
        self.port_count = 0
        self.slot_count = 0
        self.frame_stride = 0
        self._payload_offset = 0
        self._legacy_gc573 = False

        try:
            self._fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            stat = os.fstat(self._fd)
            self._size = stat.st_size
            self._dev = stat.st_dev
            self._ino = stat.st_ino
            if self._size < 8:
                raise FrameStoreError(f"{path}: shared-memory object is empty")
            self._map = mmap.mmap(self._fd, 0, prot=mmap.PROT_READ)
            magic = struct.unpack_from("<I", self._map, 0)[0]
            if magic == EVLS_MAGIC:
                self._init_evls()
            elif magic == LEGACY_MAGIC:
                self._init_legacy()
            else:
                raise FrameStoreError(
                    f"{path}: unknown frame-store magic 0x{magic:08x}")
        except Exception:
            self.close()
            raise

    # -- construction helpers ------------------------------------------------

    def _init_evls(self) -> None:
        if self._size < EVLS_HEADER_SIZE:
            raise FrameStoreError(f"{self.path}: truncated EVLS header")
        (_magic, version, port_count, slot_count,
         frame_stride, _reserved0) = struct.unpack_from("<IIIIII", self._map, 0)
        if version != EVLS_VERSION:
            raise FrameStoreError(
                f"{self.path}: unsupported EVLS version {version}")
        if not 1 <= port_count <= EVLS_MAX_PORTS:
            raise FrameStoreError(
                f"{self.path}: invalid port count {port_count}")
        if not 2 <= slot_count <= EVLS_MAX_SLOTS:
            raise FrameStoreError(
                f"{self.path}: invalid slot count {slot_count}")
        if frame_stride <= 0 or frame_stride % EVLS_ALIGN:
            raise FrameStoreError(
                f"{self.path}: invalid frame stride {frame_stride}")
        # The C header reserves two port descriptors even when only one is
        # advertised, so the payload always starts at the 192-byte boundary.
        payload_offset = _align_up(EVLS_HEADER_SIZE, EVLS_ALIGN)
        region = payload_offset + port_count * slot_count * frame_stride
        if self._size < region:
            raise FrameStoreError(
                f"{self.path}: frame store is truncated "
                f"({self._size} < {region})")
        self.version = version
        self.port_count = port_count
        self.slot_count = slot_count
        self.frame_stride = frame_stride
        self._payload_offset = payload_offset
        self.legacy = False

    def _init_legacy(self) -> None:
        if self._size < LEGACY_HEADER_SIZE:
            raise FrameStoreError(f"{self.path}: truncated legacy header")
        # The two engines used different legacy layouts.  The shm name is the
        # only reliable discriminator because both objects start with "GC57".
        self._legacy_gc573 = "gc573" in os.path.basename(self.path).lower()
        self.version = 0
        self.legacy = True
        self.port_count = 1 if self._legacy_gc573 else 2
        self.slot_count = 1 if self._legacy_gc573 else LEGACY_SLOTS
        if self._legacy_gc573:
            # The old GC573 writer used a single slot sized for the active
            # capture mode, so derive the available stride from the object.
            self.frame_stride = max(
                0, self._size - LEGACY_HEADER_SIZE -
                LEGACY_GC573_PORT_HEADER_SIZE)
            self._payload_offset = LEGACY_HEADER_SIZE
        else:
            self.frame_stride = LEGACY_FRAME_BYTES
            self._payload_offset = LEGACY_HEADER_SIZE

    # -- lifecycle -----------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._map is None

    def close(self) -> None:
        if self._map is not None:
            try:
                self._map.close()
            except BufferError:
                # A caller still holds a Frame view.  Leave the mapping alive
                # rather than invalidating it; the view will be released when
                # the last reference goes away.
                return
            except Exception:
                pass
            self._map = None
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def __enter__(self) -> "FrameStore":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def is_stale(self) -> bool:
        """True when the path now points at a different object."""
        try:
            stat = os.stat(self.path)
        except OSError:
            return True
        return stat.st_dev != self._dev or stat.st_ino != self._ino

    def heartbeat_age_ns(self) -> Optional[int]:
        """Age of the publisher heartbeat, or ``None`` for legacy stores."""
        if self._map is None or self.legacy:
            return None
        try:
            heartbeat = struct.unpack_from("<Q", self._map, 64)[0]
        except struct.error:
            return None
        if heartbeat == 0:
            return None
        return max(0, time.monotonic_ns() - heartbeat)

    def is_alive(self, max_age_s: float = 2.0) -> bool:
        age = self.heartbeat_age_ns()
        if age is None:
            return not self.legacy
        return age <= int(max_age_s * 1_000_000_000)

    # -- reading -------------------------------------------------------------

    def _port_offset(self, port: int) -> int:
        return EVLS_PORT_OFFSET + port * EVLS_PORT_SIZE

    def _legacy_port_offset(self, port: int) -> int:
        if self._legacy_gc573:
            return LEGACY_HEADER_SIZE
        return LEGACY_HEADER_SIZE + port * LEGACY_PORT_SIZE

    def _read_seq(self, port: int) -> int:
        if self._map is None:
            return 0
        try:
            if self.legacy:
                return struct.unpack_from(
                    "<Q", self._map, self._legacy_port_offset(port))[0]
            return struct.unpack_from("<Q", self._map, self._port_offset(port))[0]
        except (struct.error, IndexError):
            return 0

    def signal(self, port: int = 0) -> Optional[Signal]:
        """Read metadata for @port without exposing frame bytes."""
        if self._map is None or not 0 <= port < self.port_count:
            return None
        try:
            if self.legacy:
                return self._signal_legacy(port)
            return self._signal_evls(port)
        except (struct.error, IndexError):
            return None

    def _signal_evls(self, port: int) -> Optional[Signal]:
        assert self._map is not None
        offset = self._port_offset(port)
        seq = struct.unpack_from("<Q", self._map, offset)[0]
        if seq == 0:
            return None
        timestamp_ns = struct.unpack_from("<Q", self._map, offset + 8)[0]
        (width, height, no_signal, streaming,
         frame_bytes, _write_idx, ready_idx, _read_idx,
         _reserved) = struct.unpack_from("<9I", self._map, offset + 16)
        if ready_idx >= self.slot_count:
            return None
        if not _frame_bytes_ok(width, height, frame_bytes, self.frame_stride):
            return None
        if self._read_seq(port) != seq:
            return None
        return Signal(
            path=self.path, port=port, version=self.version, legacy=False,
            seq=seq, timestamp_ns=timestamp_ns, width=width, height=height,
            no_signal=bool(no_signal), streaming=bool(streaming),
            frame_bytes=frame_bytes, slot=ready_idx)

    def _signal_legacy(self, port: int) -> Optional[Signal]:
        assert self._map is not None
        offset = self._legacy_port_offset(port)
        seq = struct.unpack_from("<Q", self._map, offset)[0]
        if seq == 0:
            return None
        width, height, no_signal, streaming = struct.unpack_from(
            "<IIII", self._map, offset + 16)
        fps = None
        slot = 0
        frame_bytes = width * height * 2
        if self._legacy_gc573:
            fps_num, fps_den = struct.unpack_from(
                "<II", self._map, offset + 32)
            if fps_den:
                fps = fps_num / fps_den
        else:
            slot = struct.unpack_from("<I", self._map, offset + 36)[0]
            if slot >= LEGACY_SLOTS:
                return None
        if not _frame_bytes_ok(width, height, frame_bytes, self.frame_stride):
            return None
        if self._read_seq(port) != seq:
            return None
        return Signal(
            path=self.path, port=port, version=self.version, legacy=True,
            seq=seq, timestamp_ns=0, width=width, height=height,
            no_signal=bool(no_signal), streaming=bool(streaming),
            frame_bytes=frame_bytes, slot=slot, fps=fps)

    def frame(self, port: int = 0) -> Optional[Frame]:
        """Return a frame view for @port, or ``None`` when no frame is ready."""
        signal = self.signal(port)
        if signal is None or self._map is None:
            return None
        if self.legacy:
            if self._legacy_gc573:
                offset = self._legacy_port_offset(port) + LEGACY_GC573_PORT_HEADER_SIZE
            else:
                offset = (self._legacy_port_offset(port) +
                          LEGACY_PORT_HEADER_SIZE +
                          signal.slot * LEGACY_FRAME_BYTES)
        else:
            offset = (self._payload_offset +
                      (port * self.slot_count + signal.slot) * self.frame_stride)
        end = offset + signal.frame_bytes
        if offset < 0 or end > self._size:
            return None
        data = memoryview(self._map)[offset:end]
        return Frame(
            path=self.path, port=port, version=signal.version,
            legacy=signal.legacy, seq=signal.seq,
            timestamp_ns=signal.timestamp_ns, width=signal.width,
            height=signal.height, no_signal=signal.no_signal,
            streaming=signal.streaming, frame_bytes=signal.frame_bytes,
            slot=signal.slot, data=data, fps=signal.fps)

    def validate(self, frame: Frame) -> bool:
        """True when @frame is still the published slot it was read from."""
        if self._map is None or frame.seq == 0:
            return False
        return self._read_seq(frame.port) == frame.seq


def open_store(path: str) -> Optional[FrameStore]:
    """Open @path, returning ``None`` when it is absent or malformed."""
    if not os.path.exists(path):
        return None
    try:
        return FrameStore(path)
    except (OSError, FrameStoreError, ValueError):
        return None


def read_signal(path: str, port: int = 0) -> Optional[Signal]:
    """Convenience helper for one-shot metadata reads."""
    store = open_store(path)
    if store is None:
        return None
    try:
        return store.signal(port)
    finally:
        store.close()
