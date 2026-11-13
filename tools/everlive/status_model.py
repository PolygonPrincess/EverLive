#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""One snapshot of Console Hub state.

The UI used to update the header, hero line, footer, fullscreen OSD and tray
from several different code paths.  This module gives every view the same
structured snapshot so signal/audio/engine text is formatted once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SignalSnapshot:
    locked: bool = False
    width: int = 0
    height: int = 0
    interlaced: bool = False
    fps: Optional[float] = None
    chroma: str = ""
    hdr: str = ""
    cached: bool = False
    starting: bool = False
    cached_resolution: str = ""
    cached_fps: str = ""
    port: Optional[int] = None

    @classmethod
    def from_info(cls, info: Optional[Dict[str, Any]]) -> "SignalSnapshot":
        info = info or {}
        width = _as_int(info.get("width"))
        height = _as_int(info.get("height"))
        resolution = str(info.get("resolution") or "")
        if not (width and height) and "x" in resolution.lower():
            text = resolution.lower().split(" ", 1)[0]
            text = text.replace("×", "x")
            size = text.split("x", 1)[1]
            digits = ""
            for ch in size:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            try:
                width = int(text.split("x", 1)[0])
                height = int(digits)
            except ValueError:
                width = height = 0
        return cls(
            locked=bool(info.get("locked")),
            width=width,
            height=height,
            interlaced=bool(re.search(r"\d+\s*i\b", resolution.lower())),
            fps=_as_float(info.get("fps")),
            chroma=str(info.get("chroma") or ""),
            hdr=str(info.get("hdr") or ""),
            cached=bool(info.get("cached")),
            starting=bool(info.get("starting")),
            cached_resolution=str(info.get("resolution") or ""),
            cached_fps=str(info.get("framerate") or ""),
            port=info.get("port"),
        )

    @classmethod
    def from_status(cls, status: Dict[str, Any], port: Optional[int] = None) -> "SignalSnapshot":
        signal = status.get("signal") or {}
        video = status.get("video") or {}
        resolution = ""
        if signal.get("width") and signal.get("height"):
            scan = "i" if signal.get("interlaced") else "p"
            resolution = f"{signal['width']}x{signal['height']}{scan}"
        return cls(
            locked=bool(signal.get("locked")),
            width=_as_int(signal.get("width")),
            height=_as_int(signal.get("height")),
            interlaced=bool(signal.get("interlaced")),
            fps=_as_float(video.get("fps") or signal.get("rate_hz")),
            chroma="",
            hdr="",
            cached=False,
            starting=not bool(signal.get("locked")),
            cached_resolution=resolution,
            cached_fps=(f"{_as_float(video.get('fps')):.2f} FPS"
                        if video.get("fps") else ""),
            port=port,
        )

    def resolution_label(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}{'i' if self.interlaced else 'p'}"
        if self.cached_resolution:
            text = self.cached_resolution
            if "x" in text:
                text = text.replace("x", "×", 1)
            return text
        return ""

    def fps_label(self) -> str:
        if self.fps:
            return f"{self.fps:.2f} FPS"
        if self.cached_fps:
            return self.cached_fps
        return ""

    def audio_label(self, fallback: str = "No Audio") -> str:
        return fallback or "No Audio"


@dataclass(frozen=True)
class EngineSnapshot:
    owner: str = "none"          # none | local | service | remote
    state: str = "off"           # off | starting | live | error
    mode: Optional[str] = None   # gaming | studio | detached
    pid: Optional[int] = None

    def label(self) -> str:
        if self.state == "live":
            if self.owner == "service":
                return "● Background Service"
            if self.owner == "remote":
                return "● Remote Session"
            return "● Userspace VFIO"
        if self.state == "starting":
            return "● Starting…"
        if self.state == "error":
            return "⚠ Engine Error"
        return "○ Standby"

    def tone(self) -> str:
        if self.state == "live":
            return "live"
        if self.state == "error":
            return "error"
        if self.state == "starting":
            return "starting"
        return "idle"


@dataclass(frozen=True)
class AudioSnapshot:
    state: str = "unknown"
    label: str = ""
    layout: str = ""
    muted: bool = False
    gain: Optional[float] = None


@dataclass(frozen=True)
class OutputSnapshot:
    v4l2_nodes: Tuple[str, ...] = ()
    preload_nodes: Tuple[str, ...] = ()
    audio_tap: bool = False


@dataclass(frozen=True)
class FramegenSnapshot:
    mode: str = "off"
    ready: bool = False
    active: bool = False
    output_fps: Optional[float] = None


@dataclass(frozen=True)
class ServiceSnapshot:
    installed: bool = False
    enabled: bool = False
    active: bool = False


@dataclass(frozen=True)
class RecordingSnapshot:
    active: bool = False
    elapsed: float = 0.0
    path: str = ""
    frames: int = 0
    dropped: int = 0
    error: str = ""


@dataclass
class StatusSnapshot:
    card: Any = None
    engine: EngineSnapshot = field(default_factory=EngineSnapshot)
    signal: SignalSnapshot = field(default_factory=SignalSnapshot)
    audio: AudioSnapshot = field(default_factory=AudioSnapshot)
    outputs: OutputSnapshot = field(default_factory=OutputSnapshot)
    framegen: FramegenSnapshot = field(default_factory=FramegenSnapshot)
    service: ServiceSnapshot = field(default_factory=ServiceSnapshot)
    recording: RecordingSnapshot = field(default_factory=RecordingSnapshot)
    notice: Optional[str] = None

    @property
    def card_name(self) -> str:
        return getattr(self.card, "display_name", "EverLive Console Hub")

    def hero_subtitle(self) -> str:
        if self.signal.starting:
            return "Starting Userspace Engine • Initializing capture • No Audio"
        if not self.signal.locked:
            if self.signal.cached:
                res = self.signal.resolution_label()
                fps = self.signal.fps_label()
                if res and fps:
                    return f"Last Signal: {res} • {fps} • Waiting for input • No Audio"
            port = ""
            if self.signal.port is not None:
                port = f" on HDMI {self.signal.port + 1}"
            return f"No HDMI Signal{port} • Waiting for input • No Audio"

        parts = []
        res = self.signal.resolution_label()
        if res:
            parts.append(res)
        fps = self.signal.fps_label()
        if fps:
            parts.append(fps)
        parts.append(self.signal.hdr or "SDR")
        parts.append(self.audio.label or "No Audio")
        return " • ".join(parts)


def merge_signal(base: SignalSnapshot, update: SignalSnapshot) -> SignalSnapshot:
    """Keep non-empty fields from @update, otherwise retain @base."""
    return SignalSnapshot(
        locked=update.locked or base.locked,
        width=update.width or base.width,
        height=update.height or base.height,
        interlaced=update.interlaced or base.interlaced,
        fps=update.fps if update.fps is not None else base.fps,
        chroma=update.chroma or base.chroma,
        hdr=update.hdr or base.hdr,
        cached=update.cached if update.cached else base.cached,
        starting=update.starting,
        cached_resolution=update.cached_resolution or base.cached_resolution,
        cached_fps=update.cached_fps or base.cached_fps,
        port=update.port if update.port is not None else base.port,
    )
