#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Manual Hub-side capture-to-file recorder.

The Console Hub starts this recorder only when the user presses Record.  A
background thread reads raw YUY2 frames from the shared frame store and pipes
them into ffmpeg.  The UI thread never blocks on disk or encoder I/O.

Audio is intentionally out of scope for the first iteration; the file is a
video-only MP4 so the recorder can ship without touching the engine's audio
scheduling.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from framestore import FrameStore, open_store


DEFAULT_OUTPUT_DIR = os.path.expanduser("~/Videos/EverLive")
DEFAULT_QUALITY = "balanced"
QUALITY_CRF = {"high": 18, "balanced": 23, "small": 28}
MIN_FREE_BYTES = 512 * 1024 * 1024
STOP_DISK_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class RecordingInfo:
    active: bool = False
    path: str = ""
    elapsed: float = 0.0
    frames: int = 0
    dropped: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    error: str = ""


def build_output_path(output_dir: str, when: Optional[datetime] = None) -> str:
    directory = Path(output_dir or DEFAULT_OUTPUT_DIR).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    base = f"EverLive_{stamp}"
    path = directory / f"{base}.mp4"
    suffix = 1
    while path.exists():
        path = directory / f"{base}_{suffix}.mp4"
        suffix += 1
    return str(path)


def choose_encoder(preferred: str = "auto") -> str:
    # Software H.264 is the safe default.  Hardware encoders are opt-in until
    # their device/quality handling is validated on real capture hardware.
    if preferred in ("libx264", "h264_nvenc"):
        return preferred
    return "libx264"


def build_ffmpeg_command(path: str, width: int, height: int, fps: float,
                         quality: str = DEFAULT_QUALITY,
                         codec: str = "auto") -> list[str]:
    crf = QUALITY_CRF.get(quality, QUALITY_CRF[DEFAULT_QUALITY])
    encoder = choose_encoder(codec)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "yuyv422",
        "-s", f"{width}x{height}", "-r", f"{fps:.3f}",
        "-i", "-", "-an",
    ]
    if encoder == "h264_nvenc":
        cmd.extend([
            "-c:v", "h264_nvenc", "-preset", "p5",
            "-cq", str(crf), "-pix_fmt", "yuv420p"])
    else:
        cmd.extend([
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(crf), "-pix_fmt", "yuv420p"])
    cmd.extend(["-movflags", "+faststart", path])
    return cmd


class StreamRecorder:
    """Read frames from the shared store and write them to one MP4."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._store: Optional[FrameStore] = None
        self._log = None
        self._active = False
        self._path = ""
        self._started = 0.0
        self._frames = 0
        self._dropped = 0
        self._width = 0
        self._height = 0
        self._fps = 0.0
        self._port = 0
        self._error = ""
        self._output_dir = DEFAULT_OUTPUT_DIR

    # -- public state --------------------------------------------------------

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def path(self) -> str:
        with self._lock:
            return self._path

    def snapshot(self) -> RecordingInfo:
        with self._lock:
            elapsed = (time.monotonic() - self._started
                       if self._active and self._started else 0.0)
            return RecordingInfo(
                active=self._active,
                path=self._path,
                elapsed=elapsed,
                frames=self._frames,
                dropped=self._dropped,
                width=self._width,
                height=self._height,
                fps=self._fps,
                error=self._error,
            )

    # -- lifecycle -----------------------------------------------------------

    def start(self, shm_path: str, port: int, width: int, height: int,
              fps: float, output_dir: str = DEFAULT_OUTPUT_DIR,
              quality: str = DEFAULT_QUALITY,
              codec: str = "auto") -> Tuple[bool, str]:
        with self._lock:
            if self._active:
                return False, "A recording is already in progress."
        if width <= 0 or height <= 0:
            return False, "No active video frame is available."
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg was not found in PATH."
        if quality not in QUALITY_CRF:
            quality = DEFAULT_QUALITY
        store = open_store(shm_path)
        if store is None:
            return False, "The capture frame store is not available."

        output_dir = os.path.expanduser(output_dir or DEFAULT_OUTPUT_DIR)
        log = None
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(output_dir).free
            if free < MIN_FREE_BYTES:
                store.close()
                return False, "Not enough free disk space to start recording."
            path = build_output_path(output_dir)
            log_path = Path(os.path.expanduser(
                "~/.config/everlive/recording.log"))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(log_path, "wb")
            proc = subprocess.Popen(
                build_ffmpeg_command(path, width, height, fps, quality, codec),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=log)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            store.close()
            try:
                log.close()
            except Exception:
                pass
            return False, f"Could not start ffmpeg: {exc}"

        with self._lock:
            self._store = store
            self._proc = proc
            self._log = log
            self._active = True
            self._path = path
            self._started = time.monotonic()
            self._frames = 0
            self._dropped = 0
            self._width = width
            self._height = height
            self._fps = float(fps)
            self._port = 1 if port == 1 else 0
            self._error = ""
            self._output_dir = output_dir
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="everlive-recorder", daemon=True)
            self._thread.start()
        return True, path

    def stop(self, timeout: float = 6.0) -> Tuple[bool, str]:
        with self._lock:
            if not self._active:
                error = self._error
                path = self._path
                if error:
                    return False, (f"{error} Saved to {path}"
                                   if path else error)
                return False, "Not recording."
            self._stop.set()
            thread = self._thread
            proc = self._proc
            path = self._path
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive() and proc is not None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                thread.join(2.0)
        with self._lock:
            error = self._error
            self._active = False
        if error:
            return False, f"{error} Saved to {path}" if path else error
        return True, path

    # -- worker --------------------------------------------------------------

    def _run(self):
        interval = 1.0 / self._fps if self._fps > 0 else 1.0 / 60.0
        next_t = time.monotonic()
        result_error = ""
        try:
            while not self._stop.is_set():
                frame = self._store.frame(self._port)
                if frame is None:
                    time.sleep(0.002)
                    continue
                if not frame.streaming or frame.no_signal:
                    time.sleep(0.01)
                    continue
                if frame.width != self._width or frame.height != self._height:
                    result_error = (
                        "Resolution changed during recording; the file was "
                        "finalized.")
                    break
                data = bytearray(frame.frame_bytes)
                data[:] = frame.data
                if not self._store.validate(frame):
                    with self._lock:
                        self._dropped += 1
                    continue
                if self._proc is None or self._proc.stdin is None:
                    result_error = "The encoder pipe is unavailable."
                    break
                try:
                    self._proc.stdin.write(data)
                    self._proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    result_error = "ffmpeg stopped before the recording ended."
                    break
                with self._lock:
                    self._frames += 1
                if self._frames % 60 == 0:
                    try:
                        free = shutil.disk_usage(self._output_dir).free
                        if free < STOP_DISK_BYTES:
                            result_error = "Recording stopped: disk is full."
                            break
                    except OSError:
                        pass
                now = time.monotonic()
                next_t += interval
                if next_t > now:
                    time.sleep(next_t - now)
                else:
                    next_t = now
        except Exception as exc:  # keep the UI alive and report the failure
            result_error = f"Recorder error: {exc}"
        finally:
            # Release the last frame view before closing the mapping.
            try:
                del frame
            except NameError:
                pass
            self._finish(result_error)

    def _finish(self, error: str):
        with self._lock:
            proc = self._proc
            log = self._log
            store = self._store
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except (OSError, subprocess.SubprocessError):
                    pass
            if proc.returncode not in (0, None) and not error:
                error = f"ffmpeg exited with code {proc.returncode}."
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
        if store is not None:
            store.close()
        with self._lock:
            self._active = False
            self._thread = None
            self._proc = None
            self._log = None
            self._store = None
            self._error = error
            self._started = 0.0
