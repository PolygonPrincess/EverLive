#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""
EverLive - Live Gamer 4K (GC573) Control Center & Video Viewer
Desktop Studio with low-latency direct-DMA preview for Linux gaming.
"""

import sys
import os
import signal
import subprocess
import time
import argparse
import re
import json
import shutil
import mmap
import ctypes
import struct
from typing import Optional, Tuple, Dict

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from everlive_client import EverLiveIpcClient, probe as ipc_probe
except ImportError:  # imported as a module from another working directory
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from everlive_client import EverLiveIpcClient, probe as ipc_probe

__version__ = "1.4.0"

CONFIG_DIR = os.path.expanduser("~/.config/everlive")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

GC573_VENDOR_ID = "0x1461"
GC573_DEVICE_IDS = {"0x0054", "0x0055"}
GC570D_SUBSYSTEM_ID = "0x5700"
GC573_SUBSYSTEM_ID = "0x5730"
MODEL_GC573 = "gc573"
MODEL_GC570D = "gc570d"
MODEL_NAMES = {
    MODEL_GC573: "GC573 · LIVE GAMER 4K",
    MODEL_GC570D: "GC570D · LIVE GAMER DUO",
}
ENGINE_VFIO_TEXT = "● Userspace VFIO"
V4L2_DEFAULT_DEVICE = "/dev/video10"
V4L2_DUO_SECOND_DEVICE = "/dev/video11"
AUDIO_TAP_NODE = "everlive-capture-audio"

ASPECT_OPTIONS = [
    ("Auto (Native HDMI)", "auto"),
    ("16:9 Anamorphic (RetroTINK / OSSC)", "16:9"),
    ("4:3 Retro", "4:3"),
    ("Stretch to Fill", "stretch"),
]
ASPECT_KEYS = {key for _, key in ASPECT_OPTIONS}

OSD_POSITIONS = ("tr", "br", "bl", "tl")
OSD_POSITION_OPTIONS = [
    ("Top Right", "tr"),
    ("Bottom Right", "br"),
    ("Bottom Left", "bl"),
    ("Top Left", "tl"),
]

SCALE_OPTIONS = [
    ("Aspect Fit (Bilinear)", "fit"),
    ("Integer Scale (Nearest)", "integer"),
]
SCALE_MODES = {key for _, key in SCALE_OPTIONS}

# Experimental lsfg-vk frame generation (gc573_user_streamer --framegen).
FRAMEGEN_OPTIONS = [
    ("Off — Native Ultra-Low Latency", "off"),
    ("60 → 120 FPS (LSFG 2x · 60 FPS Games)", "60to120"),
    ("30 → 60 FPS (LSFG 2x · PS5 30 FPS Games)", "30to60"),
]
FRAMEGEN_KEYS = {key for _, key in FRAMEGEN_OPTIONS}

# Older settings files / scripts used the shader-pipeline names.  Map them onto
# the two real lsfg-vk modes instead of silently resetting to off.
FRAMEGEN_ALIASES = {
    "2x": "60to120",
    "interp": "60to120",
    "interpolation": "60to120",
    "60": "60to120",
    "adaptive": "30to60",
    "max": "30to60",
    "maxhz": "30to60",
    "30": "30to60",
    "ps5": "30to60",
    "none": "off",
    "native": "off",
}

DEFAULT_SETTINGS = {
    "v4l2_loopback": False,
    "ui_mode": "simple",
    "aspect_ratio": "auto",
    "osd_enabled": True,
    "osd_position": "tr",
    "audio_tap": False,
    "hdr_tonemap": True,
    "hdr_exposure": 0.0,
    "scale_mode": "fit",
    "framegen_mode": "off",
    "fg_flow_scale": 1.0,
    "fg_perf_mode": False,
    "fg_low_latency": True,
    "minimize_to_tray": True,
    "duo_input": 0,
    "duo_edid_bridge": True,
}


def load_settings() -> dict:
    data = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass

    settings = dict(DEFAULT_SETTINGS)
    settings.update(data)
    if settings.get("ui_mode") not in ("simple", "pro"):
        settings["ui_mode"] = "simple"
    if not isinstance(settings.get("v4l2_loopback"), bool):
        settings["v4l2_loopback"] = False
    if not isinstance(settings.get("minimize_to_tray"), bool):
        settings["minimize_to_tray"] = True
    if settings.get("aspect_ratio") not in ASPECT_KEYS:
        settings["aspect_ratio"] = "auto"
    if not isinstance(settings.get("osd_enabled"), bool):
        settings["osd_enabled"] = True
    if settings.get("osd_position") not in OSD_POSITIONS:
        settings["osd_position"] = "tr"
    if not isinstance(settings.get("audio_tap"), bool):
        settings["audio_tap"] = False
    if not isinstance(settings.get("hdr_tonemap"), bool):
        settings["hdr_tonemap"] = True
    if not isinstance(settings.get("hdr_exposure"), (int, float)):
        settings["hdr_exposure"] = 0.0
    settings["hdr_exposure"] = max(-2.0, min(2.0, float(settings["hdr_exposure"])))
    if settings.get("scale_mode") not in SCALE_MODES:
        settings["scale_mode"] = "fit"
    # Experimental lsfg-vk frame generation.  Migrate the old shader-era keys.
    fg_mode = str(settings.get("framegen_mode", "off")).lower()
    fg_mode = FRAMEGEN_ALIASES.get(fg_mode, fg_mode)
    if fg_mode not in FRAMEGEN_KEYS:
        fg_mode = "off"
    settings["framegen_mode"] = fg_mode
    try:
        settings["fg_flow_scale"] = max(
            0.5, min(1.0, float(settings.get("fg_flow_scale", 1.0))))
    except (TypeError, ValueError):
        settings["fg_flow_scale"] = 1.0
    if not isinstance(settings.get("fg_perf_mode"), bool):
        settings["fg_perf_mode"] = False
    if not isinstance(settings.get("fg_low_latency"), bool):
        settings["fg_low_latency"] = True
    # The OpenGL shader pipeline (HUD protection, CAS) no longer exists.
    for stale in ("framegen_hud_protect", "cas_enabled", "cas_strength"):
        settings.pop(stale, None)
    if settings.get("duo_input") not in (0, 1):
        settings["duo_input"] = 0
    if not isinstance(settings.get("duo_edid_bridge"), bool):
        settings["duo_edid_bridge"] = True
    # "bitstream" used to be a separate EDID profile.  Every built-in profile
    # now advertises AC-3/DTS, so old settings migrate to the unified default.
    if settings.get("edid_profile") == "bitstream":
        settings["edid_profile"] = "default"
    return settings


def save_settings(data: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cur = load_settings()
        cur.update(data)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
    except Exception:
        pass


def _resolve_lsfg_dll(candidate: str) -> str:
    """Resolve a candidate shader DLL path for lsfg-vk 2.x.

    If a stale config or env var points at Windows ``Lossless.dll``, prefer a
    sibling ``lsfg-vk.dll`` (from Steam's ``lsfg-vk`` beta branch) and reject
    bare ``Lossless.dll`` which lacks the precompiled Vulkan SPIR-V shaders.
    """
    if not candidate:
        return ""
    if os.path.basename(candidate).lower() == "lossless.dll":
        sibling = os.path.join(os.path.dirname(candidate), "lsfg-vk.dll")
        if os.path.isfile(sibling) and os.access(sibling, os.R_OK):
            return sibling
        return ""
    if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
        return candidate
    return ""


def _lsfg_dll_from_conf(path: str) -> str:
    """Return the ``dll = "..."`` value from lsfg-vk's conf.toml, if usable."""
    try:
        with open(path, "r", errors="ignore") as f:
            for raw in f:
                line = raw.lstrip()
                if not line.lower().startswith("dll"):
                    continue
                first = line.find('"')
                if first < 0:
                    continue
                last = line.find('"', first + 1)
                if last < 0:
                    continue
                resolved = _resolve_lsfg_dll(line[first + 1:last])
                if resolved:
                    return resolved
    except OSError:
        pass
    return ""


def _lsfg_dll_from_libraryfolders(path: str) -> Tuple[str, bool]:
    """Scan a Steam libraryfolders.vdf for a Lossless Scaling install across all library paths.

    Returns ``(lsfg_vk_dll_path, legacy_lossless_dll_found)``.
    """
    try:
        with open(path, "r", errors="ignore") as f:
            body = f.read()
    except OSError:
        return "", False

    legacy_found = False
    lowered = body.lower()
    pos = 0
    found_candidate = ""
    while True:
        pos = lowered.find('"path"', pos)
        if pos < 0:
            break
        pos += len('"path"')
        first = body.find('"', pos)
        if first < 0:
            break
        last = body.find('"', first + 1)
        if last < 0:
            break
        library = body[first + 1:last].replace("\\", "/")
        game_dir = os.path.join(
            library, "steamapps", "common", "Lossless Scaling")
        candidate = os.path.join(game_dir, "lsfg-vk.dll")
        if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            return candidate, True
        legacy = os.path.join(game_dir, "Lossless.dll")
        if os.path.isfile(legacy) and os.access(legacy, os.R_OK):
            legacy_found = True
        pos = last + 1

    return found_candidate, legacy_found


def probe_lsfg_vk() -> Dict[str, object]:
    """Locate the lsfg-vk Vulkan layer and lsfg-vk.dll without running tools.

    Mirrors ``gc573_fg_probe_system()`` in ``gc573_user_framegen.c`` so the Hub
    can show readiness before a stream is even started, and after the user
    installs the layer or enables the Steam beta while the Hub stays open.
    """
    layer_dir = ""
    home = os.path.expanduser("~")
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join(
        home, ".local", "share")
    search = [
        "/usr/share/vulkan/implicit_layer.d",
        "/etc/vulkan/implicit_layer.d",
        os.path.join(xdg_data, "vulkan", "implicit_layer.d"),
    ]
    for directory in search:
        try:
            for name in os.listdir(directory):
                lower = name.lower()
                if not lower.endswith(".json"):
                    continue
                hit = "lsfg" in lower
                if not hit:
                    try:
                        with open(os.path.join(directory, name), "r",
                                  errors="ignore") as f:
                            body = f.read().lower()
                        hit = "lsfg" in body or "lossless" in body
                    except OSError:
                        pass
                if hit:
                    layer_dir = directory
                    break
        except OSError:
            continue
        if layer_dir:
            break

    dll_path = _resolve_lsfg_dll(os.environ.get("LSFGVK_DLL_PATH") or "")
    legacy_dll_found = False
    if not dll_path:
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            home, ".config")
        dll_path = _lsfg_dll_from_conf(
            os.path.join(xdg_cfg, "lsfg-vk", "conf.toml"))
    if not dll_path:
        for root in ("~/.local/share/Steam", "~/.steam/steam"):
            base = os.path.expanduser(root)
            game_dir = os.path.join(
                base, "steamapps", "common", "Lossless Scaling")
            candidate = os.path.join(game_dir, "lsfg-vk.dll")
            if os.path.isfile(candidate) and os.access(candidate, os.R_OK):
                dll_path = candidate
                break
            legacy = os.path.join(game_dir, "Lossless.dll")
            if os.path.isfile(legacy) and os.access(legacy, os.R_OK):
                legacy_dll_found = True
            dll_path, vdf_legacy = _lsfg_dll_from_libraryfolders(
                os.path.join(base, "steamapps", "libraryfolders.vdf"))
            if vdf_legacy:
                legacy_dll_found = True
            if dll_path:
                break

    return {
        "layer_found": bool(layer_dir),
        "dll_found": bool(dll_path),
        "legacy_dll_found": legacy_dll_found,
        "layer_dir": layer_dir,
        "dll_path": dll_path,
    }


def _read_sysfs(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def detect_gc573_driver() -> Dict[str, Optional[str]]:
    """Return the capture card binding and model: vfio, kernel, or unknown.

    GC573 (Live Gamer 4K) and GC570D (Live Gamer DUO) both use PCI device
    1461:0054.  Only ``subsystem_device`` separates them: 0x5730 is the GC573
    and 0x5700 is the GC570D.  The BOLT (1461:0055) is always a GC573-class
    device.  In VFIO mode there is intentionally no /dev/video* node and no
    ALSA card; the userspace engine owns the device instead.
    """
    import glob

    result: Dict[str, Optional[str]] = {
        "mode": "unknown",
        "driver": None,
        "pci": None,
        "subsystem": None,
        "model": MODEL_GC573,
    }

    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        vendor = _read_sysfs(os.path.join(dev, "vendor")).lower()
        device = _read_sysfs(os.path.join(dev, "device")).lower()
        if vendor != GC573_VENDOR_ID or device not in GC573_DEVICE_IDS:
            continue

        subsystem = _read_sysfs(os.path.join(dev, "subsystem_device")).lower()
        result["pci"] = os.path.basename(dev)
        result["subsystem"] = subsystem or None
        if device == "0x0054" and subsystem == GC570D_SUBSYSTEM_ID:
            result["model"] = MODEL_GC570D
        else:
            result["model"] = MODEL_GC573

        driver_link = os.path.join(dev, "driver")
        try:
            if os.path.islink(driver_link):
                driver = os.path.basename(os.path.realpath(driver_link))
                result["driver"] = driver
                if driver == "vfio-pci":
                    result["mode"] = "vfio"
                elif driver in ("open_gc573", "gc570d"):
                    result["mode"] = "kernel"
        except OSError:
            pass
        break

    return result


def _proc_running(name_prefix: str) -> bool:
    """Match /proc/<pid>/comm (15-char safe) without relying on pgrep."""
    try:
        pids = os.listdir("/proc")
    except OSError:
        return False

    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm", "r") as f:
                if f.read().strip().startswith(name_prefix):
                    return True
        except OSError:
            pass
    return False


def userd_dir(model: str = MODEL_GC573) -> str:
    folder = "opengc570d-userd" if model == MODEL_GC570D else "opengc573-userd"
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", folder))


def userd_bin(name: str, model: str = MODEL_GC573) -> str:
    return os.path.join(userd_dir(model), name)


def _stop_vfio_daemon(wait_s: float = 0.8):
    """Release BAR0/VFIO before the userspace streamer starts."""
    for cmd in (["systemctl", "--user", "stop", "everlive-userd"],
                ["pkill", "-x", "everlive_userd"],
                ["pkill", "-x", "opengc570d-userd"]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not (_proc_running("everlive_userd") or
                _proc_running("opengc570d-user")):
            return
        time.sleep(0.05)


def _vfio_streamer_running() -> bool:
    # Both binaries are longer than 15 chars, so /proc/<pid>/comm is
    # truncated to "gc573_user_stre" or "opengc570d-user".
    return (_proc_running("gc573_user_stre") or
            _proc_running("opengc570d-user"))


def _raise_memlock_limit():
    """Native 4K frames need ~17 MB of pinned VFIO memory.

    The GUI launches the streamer directly (not via play-intern.sh), so it
    must raise its own soft limit before fork/exec.  This is best-effort;
    the streamer reports a clean error if the hard limit is still too small.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        try:
            resource.setrlimit(resource.RLIMIT_MEMLOCK,
                               (resource.RLIM_INFINITY,
                                resource.RLIM_INFINITY))
            return
        except (ValueError, OSError):
            pass
        if soft != hard:
            try:
                resource.setrlimit(resource.RLIMIT_MEMLOCK, (hard, hard))
            except (ValueError, OSError):
                pass
    except Exception:
        pass

# PySide6 is optional when run with --cli
try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QGridLayout, QLabel, QPushButton, QComboBox, QCheckBox, QSlider, QFrame,
        QGroupBox, QMessageBox, QStackedLayout, QSizePolicy, QFileDialog,
        QSystemTrayIcon, QMenu, QDoubleSpinBox, QAbstractSpinBox, QScrollArea,
        QColorDialog, QDialog, QLineEdit, QTextEdit
    )
    from PySide6.QtCore import Qt, QTimer, QThread, Signal, QEvent, QUrl, QSize, QRect, QPoint
    from PySide6.QtGui import (
        QGuiApplication, QFont, QIcon, QColor, QPalette, QKeySequence, QShortcut,
        QDesktopServices, QPixmap, QPainter, QAction, QImage, QPen, QBrush, QCursor,
        QFontMetrics
    )
    HAS_QT = True
except ImportError:
    HAS_QT = False


def find_gc573_vdev() -> str:
    """Auto-detects the V4L2 device node for open_gc573."""
    import glob
    # 1. Check open_gc573 driver binding in sysfs
    for cand in sorted(glob.glob("/sys/bus/pci/drivers/open_gc573/*/video4linux/video*")):
        vdev_name = os.path.basename(cand)
        dev_path = f"/dev/{vdev_name}"
        if os.path.exists(dev_path):
            return dev_path

    # 2. Check /sys/class/video4linux/*/device/driver symlink
    for d in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            driver_link = os.path.realpath(os.path.join(d, "device", "driver"))
            if os.path.basename(driver_link) == "open_gc573":
                dev_path = f"/dev/{os.path.basename(d)}"
                if os.path.exists(dev_path):
                    return dev_path
        except Exception:
            pass

    # 3. Check /sys/class/video4linux/*/name
    for name_file in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(name_file, "r") as f:
                name = f.read().lower()
                if "gc573" in name or "live gamer 4k" in name or "open_gc573" in name:
                    dev_path = f"/dev/{os.path.basename(os.path.dirname(name_file))}"
                    if os.path.exists(dev_path):
                        return dev_path
        except Exception:
            pass

    # Default fallback if card has not yet registered
    return "/dev/video0"


def find_gc573_adev() -> str:
    """Auto-detects ALSA capture card for GC573."""
    try:
        with open("/proc/asound/cards", "r") as f:
            for line in f:
                if "GC573" in line or "Live Gamer 4K" in line or "A4K" in line:
                    m = re.search(r"^\s*(\d+)\s+\[([^\]]+)\]", line)
                    if m:
                        card_id = m.group(2).strip()
                        return f"hw:CARD={card_id},DEV=0"
    except Exception:
        pass
    return "hw:CARD=A4K,DEV=0"


def find_pipewire_source_node() -> Optional[str]:
    """Finds the native PipeWire Audio/Source node.name for GC573 HDMI Capture."""
    try:
        out = subprocess.check_output(["pw-dump", "Node"], stderr=subprocess.DEVNULL)
        for n in json.loads(out):
            p = n.get("info", {}).get("props", {})
            if p.get("media.class") == "Audio/Source":
                driver = p.get("alsa.driver_name", "")
                card_id = p.get("alsa.id", "")
                card_name = p.get("alsa.card_name", "")
                node_name = p.get("node.name", "")
                node_desc = p.get("node.description", "")
                alsa_name = p.get("alsa.name", "")

                is_gc573 = (
                    driver == "open_gc573"
                    or card_id in ("A4K", "GC573")
                    or "live gamer 4k" in card_name.lower()
                    or "live gamer 4k" in node_desc.lower()
                    or "gc573" in node_name.lower()
                    or "gc573" in node_desc.lower()
                    or "gc573" in alsa_name.lower()
                )
                is_line = (
                    "line" in node_desc.lower()
                    or "line" in alsa_name.lower()
                    or "input-1" in node_name
                    or "pro-input-1" in node_name
                    or p.get("alsa.device") == 1
                )
                if is_gc573 and not is_line:
                    return node_name
    except Exception:
        pass

    # Secondary check via wpctl status if pw-dump didn't match directly
    try:
        import re
        out = subprocess.check_output(["wpctl", "status"], stderr=subprocess.DEVNULL).decode("utf-8", errors="ignore")
        in_sources = False
        for line in out.splitlines():
            if "Sources:" in line:
                in_sources = True
                continue
            elif in_sources and ("Sinks:" in line or "Filters:" in line or "Streams:" in line):
                break
            if in_sources and ("live gamer 4k" in line.lower() or "gc573" in line.lower() or "a4k" in line.lower()) and "line" not in line.lower():
                m = re.search(r"^\s*[│\s*]*(\d+)\.", line)
                if m:
                    wp_id = m.group(1)
                    # Resolve numeric wpctl ID to exact PipeWire node.name
                    try:
                        insp = subprocess.check_output(["wpctl", "inspect", wp_id], text=True, stderr=subprocess.DEVNULL)
                        for insp_line in insp.splitlines():
                            if "node.name" in insp_line and "=" in insp_line:
                                actual_name = insp_line.split("=")[-1].strip().strip('"')
                                if actual_name:
                                    return actual_name
                    except Exception:
                        pass
                    return wp_id
    except Exception:
        pass

    return None


PRESETS = {
    "Auto (HDMI Signal)": None,
    "3840x2160 @ 60 FPS":  {"width": 3840, "height": 2160, "fps": 60,  "fmt": "YUYV"},
    "3840x2160 @ 30 FPS":  {"width": 3840, "height": 2160, "fps": 30,  "fmt": "YUYV"},
    "3840x2160 @ 24 FPS":  {"width": 3840, "height": 2160, "fps": 24,  "fmt": "YUYV"},
    "2880x2160 @ 60 FPS (4:3 4K)": {"width": 2880, "height": 2160, "fps": 60, "fmt": "YUYV"},
    "3840x1080 @ 120 FPS": {"width": 3840, "height": 1080, "fps": 120, "fmt": "YUYV"},
    "3840x1080 @ 60 FPS":  {"width": 3840, "height": 1080, "fps": 60,  "fmt": "YUYV"},
    "2560x1440 @ 144 FPS": {"width": 2560, "height": 1440, "fps": 144, "fmt": "YUYV"},
    "2560x1440 @ 120 FPS": {"width": 2560, "height": 1440, "fps": 120, "fmt": "YUYV"},
    "2560x1440 @ 100 FPS": {"width": 2560, "height": 1440, "fps": 100, "fmt": "YUYV"},
    "2560x1440 @ 60 FPS":  {"width": 2560, "height": 1440, "fps": 60,  "fmt": "YUYV"},
    "2560x1080 @ 144 FPS": {"width": 2560, "height": 1080, "fps": 144, "fmt": "YUYV"},
    "2560x1080 @ 60 FPS":  {"width": 2560, "height": 1080, "fps": 60,  "fmt": "YUYV"},
    "1920x1080 @ 240 FPS": {"width": 1920, "height": 1080, "fps": 240, "fmt": "YUYV"},
    "1920x1080 @ 180 FPS": {"width": 1920, "height": 1080, "fps": 180, "fmt": "YUYV"},
    "1920x1080 @ 144 FPS": {"width": 1920, "height": 1080, "fps": 144, "fmt": "YUYV"},
    "1920x1080 @ 120 FPS": {"width": 1920, "height": 1080, "fps": 120, "fmt": "YUYV"},
    "1920x1080 @ 100 FPS": {"width": 1920, "height": 1080, "fps": 100, "fmt": "YUYV"},
    "1920x1080 @ 60 FPS":  {"width": 1920, "height": 1080, "fps": 60,  "fmt": "YUYV"},
    "1920x1080 @ 50 FPS":  {"width": 1920, "height": 1080, "fps": 50,  "fmt": "YUYV"},
    "1280x720 @ 60 FPS":   {"width": 1280, "height": 720,  "fps": 60,  "fmt": "YUYV"},
    "720x480 @ 60 FPS (480p)": {"width": 720, "height": 480, "fps": 60, "fmt": "YUYV"},
    "720x576 @ 50 FPS (576p)": {"width": 720, "height": 576, "fps": 50, "fmt": "YUYV"},
    "1920x240 @ 60 FPS (240p CRT)": {"width": 1920, "height": 240, "fps": 60, "fmt": "YUYV"},
    "1440x240 @ 60 FPS (240p)": {"width": 1440, "height": 240, "fps": 60, "fmt": "YUYV"},
    "2880x240 @ 60 FPS (240p RT4K)": {"width": 2880, "height": 240, "fps": 60, "fmt": "YUYV"},
}

LED_MODES = {
    "Off (Dark)": 0,
    "Blue": 1,
    "Green": 2,
    "Red": 3,
    "Cyan": 4,
    "Magenta": 5,
    "Yellow": 6,
    "White": 7,
    "Rainbow Wave (Multi-Zone)": 8,
    "Spectrum Cycle (Breathing)": 9,
}

LED_NAME_MAP = {
    "0": 0, "off": 0, "aus": 0, "dark": 0,
    "1": 1, "blue": 1, "blau": 1,
    "2": 2, "green": 2, "gruen": 2, "grün": 2,
    "3": 3, "red": 3, "rot": 3,
    "4": 4, "cyan": 4, "tuerkis": 4, "türkis": 4,
    "5": 5, "magenta": 5, "lila": 5, "purple": 5,
    "6": 6, "yellow": 6, "gelb": 6,
    "7": 7, "white": 7, "weiss": 7, "weiß": 7,
    "8": 8, "wave": 8, "welle": 8, "dream": 8, "softdream": 8,
    "9": 9, "rainbow": 9, "cycle": 9, "regenbogen": 9,
}

# Hardware EDID profiles understood by gc573_user_streamer --edid.
# The display text is what the user sees; the key is the CLI value.
# Every built-in profile advertises L-PCM, Dolby Digital (AC-3) and DTS 5.1,
# so compressed audio is detected and decoded without a profile switch.
EDID_PROFILES = [
    ("Default (4K60 / 1440p120 HDR)", "default"),
    ("Force 1440p @ 120 Hz", "1440p120"),
    ("Force 1080p @ 240 Hz", "1080p240"),
    ("Load Custom EDID (.bin)...", "custom"),
]
EDID_PROFILE_KEYS = {key for _, key in EDID_PROFILES if key != "custom"}

LED_COLORS = {
    0: "#334155",  # Aus (Dunkel)
    1: "#3b82f6",  # Blau (Standard)
    2: "#10b981",  # Grün
    3: "#ef4444",  # Rot
    4: "#00ccff",  # Cyan / Türkis
    5: "#d946ef",  # Magenta / Lila
    6: "#eab308",  # Gelb
    7: "#f8fafc",  # Weiß
    8: "#a855f7",  # Rainbow Wave (Multi-Zone)
    9: "#00ccff",  # Spectrum Cycle
}

# RGB values understood by everlive_userd --color.  Cyan matches the verified
# streamer/daemon colour (0, 204, 255) rather than the kernel classdev value.
LED_RGB_VALUES = {
    0: (0, 0, 0),
    1: (0, 0, 255),
    2: (0, 255, 0),
    3: (255, 0, 0),
    4: (0, 204, 255),
    5: (255, 0, 255),
    6: (255, 255, 0),
    7: (255, 255, 255),
    8: (168, 85, 247),
    9: (0, 204, 255),
}


def led_hex_for_mode(mode: int) -> str:
    """LED mode index -> "#rrggbb" understood by gc573_user_streamer."""
    red, green, blue = LED_RGB_VALUES.get(int(mode), (0, 204, 255))
    return f"#{red:02x}{green:02x}{blue:02x}"


def send_gc570d_ctl(*args: str) -> Optional[str]:
    """Send one command to a running GC570D daemon."""
    ctl = userd_bin("gc570d-ctl", MODEL_GC570D)
    if not os.access(ctl, os.X_OK):
        return None
    try:
        return subprocess.check_output(
            [ctl, *args], text=True, stderr=subprocess.DEVNULL, timeout=2.0)
    except Exception:
        return None


def set_gc570d_led(mode: int) -> bool:
    """Apply a GC570D LED effect through gc570d-ctl."""
    mode = int(mode)
    if mode == 0:
        effect = "off"
    elif mode == 8:
        effect = "colorful"
    elif mode == 9:
        effect = "breathing"
    else:
        effect = "static"
    if send_gc570d_ctl("led", effect) is None:
        return False
    save_settings({"led_mode": mode})
    return True


def set_vfio_led(mode: int, async_apply: bool = False) -> bool:
    """Apply an LED colour through everlive_userd (VFIO mode).

    everlive_userd owns BAR0 in standby.  A colour change therefore restarts
    the daemon with --color.  While the userspace streamer is running it owns
    BAR0 and drives the LED itself; in that case the choice is only persisted
    and restored after the streamer exits.
    """
    if async_apply:
        import threading
        threading.Thread(target=set_vfio_led, args=(mode, False), daemon=True).start()
        return True

    mode = int(mode)
    resolved = userd_bin("everlive_userd")
    if not (os.path.isfile(resolved) and os.access(resolved, os.X_OK)):
        return False

    if _vfio_streamer_running():
        save_settings({"led_mode": mode})
        return True

    _stop_vfio_daemon()
    args = [resolved, "--daemon"]
    if mode == 8:
        args.append("--wave")
    elif mode == 9:
        args.append("--rainbow")
    elif mode == 0:
        args.append("--off")
    else:
        red, green, blue = LED_RGB_VALUES.get(mode, (0, 0, 0))
        args.extend(["--color", str(red), str(green), str(blue)])

    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False

    time.sleep(0.35)
    if _proc_running("everlive_userd"):
        save_settings({"led_mode": mode})
        return True
    return False


def create_led_icon(mode: int, size: int = 14):
    """Creates a smooth anti-aliased circular color swatch for the LED mode dropdown."""
    if not HAS_QT:
        return None
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    color_hex = LED_COLORS.get(mode, "#94a3b8")
    painter.setBrush(QColor(color_hex))
    if mode == 0:
        painter.setPen(QColor("#64748b"))
    elif mode == 7:
        painter.setPen(QColor("#94a3b8"))
    else:
        painter.setPen(Qt.NoPen)
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pix)


def find_led_sysfs_path() -> Optional[str]:
    """Locates the open_gc573 standard multicolor LED class or legacy sysfs led_mode attribute."""
    import glob
    led_class = "/sys/class/leds/gc573:rgb:status"
    if os.path.exists(led_class):
        return led_class
    candidates = glob.glob("/sys/bus/pci/drivers/open_gc573/*/led_mode")
    if candidates and os.path.exists(candidates[0]):
        return candidates[0]
    return None


def read_led_mode() -> int:
    """Reads current LED mode from kernel driver."""
    path = find_led_sysfs_path()
    if not path:
        return -1
    try:
        if os.path.isdir(path):
            # Official Linux Multicolor LED classdev
            with open(os.path.join(path, "brightness"), "r") as f:
                brightness = int(f.read().strip())
            if brightness == 0:
                return 0
            with open(os.path.join(path, "multi_intensity"), "r") as f:
                vals = [int(x) > 0 for x in f.read().strip().split()]
            if len(vals) >= 3:
                r, g, b = vals[0], vals[1], vals[2]
                modes = {
                    (False, False, False): 0,
                    (False, False, True): 1,
                    (False, True, False): 2,
                    (True, False, False): 3,
                    (False, True, True): 4,
                    (True, False, True): 5,
                    (True, True, False): 6,
                    (True, True, True): 7,
                }
                return modes.get((r, g, b), 1)
            return 1
        else:
            with open(path, "r") as f:
                return int(f.read().strip())
    except Exception:
        pass
    return -1


def write_led_mode(mode: int) -> bool:
    """Applies new LED mode to hardware in real-time and persists it."""
    path = find_led_sysfs_path()
    if not path:
        return False
    success = False
    try:
        if os.path.isdir(path):
            # Official Linux Multicolor LED classdev
            colors = {
                0: ((0, 0, 0), 0),
                1: ((0, 0, 255), 255),
                2: ((0, 255, 0), 255),
                3: ((255, 0, 0), 255),
                4: ((0, 255, 255), 255),
                5: ((255, 0, 255), 255),
                6: ((255, 255, 0), 255),
                7: ((255, 255, 255), 255),
            }
            rgb, brightness = colors.get(mode, ((0, 0, 0), 0))
            with open(os.path.join(path, "multi_intensity"), "w") as f:
                f.write(f"{rgb[0]} {rgb[1]} {rgb[2]}\n")
            with open(os.path.join(path, "brightness"), "w") as f:
                f.write(f"{brightness}\n")
            success = True
        else:
            with open(path, "w") as f:
                f.write(f"{mode}\n")
            success = True
    except Exception:
        success = False

    if success:
        save_settings({"led_mode": mode})
    return success


def find_hpd_pulse_sysfs_path() -> Optional[str]:
    """Locates the open_gc573 sysfs hpd_pulse attribute."""
    import glob
    candidates = glob.glob("/sys/bus/pci/drivers/open_gc573/*/hpd_pulse")
    if candidates and os.path.exists(candidates[0]):
        return candidates[0]
    return None


def trigger_hpd_pulse() -> bool:
    """Pulses the hardware HDMI Hot-Plug Detect line to trigger a physical HDMI re-sync."""
    path = find_hpd_pulse_sysfs_path()
    if not path:
        return False
    try:
        with open(path, "w") as f:
            f.write("1\n")
        return True
    except Exception:
        return False


def _is_everlive_audio_line(line: str) -> bool:
    """Match EverLive audio streams in wpctl/pactl output.

    The kernel path names its nodes after the capture source; the userspace
    VFIO engine plays through SDL2, so its sink-input is identified by the
    process binary instead.
    """
    low = line.lower()
    if ("gc573_user_streamer" in low or "opengc570d-user" in low or
            "sdl application" in low):
        return True
    if "avermedia live gamer 4k pro" in low and "pro 1" not in low:
        return True
    return (any(s in low for s in ("everlive", "everlive-audio", "gc573-audio"))
            and "input." not in low)


def _wpctl_everlive_stream_ids() -> list:
    """PipeWire stream ids for EverLive playback, via wpctl."""
    ids = []
    try:
        out = subprocess.check_output(["wpctl", "status"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=0.5)
    except Exception:
        return ids

    in_audio = False
    for line in out.splitlines():
        line_s = line.strip()
        if line_s.startswith("Audio"):
            in_audio = True
        elif in_audio and (line_s.startswith("Video") or line_s.startswith("Settings")):
            in_audio = False
        if in_audio and _is_everlive_audio_line(line_s):
            for part in line_s.split():
                clean = part.strip(".│*")
                if clean.isdigit():
                    ids.append(clean)
                    break
    return ids


def _pactl_everlive_sink_ids() -> list:
    """PulseAudio/PipeWire-pulse sink-input ids for EverLive playback."""
    ids = []
    try:
        out = subprocess.check_output(["pactl", "list", "sink-inputs"],
                                      text=True, stderr=subprocess.DEVNULL,
                                      timeout=0.5)
    except Exception:
        return ids

    current = None
    matches = False

    def flush():
        if current is not None and matches:
            ids.append(current)

    for line in out.splitlines():
        line_s = line.strip()
        if line_s.startswith("Sink-Eingabe #") or line_s.startswith("Sink Input #"):
            flush()
            current = line_s.split("#")[-1].strip()
            matches = False
        elif current is not None and _is_everlive_audio_line(line_s):
            matches = True
    flush()
    return ids


def set_audio_stream_unity():
    """Sets the userspace SDL2 sink-input in PipeWire to 100% (unity gain).

    Userspace VFIO engines (gc573_user_streamer and gc570d-user) perform
    software volume scaling internally with a clickless ramp and hotkey
    controls. The host PipeWire stream must remain at unity (100%) so that
    volume is not double-attenuated.
    """
    try:
        for target in _wpctl_everlive_stream_ids():
            subprocess.run(["wpctl", "set-volume", target, "1.00"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    try:
        for target in _pactl_everlive_sink_ids():
            subprocess.run(["pactl", "set-sink-input-volume", target, "100%"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def set_audio_volume(vol_pct: int):
    """Sets EverLive stream volume via wpctl / pactl (0-150%).

    Covers both the kernel PipeWire stream and the SDL2 sink-input created by
    gc573_user_streamer in VFIO mode.
    """
    vol_float = vol_pct / 100.0
    try:
        for target in _wpctl_everlive_stream_ids():
            subprocess.run(["wpctl", "set-volume", target, f"{vol_float:.2f}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    try:
        for target in _pactl_everlive_sink_ids():
            subprocess.run(["pactl", "set-sink-input-volume", target, f"{vol_pct}%"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def set_audio_mute(mute: bool):
    """Mutes or unmutes EverLive playback via wpctl / pactl."""
    mute_arg = "1" if mute else "0"
    try:
        for target in _wpctl_everlive_stream_ids():
            subprocess.run(["wpctl", "set-mute", target, mute_arg],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    try:
        for target in _pactl_everlive_sink_ids():
            subprocess.run(["pactl", "set-sink-input-mute", target, mute_arg],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def default_sink_issue():
    """Return a hint when the default output is muted or at 0%, else None."""
    try:
        out = subprocess.check_output(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            text=True, stderr=subprocess.DEVNULL).strip()
        vol_m = re.search(r"Volume:\s*([0-9.]+)", out)
        if vol_m:
            if "MUTED" in out.upper():
                return "default audio sink is muted"
            if float(vol_m.group(1)) <= 0.0:
                return "default audio sink is at 0%"
            return None
    except Exception:
        pass

    try:
        mute = subprocess.check_output(
            ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
            text=True, stderr=subprocess.DEVNULL)
        if "yes" in mute.lower():
            return "default audio sink is muted"
        vol = subprocess.check_output(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
            text=True, stderr=subprocess.DEVNULL)
        vol_m = re.search(r"(\d+)%", vol)
        if vol_m and int(vol_m.group(1)) == 0:
            return "default audio sink is at 0%"
    except Exception:
        pass
    return None


class GameStreamer:
    """Manages low-latency video and audio child processes."""
    def __init__(self, vdev: Optional[str] = None, adev: Optional[str] = None, force_mpv: bool = False):
        self.hw_info = detect_gc573_driver()
        self.mode = self.hw_info.get("mode", "unknown")
        self.model = self.hw_info.get("model", MODEL_GC573)
        self.duo_mode = self.model == MODEL_GC570D
        self.model_label = MODEL_NAMES.get(self.model, MODEL_NAMES[MODEL_GC573])
        self.vfio_mode = self.mode == "vfio"
        self.kernel_mode = self.mode == "kernel"

        if self.vfio_mode:
            # VFIO intentionally has no V4L2 node and no ALSA card.  Never
            # probe them, or the GUI would report misleading warnings.
            self.vdev = vdev or ""
            self.adev = adev or ""
            self.pw_node = None
        else:
            self.vdev = vdev if vdev is not None else find_gc573_vdev()
            self.adev = adev if adev is not None else find_gc573_adev()
            self.pw_node = find_pipewire_source_node()

        self.force_mpv = bool(force_mpv) and not self.vfio_mode
        self.mpv_proc: Optional[subprocess.Popen] = None
        self.arecord_proc: Optional[subprocess.Popen] = None
        self.pwplay_proc: Optional[subprocess.Popen] = None
        self.is_running = False

        # Userspace VFIO engine status cache.  The streamer writes a
        # line-buffered log which is parsed below; the last signal is also
        # persisted so standby can show it without touching the hardware.
        if self.duo_mode:
            self.streamer_bin = userd_bin("opengc570d-userd", self.model)
            self.userd_bin = self.streamer_bin
            self.probe_bin = ""
            self.ctl_bin = userd_bin("gc570d-ctl", self.model)
        else:
            self.streamer_bin = userd_bin("gc573_user_streamer", self.model)
            self.userd_bin = userd_bin("everlive_userd", self.model)
            self.probe_bin = userd_bin("gc573_user_probe", self.model)
            self.ctl_bin = ""
        self._vfio_log_path = os.path.join(CONFIG_DIR, "streamer.log")
        self._vfio_log_pos = 0
        self._vfio_live = False
        self._vfio_saw_output = False
        self._duo_no_signal = False
        # Last audio state parsed from the streamer log; shown in the capsule.
        self._vfio_audio = {"state": "unknown", "detail": "", "kind": ""}
        saved_aspect = load_settings().get("aspect_ratio", "auto")
        self.aspect = saved_aspect if saved_aspect in ASPECT_KEYS else "auto"
        self.headless = False
        self.v4l2_device: Optional[str] = None
        self.v4l2_device_2: Optional[str] = None
        self.duo_input = 0
        saved_signal = load_settings().get("last_signal")
        self._vfio_signal = saved_signal if isinstance(saved_signal, dict) else None

    def _update_vfio_signal(self, width: int, height: int, scan: str,
                            rate_hz: int, rate_frac: int,
                            chroma: Optional[str] = None):
        resolution = f"{width}x{height}{scan}"
        framerate = f"{rate_hz}.{rate_frac:02d} FPS"
        previous = self._vfio_signal or {}
        if chroma is None:
            chroma = previous.get("chroma", "")

        info = {
            "locked": True,
            "resolution": resolution,
            "framerate": framerate,
            "chroma": chroma,
            "hdr": previous.get("hdr", ""),
        }
        self._vfio_signal = info
        self._vfio_live = not getattr(self, "_duo_no_signal", False)

        if (previous.get("resolution") != resolution or
                previous.get("framerate") != framerate or
                previous.get("chroma") != chroma):
            save_settings({"last_signal": {
                "resolution": resolution,
                "framerate": framerate,
                "chroma": chroma,
                "hdr": info["hdr"],
            }})

    @staticmethod
    def _friendly_chroma(raw: str) -> str:
        raw = (raw or "").strip()
        return {
            "RGB": "RGB 4:4:4",
            "YCbCr 4:2:2": "YUV 4:2:2",
            "YCbCr 4:4:4": "YUV 4:4:4",
            "YCbCr 4:2:0": "YUV 4:2:0",
        }.get(raw, raw)

    def _parse_streamer_line(self, line: str):
        if not line:
            return
        self._vfio_saw_output = True

        # [VIDEO] Capturing 2560x1440p @ 120.00 Hz (VIP 120 Hz, RGB, pclk 483000 kHz) -> ...
        m = re.search(
            r"\[VIDEO\] Capturing (\d+)x(\d+)([ip]) @ (\d+)\.(\d+) Hz "
            r"\([^,]+, ([^,]+), pclk", line)
        if m:
            self._update_vfio_signal(
                int(m.group(1)), int(m.group(2)), m.group(3),
                int(m.group(4)), int(m.group(5)),
                self._friendly_chroma(m.group(6)))
            return

        # [STREAM] 2560x1440p @ 120.00 Hz -> 2560x1440 | FPS: 120.0 | Frames: ...
        m = re.search(
            r"\[STREAM\] (\d+)x(\d+)([ip]) @ (\d+)\.(\d+) Hz -> "
            r"(\d+)x(\d+) \| FPS: ([0-9.]+)", line)
        if m:
            fps = float(m.group(8))
            if fps > 0.5:
                self._duo_no_signal = False
            self._update_vfio_signal(
                int(m.group(1)), int(m.group(2)), m.group(3),
                int(m.group(4)), int(m.group(5)))
            return

        # Audio diagnostics; keep the last state for the status capsule and
        # the Audio Format label.
        m = re.search(r"\[AUDIO\] Decoding (.+?) -> Stereo", line)
        if m:
            self._vfio_audio = {"state": "on",
                                "detail": f"{m.group(1).strip()} -> Stereo",
                                "kind": "bitstream"}
            return
        m = re.search(r"\[AUDIO\] Bitstream detected: (.+)", line)
        if m:
            # Streamer appends ", muted)" while it cannot decode the burst
            # (Phase 1 guard) and omits it once decoding is active.
            detail = m.group(1).strip()
            state = "detecting"
            if detail.endswith(", muted)"):
                detail = detail[:-len(", muted)")] + ") (Muted)"
                state = "muted"
            self._vfio_audio = {"state": state,
                                "detail": detail,
                                "kind": "bitstream"}
            return
        m = re.search(r"\[AUDIO\] (.+?) \(muted\)", line)
        if m:
            self._vfio_audio = {"state": "muted",
                                "detail": f"{m.group(1).strip()} (Muted)",
                                "kind": "bitstream"}
            return
        m = re.search(r"SDL audio: opened (\d+) Hz, (\d+) ch, (\d+)-bit", line)
        if m:
            self._vfio_audio = {"state": "on",
                                "detail": f"{m.group(1)} Hz {m.group(2)} ch",
                                "kind": "lpcm"}
            return
        m = re.search(r"SDL audio: playing (\d+) Hz, 2 ch, (\d+)-bit via (\S+)", line)
        if m:
            self._vfio_audio = {"state": "on",
                                "detail": f"{m.group(1)} Hz 2 ch",
                                "kind": "lpcm"}
            return
        m = re.search(r"\[AUDIO\] (\d+) Hz (\d+) ch (\d+)-bit", line)
        if m:
            self._vfio_audio = {"state": "on",
                                "detail": f"{m.group(1)} Hz {m.group(2)} ch",
                                "kind": "lpcm"}
            return
        if "SDL audio unavailable" in line or "dummy driver" in line:
            self._vfio_audio = {"state": "unavailable", "detail": "", "kind": ""}
            return
        if "receiver reports no playable stream" in line:
            self._vfio_audio = {"state": "absent", "detail": "", "kind": ""}
            return
        if "format not playable" in line:
            self._vfio_audio = {"state": "unsupported", "detail": "", "kind": ""}
            return
        if "HDMI audio DMA start failed" in line:
            self._vfio_audio = {"state": "error", "detail": "", "kind": ""}
            return

        if ("HDMI signal lost" in line or
                "Signal vanished" in line or
                "Waiting for HDMI signal" in line):
            self._vfio_live = False
            return
        if "EVERLIVE DUO" in line and "no signal" in line.lower():
            self._duo_no_signal = True
            self._vfio_live = False

    def _read_vfio_log(self):
        if not self._vfio_log_path:
            return
        try:
            with open(self._vfio_log_path, "r", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if self._vfio_log_pos > size:
                    self._vfio_log_pos = 0
                f.seek(self._vfio_log_pos)
                for line in f:
                    self._parse_streamer_line(line.strip())
                self._vfio_log_pos = f.tell()
        except OSError:
            pass

    def get_vfio_signal_info(self) -> Dict[str, object]:
        """Signal snapshot for the userspace engine, without V4L2/ALSA."""
        self._read_vfio_log()
        sig = self._vfio_signal or {}
        live = bool(self.is_running and self._vfio_live and sig.get("resolution"))
        has_cache = bool(sig.get("resolution") and
                         sig.get("resolution") != "No Signal")

        return {
            "locked": live,
            "resolution": sig.get("resolution", "No Signal"),
            "framerate": sig.get("framerate", ""),
            "chroma": sig.get("chroma", ""),
            "hdr": sig.get("hdr", ""),
            "cached": (not live) and has_cache,
            "starting": bool(self.is_running and not live and
                             not self._vfio_saw_output),
            "engine": "vfio",
        }

    def audio_state(self) -> Dict[str, str]:
        """Audio state parsed from the userspace streamer log."""
        return dict(self._vfio_audio)

    def audio_format_label(self) -> str:
        """Human-readable HDMI audio format for the hub status row."""
        state = self._vfio_audio.get("state", "unknown")
        detail = (self._vfio_audio.get("detail") or "").strip()
        kind = self._vfio_audio.get("kind", "")

        if state == "detecting" and detail:
            return f"{detail} (detecting)"
        if state in ("unknown", "absent", "unavailable", "unsupported", "error"):
            return "No Audio"
        if kind == "bitstream" or "-> Stereo" in detail or "Muted" in detail:
            return detail or "Bitstream"

        m = re.search(r"(\d+) Hz (\d+) ch", detail)
        if m:
            khz = int(m.group(1)) / 1000.0
            channels = int(m.group(2))
            channel_label = {
                1: "Mono",
                2: "Stereo",
                6: "5.1",
                8: "7.1",
            }.get(channels, f"{channels} ch")
            return f"L-PCM {khz:g}kHz ({channel_label})"
        return "No Audio"

    def get_signal_info(self) -> Dict[str, object]:
        """Queries current signal status via v4l2-ctl (kernel mode) or cache."""
        if self.vfio_mode:
            return self.get_vfio_signal_info()

        info = {
            "locked": False,
            "resolution": "No Signal",
            "framerate": "",
            "pixelformat": "",
            "audio": "8 Channels @ 48 kHz"
        }
        if not os.path.exists(self.vdev):
            info["resolution"] = f"{self.vdev} not found"
            return info

        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "-d", self.vdev, "--query-dv-timings"],
                stderr=subprocess.DEVNULL, timeout=1.0
            ).decode()
            
            w_m = re.search(r"Active width:\s*(\d+)", out)
            h_m = re.search(r"Active height:\s*(\d+)", out)
            clk_m = re.search(r"Pixelclock:\s*(\d+)", out)
            tot_w = re.search(r"Total width:\s*(\d+)", out)
            tot_h = re.search(r"Total height:\s*(\d+)", out)
            il_m = re.search(r"interlaced", out, re.IGNORECASE)
            scan = "i" if il_m else "p"

            if w_m and h_m:
                w, h = int(w_m.group(1)), int(h_m.group(1))
                fps_str = "60 FPS"
                if clk_m and tot_w and tot_h:
                    clk = int(clk_m.group(1))
                    tw = int(tot_w.group(1))
                    th = int(tot_h.group(1))
                    if tw > 0 and th > 0:
                        fps_calc = clk / (tw * th)
                        if scan == "i":
                            fps_calc *= 2.0
                        if abs(fps_calc - round(fps_calc)) < 0.01:
                            fps_str = f"{int(round(fps_calc))} FPS"
                        else:
                            fps_str = f"{fps_calc:.2f} FPS"
                disp_w = 720 if (w == 1440 and (h == 480 or h == 576)) else w
                info["locked"] = True
                info["resolution"] = f"{disp_w}x{h}{scan}"
                info["framerate"] = fps_str
                info["chroma"] = self.chroma_label()
                info["hdr"] = self.hdr_label() or "SDR"
        except Exception:
            info["locked"] = False
            info["resolution"] = "No Signal"
            info["framerate"] = ""
            info["chroma"] = ""
            info["hdr"] = ""

        return info

    def chroma_label(self) -> str:
        """Reads active driver pixel format and returns friendly chroma description."""
        if self.vfio_mode:
            return (self._vfio_signal or {}).get("chroma", "")
        if not self.vdev or not os.path.exists(self.vdev):
            return ""
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "-d", self.vdev, "--get-fmt-video"],
                stderr=subprocess.DEVNULL, timeout=0.8
            ).decode()
            if "BGR3" in out or "RGB4" in out:
                return "RGB 4:4:4"
            if "NV12" in out:
                return "YUV 4:2:0"
            if "P010" in out:
                return "P010 4:2:0"
            if "YUYV" in out:
                return "YUV 4:2:2"
        except Exception:
            pass
        return "YUV 4:2:2"

    def hdr_label(self, fallback: bool = True) -> str:
        """SDR, HDR10 or HLG from the driver's cached metadata. No HDMI I2C."""
        if self.vfio_mode:
            return (self._vfio_signal or {}).get("hdr", "")
        if not self.vdev or not os.path.exists(self.vdev):
            return ""
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "-d", self.vdev, "-C", "hdr_mode"],
                stderr=subprocess.DEVNULL, timeout=0.8
            ).decode()
            for line in out.splitlines():
                if "hdr_mode" in line and ":" in line:
                    val = int(line.split(":")[-1].strip())
                    return {0: "SDR", 1: "HDR10", 2: "HDR10", 3: "HLG"}.get(val, "SDR")
        except Exception:
            pass
        if not fallback:
            return ""
        try:
            fmt_out = subprocess.check_output(
                ["v4l2-ctl", "-d", self.vdev, "--get-fmt-video"],
                stderr=subprocess.DEVNULL, timeout=0.8
            ).decode()
            if "SMPTE 2084" in fmt_out or "P010" in fmt_out:
                return "HDR10"
        except Exception:
            pass
        return ""

    def get_active_format(self) -> Tuple[Optional[str], Optional[str]]:
        """Reads active driver streaming resolution and framerate without touching HDMI I2C."""
        if self.vfio_mode:
            info = self.get_vfio_signal_info()
            if not info.get("locked"):
                return None, None
            return info.get("resolution"), info.get("framerate")
        if not self.vdev or not os.path.exists(self.vdev):
            return None, None
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "-d", self.vdev, "--get-dv-timings"],
                stderr=subprocess.DEVNULL, timeout=0.5
            ).decode()
            w_m = re.search(r"Active width:\s*(\d+)", out)
            h_m = re.search(r"Active height:\s*(\d+)", out)
            clk_m = re.search(r"Pixelclock:\s*(\d+)", out)
            tot_w = re.search(r"Total width:\s*(\d+)", out)
            tot_h = re.search(r"Total height:\s*(\d+)", out)
            il_m = re.search(r"interlaced", out, re.IGNORECASE)
            scan = "i" if il_m else "p"
            if w_m and h_m:
                w, h = int(w_m.group(1)), int(h_m.group(1))
                fps_str = None
                if clk_m and tot_w and tot_h:
                    clk = int(clk_m.group(1))
                    tw = int(tot_w.group(1))
                    th = int(tot_h.group(1))
                    if tw > 0 and th > 0:
                        fps_calc = clk / (tw * th)
                        if scan == "i":
                            fps_calc *= 2.0
                        if abs(fps_calc - round(fps_calc)) < 0.01:
                            fps_str = f"{int(round(fps_calc))} FPS"
                        else:
                            fps_str = f"{fps_calc:.2f} FPS"
                disp_w = 720 if (w == 1440 and (h == 480 or h == 576)) else w
                return f"{disp_w}x{h}{scan}", fps_str
        except Exception:
            pass

        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "-d", self.vdev, "--get-fmt-video"],
                stderr=subprocess.DEVNULL, timeout=0.5
            ).decode()
            m = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out)
            il_m = re.search(r"Field\s*:\s*(Interlaced|Alternate|Top|Bottom)", out, re.IGNORECASE)
            scan = "i" if il_m else "p"
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                disp_w = 720 if (w == 1440 and (h == 480 or h == 576)) else w
                if w > 0 and h > 0:
                    return f"{disp_w}x{h}{scan}", None
        except Exception:
            pass
        return None, None

    def get_active_resolution(self) -> Optional[str]:
        res, _ = self.get_active_format()
        return res

    def resync(self) -> bool:
        """Forces hardware HDMI HPD re-sync (kernel mode only)."""
        if self.vfio_mode:
            # The userspace engine handles hot-switching automatically.
            return False
        trigger_hpd_pulse()
        time.sleep(0.35)
        if not os.path.exists(self.vdev):
            return False
        try:
            subprocess.run(
                ["v4l2-ctl", "-d", self.vdev, "--set-dv-bt-timings", "query"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0
            )
            return True
        except Exception:
            return False

    def ctl(self, *args: str) -> Optional[str]:
        """Run gc570d-ctl and return its output (None when unavailable)."""
        if not self.ctl_bin or not os.access(self.ctl_bin, os.X_OK):
            return None
        try:
            return subprocess.check_output(
                [self.ctl_bin, *args], text=True,
                stderr=subprocess.DEVNULL, timeout=2.0)
        except Exception:
            return None

    def set_duo_input(self, port: int) -> bool:
        """Select HDMI 1 (0) or HDMI 2 (1) on the Live Gamer DUO."""
        port = 1 if int(port) else 0
        self.duo_input = port
        save_settings({"duo_input": port})
        if not self.duo_mode:
            return False
        if not self.is_running:
            return True
        return self.ctl("input", str(port + 1)) is not None

    def apply_duo_led(self, mode: int) -> bool:
        """Map an EverLive LED mode to the GC570D effect interface."""
        mode = int(mode)
        if mode == 0:
            effect = "off"
        elif mode == 8:
            effect = "colorful"
        elif mode == 9:
            effect = "breathing"
        else:
            effect = "static"
        return self.ctl("led", effect) is not None

    def duo_hotplug(self, port: int = 0) -> bool:
        """Pulse HPD on IT68051 (HDMI 1) or IT6802 (HDMI 2)."""
        node = "it68051_hpd" if int(port) == 0 else "it6802_hpd"
        return self.ctl("action", node, "1") is not None

    def duo_edid_policy(self, bridge: bool) -> bool:
        """Select internal bridge EDID (True) or downstream copy (False)."""
        node = ("splitter_windows_bridge_edid" if bridge
                else "splitter_windows_edid_copy")
        return self.ctl("action", node, "1") is not None

    def _start_vfio_duo(self, fullscreen: bool, vsync: bool,
                        streamer_mode: bool, v4l2_device: Optional[str],
                        headless: bool, aspect: Optional[str], osd: bool,
                        osd_pos: str, scale: str,
                        volume: float) -> bool:
        """Launch the GC570D (Live Gamer DUO) userspace engine."""
        if _vfio_streamer_running():
            print("[*] Userspace streamer is already active; attaching to live feed.")
            self.is_running = True
            return True

        if not (os.path.isfile(self.streamer_bin) and
                os.access(self.streamer_bin, os.X_OK)):
            print(f"[!] Userspace streamer not found: {self.streamer_bin}")
            print("    Build it with: make -C tools/opengc570d-userd")
            return False

        _stop_vfio_daemon()
        _raise_memlock_limit()

        cmd = [self.streamer_bin]
        if self.hw_info.get("pci"):
            cmd.append(self.hw_info["pci"])

        # Experimental lsfg-vk frame generation.  This is deliberately passed
        # in headless mode too: the engine keeps LSFG idle while the window is
        # closed and activates Vulkan + LSFG as soon as mode.set("gaming")
        # opens it, so a Studio -> Gaming switch needs no restart.  The V4L2
        # loopback keeps receiving the raw capture path independently.
        if framegen not in FRAMEGEN_KEYS:
            framegen = FRAMEGEN_ALIASES.get(str(framegen).lower(), "off")
        if framegen in FRAMEGEN_KEYS and framegen != "off":
            cmd.extend(["--framegen", framegen])
        # Flow/perf/latency are passed even in mode OFF so a later live toggle
        # (hotkey or IPC) starts from the Hub's saved values, not defaults.
        cmd.extend(["--fg-flow-scale",
                    f"{max(0.5, min(1.0, float(fg_flow_scale))):.2f}"])
        cmd.append("--fg-perf" if fg_perf_mode else "--no-fg-perf")
        cmd.append("--fg-low-latency" if fg_low_latency else "--no-fg-low-latency")

        if headless:
            cmd.append("--headless")
            # Studio/streamer mode publishes a clean OBS feed without OSD.
            cmd.append("--no-osd" if streamer_mode or not osd else "--osd")
        else:
            cmd.append("--sdl")
            if fullscreen:
                cmd.append("--fullscreen")
            cmd.append("--vsync" if vsync else "--no-vsync")
            if streamer_mode:
                cmd.append("--streamer-mode")
            else:
                cmd.append("--osd" if osd else "--no-osd")
        if osd_pos not in ("tr", "br", "bl", "tl"):
            osd_pos = "tr"
        cmd.extend(["--osd-pos", osd_pos])

        if aspect is None:
            aspect = getattr(self, "aspect", "auto")
        if aspect not in ASPECT_KEYS:
            aspect = "auto"
        self.aspect = aspect
        if aspect != "auto":
            cmd.extend(["--aspect", aspect])

        if scale not in SCALE_MODES:
            scale = "fit"
        cmd.extend(["--scale", scale])
        cmd.extend(["--volume", f"{volume:.2f}"])

        duo_input = load_settings().get("duo_input", 0)
        self.duo_input = 1 if duo_input == 1 else 0
        cmd.extend(["--port", str(self.duo_input + 1)])

        if not load_settings().get("duo_edid_bridge", True):
            cmd.append("--copy-edid")

        self.v4l2_device = v4l2_device
        self.v4l2_device_2 = None
        if v4l2_device:
            cmd.extend(["--v4l2", v4l2_device])
            if os.path.exists(V4L2_DUO_SECOND_DEVICE):
                self.v4l2_device_2 = V4L2_DUO_SECOND_DEVICE
                cmd.extend(["--v4l2-2", V4L2_DUO_SECOND_DEVICE])

        self.headless = bool(headless)
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            log = open(self._vfio_log_path, "wb")
        except OSError:
            log = subprocess.DEVNULL

        stdbuf = shutil.which("stdbuf")
        if stdbuf:
            cmd = [stdbuf, "-oL", "-eL"] + cmd

        try:
            self.mpv_proc = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        except Exception as e:
            print(f"[!] Failed to launch userspace streamer: {e}")
            self.headless = False
            self.v4l2_device = None
            self.v4l2_device_2 = None
            if log is not subprocess.DEVNULL:
                log.close()
            return False
        finally:
            if log is not subprocess.DEVNULL and not log.closed:
                log.close()

        self._vfio_log_pos = 0
        self._vfio_live = False
        self._vfio_saw_output = False
        self._duo_no_signal = False
        self._vfio_audio = {"state": "unknown", "detail": "", "kind": ""}
        self.is_running = True
        print(f"[*] Starting Userspace VFIO engine (DUO): {' '.join(cmd)}")

        time.sleep(0.25)
        if self.mpv_proc.poll() is not None:
            self._read_vfio_log()
            self.is_running = False
            self.mpv_proc = None
            self.headless = False
            self.v4l2_device = None
            self.v4l2_device_2 = None
            print(f"[!] Userspace streamer exited at startup; see {self._vfio_log_path}")
            return False

        # Apply the saved LED effect once the daemon owns BAR0.
        mode = load_settings().get("led_mode", 4)
        if isinstance(mode, int):
            self.apply_duo_led(mode)
        return True

    def _start_vfio(self, fullscreen: bool = False, vsync: bool = False,
                    streamer_mode: bool = False,
                    v4l2_device: Optional[str] = None,
                    headless: bool = False,
                    aspect: Optional[str] = None,
                    osd: bool = True,
                    osd_pos: str = "tr",
                    audio_tap: Optional[str] = None,
                    hdr: bool = True,
                    hdr_exposure: float = 0.0,
                    scale: str = "fit",
                    volume: float = 1.0,
                    framegen: str = "off",
                    fg_flow_scale: float = 1.0,
                    fg_perf_mode: bool = False,
                    fg_low_latency: bool = True) -> bool:
        """Launch the blob-free userspace VFIO streamer directly."""
        if self.duo_mode:
            return self._start_vfio_duo(
                fullscreen=fullscreen, vsync=vsync,
                streamer_mode=streamer_mode,
                v4l2_device=v4l2_device, headless=headless,
                aspect=aspect, osd=osd, osd_pos=osd_pos,
                scale=scale, volume=volume)

        if _vfio_streamer_running():
            print("[*] Userspace streamer is already active; attaching to live feed.")
            self.is_running = True
            return True

        if not (os.path.isfile(self.streamer_bin) and
                os.access(self.streamer_bin, os.X_OK)):
            print(f"[!] Userspace streamer not found: {self.streamer_bin}")
            print("    Build it with: make -C tools/opengc573-userd")
            return False

        # everlive_userd holds BAR0/VFIO in standby; release it first.
        _stop_vfio_daemon()
        _raise_memlock_limit()

        cmd = [self.streamer_bin]
        if self.hw_info.get("pci"):
            cmd.append(self.hw_info["pci"])

        # Keep the colour selected in the GUI while gaming instead of the
        # streamer's built-in cyan default.
        led_mode = load_settings().get("led_mode", 4)
        if led_mode == 8:
            cmd.extend(["--led", "wave"])
        elif led_mode == 9:
            cmd.extend(["--led", "rainbow"])
        elif isinstance(led_mode, int) and 0 <= led_mode <= 7:
            cmd.extend(["--led", led_hex_for_mode(led_mode)])
        else:
            cmd.extend(["--led", "#00ccff"])

        # Hardware EDID profile advertised to the console.
        edid_profile = load_settings().get("edid_profile", "default")
        edid_custom = load_settings().get("edid_custom_path", "")
        if edid_profile == "custom":
            if edid_custom and os.path.isfile(edid_custom):
                cmd.extend(["--edid", edid_custom])
            else:
                print("[!] Custom EDID file is missing; using the default profile.")
                cmd.extend(["--edid", "default"])
        elif edid_profile in EDID_PROFILE_KEYS:
            cmd.extend(["--edid", edid_profile])
        else:
            cmd.extend(["--edid", "default"])

        # Presentation options only matter for the interactive SDL2 window.
        if not headless:
            if fullscreen:
                cmd.append("--fullscreen")
            cmd.append("--vsync" if vsync else "--no-vsync")
            if streamer_mode:
                cmd.append("--streamer-mode")
            else:
                cmd.append("--osd" if osd else "--no-osd")
        if osd_pos not in ("tr", "br", "bl", "tl"):
            osd_pos = "tr"
        cmd.extend(["--osd-pos", osd_pos])

        if aspect is None:
            aspect = getattr(self, "aspect", "auto")
        if aspect not in ASPECT_KEYS:
            aspect = "auto"
        self.aspect = aspect
        if aspect != "auto":
            cmd.extend(["--aspect", aspect])

        if scale not in SCALE_MODES:
            scale = "fit"
        cmd.extend(["--scale", scale])
        cmd.extend(["--volume", f"{volume:.2f}"])

        if v4l2_device:
            cmd.extend(["--v4l2", v4l2_device])
        if audio_tap:
            cmd.extend(["--audio-tap", audio_tap])
        if not hdr:
            cmd.append("--no-hdr")
        elif abs(hdr_exposure) >= 0.05:
            cmd.extend(["--hdr-exposure", f"{hdr_exposure:.1f}"])
        if headless:
            cmd.append("--headless")

        self.headless = bool(headless)
        self.v4l2_device = v4l2_device
        self.v4l2_device_2 = None

        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            log = open(self._vfio_log_path, "wb")
        except OSError:
            log = subprocess.DEVNULL

        # The streamer's stdout carries the current signal; keep it line
        # buffered so standby/live status can be parsed without V4L2.
        stdbuf = shutil.which("stdbuf")
        if stdbuf:
            cmd = [stdbuf, "-oL", "-eL"] + cmd

        try:
            self.mpv_proc = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        except Exception as e:
            print(f"[!] Failed to launch userspace streamer: {e}")
            self.headless = False
            self.v4l2_device = None
            if log is not subprocess.DEVNULL:
                log.close()
            return False
        finally:
            if log is not subprocess.DEVNULL and not log.closed:
                log.close()

        self._vfio_log_pos = 0
        self._vfio_live = False
        self._vfio_saw_output = False
        self._duo_no_signal = False
        self._vfio_audio = {"state": "unknown", "detail": "", "kind": ""}
        self.is_running = True
        print(f"[*] Starting Userspace VFIO engine: {' '.join(cmd)}")

        # Catch immediate failures (missing VFIO permission, unbound card).
        time.sleep(0.25)
        if self.mpv_proc.poll() is not None:
            self._read_vfio_log()
            self.is_running = False
            self.mpv_proc = None
            self.headless = False
            self.v4l2_device = None
            print(f"[!] Userspace streamer exited at startup; see {self._vfio_log_path}")
            return False
        return True

    def start(self, preset_name: str = "Auto (HDMI Signal)", fullscreen: bool = False, volume: float = 1.0, vsync: bool = False, streamer_mode: bool = False, v4l2_device: Optional[str] = None, headless: bool = False, aspect: Optional[str] = None, osd: bool = True, osd_pos: str = "tr", audio_tap: Optional[str] = None, hdr: bool = True, hdr_exposure: float = 0.0, scale: str = "fit", framegen: str = "off", fg_flow_scale: float = 1.0, fg_perf_mode: bool = False, fg_low_latency: bool = True) -> bool:
        if self.is_running:
            return True

        if self.vfio_mode:
            if preset_name != "Auto (HDMI Signal)":
                print("[*] VFIO mode: the input mode comes from the HDMI "
                      "source and the selected EDID profile.")
            return self._start_vfio(fullscreen=fullscreen, vsync=vsync,
                                    streamer_mode=streamer_mode,
                                    v4l2_device=v4l2_device,
                                    headless=headless,
                                    aspect=aspect,
                                    osd=osd,
                                    osd_pos=osd_pos,
                                    audio_tap=audio_tap,
                                    hdr=hdr,
                                    hdr_exposure=hdr_exposure,
                                    scale=scale,
                                    volume=volume,
                                    framegen=framegen,
                                    fg_flow_scale=fg_flow_scale,
                                    fg_perf_mode=fg_perf_mode,
                                    fg_low_latency=fg_low_latency)

        if headless:
            print("[!] Headless/Studio mode requires the userspace VFIO engine.")
            return False
        if v4l2_device:
            print("[*] Note: --v4l2 is only used by the userspace VFIO engine.")

        self.vsync = vsync
        if not os.path.exists(self.vdev):
            print(f"[!] Error: Video device {self.vdev} does not exist.")
            return False

        preset = PRESETS.get(preset_name)
        width, height, fps = None, None, None

        if preset:
            width = preset["width"]
            height = preset["height"]
            fps = preset["fps"]
            fmt = preset["fmt"]
            # Apply fixed preset to V4L2 device
            try:
                subprocess.run(
                    ["v4l2-ctl", "-d", self.vdev, f"--set-fmt-video=width={width},height={height},pixelformat={fmt}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0
                )
            except Exception as e:
                print(f"[*] Note while setting format: {e}")
        else:
            # Auto (HDMI Signal): Synchronous v4l2 query only needed for mpv fallback.
            # Native everlive_display manages DV-Timings and Standby asynchronously without blocking GUI.
            script_dir = os.path.dirname(os.path.abspath(__file__))
            native_bin = os.path.join(script_dir, "everlive_display")
            if not os.path.isfile(native_bin):
                native_bin = os.path.join(script_dir, "gc573_display")
            if self.force_mpv or not (os.path.isfile(native_bin) and os.access(native_bin, os.X_OK)):
                try:
                    subprocess.run(
                        ["v4l2-ctl", "-d", self.vdev, "--set-dv-bt-timings", "query"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0
                    )
                except Exception:
                    pass

        # Prefer native high-performance everlive_display binary (< 2 ms display latency)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        native_bin = os.path.join(script_dir, "everlive_display")
        if not os.path.isfile(native_bin):
            native_bin = os.path.join(script_dir, "gc573_display")

        if not self.force_mpv and os.path.isfile(native_bin) and os.access(native_bin, os.X_OK):
            cmd = [native_bin, "-d", self.vdev, "-a", self.adev]
            if self.pw_node:
                cmd.extend(["--pw-node", self.pw_node])
            if fullscreen:
                cmd.append("-f")
            if vsync:
                cmd.append("--vsync")
            if streamer_mode:
                cmd.append("--streamer-mode")
            cmd.extend(["-v", f"{volume:.2f}"])
            print(f"[*] Starting native EverLive Direct-Renderer (Pure Native Wayland): {' '.join(cmd)}")
            try:
                self.mpv_proc = subprocess.Popen(cmd, start_new_session=True)
                self.is_running = True
                return True
            except Exception as e:
                print(f"[!] Error launching everlive_display ({e}), falling back to mpv...")

        # Fallback: optimized mpv command with zero-delay low-latency profile
        mpv_cmd = [
            "mpv",
            f"av://v4l2:{self.vdev}",
            "--title=EverLive - Live Gamer 4K",
            "--profile=low-latency",
            "--untimed",
            "--no-cache",
            "--no-correct-pts",
            "--demuxer-lavf-probe-info=no",
            "--demuxer-lavf-analyzeduration=0",
            "--demuxer-readahead-secs=0",
            "--demuxer-lavf-o=fflags=nobuffer+fastseek",
            "--demuxer-lavf-o-add=flags=low_delay",
            "--demuxer-lavf-o-add=probesize=32",
            "--video-latency-hacks=yes",
            "--interpolation=no",
            "--framedrop=vo",
            "--video-sync=display-resync" if vsync else "--video-sync=display-desync",
            "--opengl-glfinish=no",
            "--vd-lavc-threads=1",
            "--vd-lavc-fast=yes",
            "--vo=gpu",
            "--cursor-autohide=1000",
            "--no-terminal",
            "--input-default-bindings=yes",
        ]

        if fullscreen:
            mpv_cmd.append("--fullscreen")
        if streamer_mode:
            mpv_cmd.extend(["--no-osc", "--osd-level=0", "--cursor-autohide=always"])

        # Automatic HDR10 / HLG metadata bridging for mpv:
        # Since FFmpeg's libavdevice/v4l2 demuxer does not propagate V4L2 colorimetry,
        # detect HDR directly from V4L2 metadata and inject the exact tone-mapping pipeline.
        hdr_type = self.hdr_label()
        if hdr_type == "HDR10":
            mpv_cmd.append("--vf=setparams=range=limited:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc")
            print("[*] HDR10 (PQ BT.2020) signal detected: Auto-configured mpv GPU tone-mapping (zero CPU delay)")
        elif hdr_type == "HLG":
            mpv_cmd.append("--vf=setparams=range=limited:color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc")
            print("[*] HLG (BT.2020) signal detected: Auto-configured mpv HLG GPU tone-mapping (zero CPU delay)")

        # Anamorphic 3840x1080 scaling:
        # 3840x1080 is an anamorphic half-vertical 4K120 signal (e.g. RetroTINK 4K Pro)
        # that requires 2x vertical stretching to restore the intended 16:9 aspect ratio (3840x2160).
        active_fmt, _ = self.get_active_format()
        if active_fmt and "3840x1080" in active_fmt:
            mpv_cmd.append("--video-aspect-override=16:9")
            print("[*] Anamorphic 3840x1080 signal detected: Auto-configured 16:9 aspect ratio (2x vertical stretch) in mpv")

        try:
            mpv_env = os.environ.copy()
            self.mpv_proc = subprocess.Popen(mpv_cmd, env=mpv_env, start_new_session=True)
            self.is_running = True
            if self.adev and self.adev != "none":
                try:
                    mpv_pid = self.mpv_proc.pid if self.mpv_proc else os.getpid()
                    props = (
                        "{ application.name = \"EverLive\" application.icon-name = \"everlive\" "
                        "application.id = \"everlive\" application.process.binary = \"everlive\" "
                        f"application.process.id = {mpv_pid} "
                        "node.name = \"everlive-audio\" node.description = \"EverLive Game Audio\" "
                        "media.name = \"HDMI Game Audio\" media.role = \"Game\" }"
                    )
                    client_env = (
                        "PIPEWIRE_PROPS='application.name=\"EverLive\" application.icon-name=\"everlive\" application.id=\"everlive\"' "
                    )
                    if self.pw_node:
                        # Native PipeWire loopback with 60ms safe buffer matching everlive_display
                        cmd = (
                            f"{client_env}pw-loopback -i '{{ target.object = \"{self.pw_node}\" resample.disable = false }}' -l 60 "
                            f"-o '{{ node.virtual = false resample.disable = false resample.quality = 4 {props[1:-1]} }}' 2>/dev/null"
                        )
                    else:
                        plug_adev = f"plughw:{self.adev[3:]}" if self.adev.startswith("hw:") else self.adev
                        cmd = (
                            f"{{ arecord -D '{self.adev}' -t wav -f S16_LE -c 2 -r 48000 --period-time=20000 --buffer-time=80000 -q 2>/dev/null || "
                            f"   arecord -D '{plug_adev}' -t wav -f S16_LE -c 2 -r 48000 --period-time=20000 --buffer-time=80000 -q 2>/dev/null; }} | "
                            f"{client_env}pw-play --latency=40ms -P '{props}' - 2>/dev/null"
                        )
                    self.pwplay_proc = subprocess.Popen(cmd, shell=True, start_new_session=True)
                except Exception as ae:
                    print(f"[*] Note while starting audio stream: {ae}")
            return True
        except Exception as e:
            print(f"[!] Failed to launch mpv: {e}")
            self.stop()
            return False

    def stop(self):
        self.is_running = False
        if self.vfio_mode:
            self._read_vfio_log()
            self._vfio_live = False
            self._duo_no_signal = False
        if self.mpv_proc:
            try:
                pgid = os.getpgid(self.mpv_proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                try:
                    self.mpv_proc.terminate()
                except Exception:
                    pass
            try:
                # The userspace streamer performs the LED handoff and DMA
                # teardown on SIGTERM; give it a little more time.
                self.mpv_proc.wait(timeout=5.0 if self.vfio_mode else 1.0)
            except Exception:
                try:
                    pgid = os.getpgid(self.mpv_proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        self.mpv_proc.kill()
                    except Exception:
                        pass
            self.mpv_proc = None

        if self.pwplay_proc:
            try:
                pgid = os.getpgid(self.pwplay_proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                try:
                    self.pwplay_proc.terminate()
                except Exception:
                    pass
            try:
                self.pwplay_proc.wait(timeout=0.5)
            except Exception:
                try:
                    pgid = os.getpgid(self.pwplay_proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        self.pwplay_proc.kill()
                    except Exception:
                        pass
            self.pwplay_proc = None

        if self.arecord_proc:
            try:
                pgid = os.getpgid(self.arecord_proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                try:
                    self.arecord_proc.terminate()
                except Exception:
                    pass
            try:
                self.arecord_proc.wait(timeout=0.5)
            except Exception:
                try:
                    pgid = os.getpgid(self.arecord_proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        self.arecord_proc.kill()
                    except Exception:
                        pass
            self.arecord_proc = None

        self.headless = False
        self.v4l2_device = None
        self.v4l2_device_2 = None

    def poll(self) -> bool:
        """Returns True while the stream process is still alive."""
        if self.mpv_proc:
            if self.vfio_mode:
                self._read_vfio_log()
            if self.mpv_proc.poll() is not None:
                if self.vfio_mode:
                    self._read_vfio_log()
                self.stop()
                return False
            return True
        elif self.vfio_mode and _vfio_streamer_running():
            self._read_vfio_log()
            return True
        return False


if HAS_QT:
    SHM_NAME = "/dev/shm/gc570d_v4l2_shm"
    SHM_GC573_NAME = "/dev/shm/gc573_v4l2_shm"
    SHM_MAGIC = 0x47433537  # "GC57"
    SHM_HEADER_SIZE = 40
    SHM_PORT_HEADER_SIZE = 32
    SHM_FRAME_BYTES = 1920 * 1080 * 2
    SHM_PORT_SIZE = SHM_PORT_HEADER_SIZE + SHM_FRAME_BYTES

    # Base 256-byte CTA-861 EDID for custom modeline synthesis
    BASE_GC573_EDID = bytes.fromhex(
        "00ffffffffffff0006d8360000000000"
        "101c010380a05a78ea08a5a2574fa228"
        "0f5054250b00d1c08140818081008bc0"
        "9500b3003b8008e80030f2705a80b058"
        "8a006d552100001e0cdf80a070384040"
        "30403500202f2100001e000000fc0041"
        "56542047433537330a202020000000fd"
        "0032f01ede3c000a2020202020200111"
        "020344f15961605f5e5d101f5a3f0514"
        "0413121103020122212016150706230f"
        "0707834f000067030c001000383c67d8"
        "5dc401788003e200cfe305c000e20f03"
        "e30605016fc200a0a0a0555030203500"
        "55502100001e9ee80078a0a067500820"
        "980455502100001e565e00a0a0a02950"
        "30203500555021000018000000000006"
    )

    def parse_modeline(line: str) -> dict:
        """Parse an X11 / xrandr Modeline into its component timings and validate."""
        text = line.strip()
        if text.startswith("Modeline"):
            text = text[8:].strip()

        name = "custom"
        m_name = re.match(r'^"([^"]+)"\s*(.*)', text)
        if m_name:
            name = m_name.group(1).strip()
            text = m_name.group(2).strip()
        else:
            parts = text.split(None, 1)
            if len(parts) == 2 and not re.match(r'^[0-9.]+$', parts[0]):
                name = parts[0]
                text = parts[1]

        tokens = text.split()
        if len(tokens) < 9:
            raise ValueError("Modeline must contain at least 9 numeric timing values (Clock, 4 H, 4 V)")

        pclk_mhz = float(tokens[0])
        h_active = int(tokens[1])
        h_sync_start = int(tokens[2])
        h_sync_end = int(tokens[3])
        h_total = int(tokens[4])

        v_active = int(tokens[5])
        v_sync_start = int(tokens[6])
        v_sync_end = int(tokens[7])
        v_total = int(tokens[8])

        flags = [t.lower() for t in tokens[9:]]
        interlaced = "interlace" in flags or "interlaced" in flags
        hsync_pos = "+hsync" in flags or "-hsync" not in flags
        vsync_pos = "+vsync" in flags or "-vsync" not in flags

        if not (10.0 <= pclk_mhz <= 650.0):
            raise ValueError(f"Pixel clock {pclk_mhz:.2f} MHz outside supported HDMI range (10 - 650 MHz)")
        if not (320 <= h_active <= 4096):
            raise ValueError(f"Horizontal active {h_active} out of range (320 - 4096)")
        if not (200 <= v_active <= 2160):
            raise ValueError(f"Vertical active {v_active} out of range (200 - 2160)")
        if not (h_active < h_sync_start <= h_sync_end < h_total):
            raise ValueError("Horizontal timings must satisfy: active < sync_start <= sync_end < total")
        if not (v_active < v_sync_start <= v_sync_end < v_total):
            raise ValueError("Vertical timings must satisfy: active < sync_start <= sync_end < total")

        h_blank = h_total - h_active
        v_blank = v_total - v_active
        if h_blank > 4095 or v_blank > 4095:
            raise ValueError("Blanking intervals exceed 12-bit EDID field limits")

        h_sync_offset = h_sync_start - h_active
        h_sync_width = h_sync_end - h_sync_start
        v_sync_offset = v_sync_start - v_active
        v_sync_width = v_sync_end - v_sync_start

        if h_sync_offset > 1023 or h_sync_width > 1023:
            raise ValueError("Horizontal sync pulse or offset exceeds 10-bit EDID limits")
        if v_sync_offset > 63 or v_sync_width > 63:
            raise ValueError("Vertical sync pulse or offset exceeds 6-bit EDID limits")

        v_refresh = (pclk_mhz * 1e6) / (float(h_total) * float(v_total))

        return {
            "name": name,
            "pclk_mhz": pclk_mhz,
            "pclk_10khz": int(round(pclk_mhz * 100)),
            "h_active": h_active,
            "h_sync_start": h_sync_start,
            "h_sync_end": h_sync_end,
            "h_total": h_total,
            "h_blank": h_blank,
            "h_sync_offset": h_sync_offset,
            "h_sync_width": h_sync_width,
            "v_active": v_active,
            "v_sync_start": v_sync_start,
            "v_sync_end": v_sync_end,
            "v_total": v_total,
            "v_blank": v_blank,
            "v_sync_offset": v_sync_offset,
            "v_sync_width": v_sync_width,
            "interlaced": interlaced,
            "hsync_pos": hsync_pos,
            "vsync_pos": vsync_pos,
            "v_refresh": v_refresh,
        }

    def modeline_to_dtd(m: dict) -> bytes:
        """Convert parsed modeline timing parameters into an 18-byte EDID Detailed Timing Descriptor."""
        pclk = m["pclk_10khz"]
        ha = m["h_active"]
        hb = m["h_blank"]
        va = m["v_active"]
        vb = m["v_blank"]
        hso = m["h_sync_offset"]
        hsw = m["h_sync_width"]
        vso = m["v_sync_offset"]
        vsw = m["v_sync_width"]

        b0 = pclk & 0xFF
        b1 = (pclk >> 8) & 0xFF
        b2 = ha & 0xFF
        b3 = hb & 0xFF
        b4 = (((ha >> 8) & 0x0F) << 4) | ((hb >> 8) & 0x0F)
        b5 = va & 0xFF
        b6 = vb & 0xFF
        b7 = (((va >> 8) & 0x0F) << 4) | ((vb >> 8) & 0x0F)
        b8 = hso & 0xFF
        b9 = hsw & 0xFF
        b10 = ((vso & 0x0F) << 4) | (vsw & 0x0F)
        b11 = (((hso >> 8) & 0x03) << 6) | (((hsw >> 8) & 0x03) << 4) | (((vso >> 4) & 0x03) << 2) | ((vsw >> 4) & 0x03)
        b12 = 0x58  # Horizontal size in mm (e.g. 600 mm)
        b13 = 0x54  # Vertical size in mm (e.g. 340 mm)
        b14 = ((0x02 & 0x0F) << 4) | (0x01 & 0x0F)
        b15 = 0     # Border H
        b16 = 0     # Border V

        flags = 0x18  # Digital separate sync
        if m["interlaced"]:
            flags |= 0x80
        if m["vsync_pos"]:
            flags |= 0x04
        if m["hsync_pos"]:
            flags |= 0x02
        b17 = flags

        return bytes([b0, b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12, b13, b14, b15, b16, b17])

    def build_edid_from_modeline(m: dict, monitor_name: str = "") -> bytes:
        """Synthesize a complete, valid 256-byte CTA-861 EDID with custom Detailed Timing Descriptor."""
        edid = bytearray(BASE_GC573_EDID)
        dtd = modeline_to_dtd(m)

        # Place into Block 0 Detailed Timing Descriptor 1 (offset 54..71)
        edid[54:72] = dtd

        # Update Monitor Name Descriptor in Block 0 (offset 90..107)
        name = (monitor_name or m["name"] or "Custom").strip()[:13]
        name_bytes = name.encode("ascii", errors="replace")
        desc_name = bytearray(b"\x00\x00\x00\xfc\x00" + name_bytes)
        if len(desc_name) < 18:
            desc_name.append(0x0A)
            while len(desc_name) < 18:
                desc_name.append(0x20)
        edid[90:108] = desc_name[:18]

        # Recalculate Block 0 checksum
        edid[127] = (256 - (sum(edid[0:127]) % 256)) % 256

        # Update Block 1 CTA-861 detailed timing slot
        dtd_start = 128 + edid[130]
        if dtd_start + 18 <= 255:
            edid[dtd_start:dtd_start + 18] = dtd

        # Recalculate Block 1 checksum
        edid[255] = (256 - (sum(edid[128:255]) % 256)) % 256

        return bytes(edid)

    class CustomModelineDialog(QDialog):
        """Import and validate X11 / xrandr Modeline to generate a custom EDID profile."""
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Import Custom Modeline (EDID Generator)")
            self.resize(580, 440)
            self.setStyleSheet("""
                QDialog { background-color: #121620; color: #e6e9ef; }
                QLabel { color: #d0d5e0; }
                QTextEdit, QLineEdit {
                    background-color: #1a202c;
                    border: 1px solid #2d3748;
                    border-radius: 6px;
                    color: #ffffff;
                    padding: 6px;
                    font-family: monospace;
                }
                QPushButton {
                    background-color: #2b4f6e;
                    color: #ffffff;
                    border-radius: 6px;
                    padding: 8px 16px;
                    font-weight: bold;
                }
                QPushButton:hover { background-color: #3b668e; }
            """)

            layout = QVBoxLayout(self)

            lbl_desc = QLabel(
                "Paste an X11 / xrandr Modeline (e.g. from <code>cvt</code>, <code>gtf</code>, or custom timings). "
                "EverLive will parse and validate the timings, build an industry-standard 256-byte CTA-861 EDID, "
                "and save it to <code>~/.config/everlive/edids/custom/</code>."
            )
            lbl_desc.setWordWrap(True)
            layout.addWidget(lbl_desc)

            layout.addWidget(QLabel("Modeline String:"))
            self.txt_modeline = QTextEdit()
            self.txt_modeline.setPlaceholderText(
                'Modeline "1440x1080_120.00"  266.00  1440 1552 1704 1968  1080 1083 1088 1128 -hsync +vsync'
            )
            self.txt_modeline.setMaximumHeight(85)
            self.txt_modeline.textChanged.connect(self._on_text_changed)
            layout.addWidget(self.txt_modeline)

            h_name = QHBoxLayout()
            h_name.addWidget(QLabel("Profile / Output Name:"))
            self.edit_name = QLineEdit("custom_timing")
            h_name.addWidget(self.edit_name, 1)
            layout.addLayout(h_name)

            self.lbl_info = QLabel("Enter a valid modeline above to preview timings.")
            self.lbl_info.setStyleSheet("color: #8a94a6; font-size: 11px;")
            layout.addWidget(self.lbl_info)

            self.chk_activate = QCheckBox("Set as active EDID profile immediately")
            self.chk_activate.setChecked(True)
            layout.addWidget(self.chk_activate)

            layout.addStretch(1)

            h_btns = QHBoxLayout()
            self.btn_cancel = QPushButton("Cancel")
            self.btn_cancel.setStyleSheet("background-color: #2a303c;")
            self.btn_cancel.clicked.connect(self.reject)
            h_btns.addWidget(self.btn_cancel)

            self.btn_import = QPushButton("Import & Generate EDID")
            self.btn_import.setEnabled(False)
            self.btn_import.clicked.connect(self._on_import)
            h_btns.addWidget(self.btn_import)
            layout.addLayout(h_btns)

            self.generated_edid_path = ""
            self.profile_name = ""
            self._parsed = None

        def _on_text_changed(self):
            text = self.txt_modeline.toPlainText().strip()
            if not text:
                self.lbl_info.setText("Enter a valid modeline above to preview timings.")
                self.lbl_info.setStyleSheet("color: #8a94a6; font-size: 11px;")
                self.btn_import.setEnabled(False)
                self._parsed = None
                return

            try:
                m = parse_modeline(text)
                self._parsed = m
                msg = (
                    f"✓ Valid: {m['h_active']}×{m['v_active']}{'i' if m['interlaced'] else 'p'} @ "
                    f"{m['v_refresh']:.2f} Hz | Clock: {m['pclk_mhz']:.2f} MHz | "
                    f"HTotal: {m['h_total']}, VTotal: {m['v_total']}"
                )
                self.lbl_info.setText(msg)
                self.lbl_info.setStyleSheet("color: #4ade80; font-size: 11px; font-weight: bold;")
                clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', m["name"]).strip('_')
                if clean_name and (self.edit_name.text() in ("custom_timing", "", m["name"])):
                    self.edit_name.setText(clean_name)
                self.btn_import.setEnabled(True)
            except Exception as e:
                self._parsed = None
                self.lbl_info.setText(f"✗ Timing error: {str(e)}")
                self.lbl_info.setStyleSheet("color: #f87171; font-size: 11px;")
                self.btn_import.setEnabled(False)

        def _on_import(self):
            if not self._parsed:
                return
            raw_name = self.edit_name.text().strip() or self._parsed["name"]
            clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_name).strip('_')
            if not clean_name:
                clean_name = "custom_timing"

            dest_dir = os.path.expanduser("~/.config/everlive/edids/custom")
            os.makedirs(dest_dir, exist_ok=True)
            dest_file = os.path.join(dest_dir, f"{clean_name}.bin")

            try:
                edid_bytes = build_edid_from_modeline(self._parsed, clean_name)
                with open(dest_file, "wb") as f:
                    f.write(edid_bytes)
                self.generated_edid_path = dest_file
                self.profile_name = clean_name
                self.accept()
            except Exception as ex:
                QMessageBox.critical(self, "EDID Generation Failed", f"Failed to save EDID: {ex}")

    class _Py_buffer(ctypes.Structure):
        _fields_ = [
            ("buf", ctypes.c_void_p),
            ("obj", ctypes.c_void_p),
            ("len", ctypes.c_ssize_t),
            ("itemsize", ctypes.c_ssize_t),
            ("readonly", ctypes.c_int),
            ("ndim", ctypes.c_int),
            ("format", ctypes.c_char_p),
            ("shape", ctypes.POINTER(ctypes.c_ssize_t)),
            ("strides", ctypes.POINTER(ctypes.c_ssize_t)),
            ("suboffsets", ctypes.POINTER(ctypes.c_ssize_t)),
            ("internal", ctypes.c_void_p),
        ]

    def _get_buffer_addr(buf) -> int:
        try:
            pybuf = _Py_buffer()
            if ctypes.pythonapi.PyObject_GetBuffer(ctypes.py_object(buf), ctypes.byref(pybuf), 0) == 0:
                addr = pybuf.buf
                ctypes.pythonapi.PyBuffer_Release(ctypes.byref(pybuf))
                return addr or 0
        except Exception:
            pass
        return 0

    _lib_core = None
    try:
        _core_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "libeverlive-core", "libeverlive_core.so")
        if os.path.isfile(_core_path):
            _lib_core = ctypes.CDLL(_core_path)
            _lib_core.everlive_yuy2_to_rgb.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            _lib_core.everlive_yuy2_to_rgb_scaled.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    except Exception:
        _lib_core = None

    class EmbeddedVideoCanvas(QWidget):
        """Zero-latency embedded live video canvas for EverLive Console Hub.

        Reads frames directly from userspace shared memory (/dev/shm/gc570d_v4l2_shm),
        converts via SIMD-accelerated C or NumPy fixed-point, renders with aspect ratio
        correction and letterboxing, and supports 1-click borderless fullscreen.
        """
        fullscreen_requested = Signal()
        toggle_play_requested = Signal()
        port_requested = Signal(int)

        def __init__(self, parent=None, duo_mode: bool = False):
            super().__init__(parent)
            self.duo_mode = duo_mode
            self.port = 0
            self.aspect = "auto"
            self.scale_mode = "fit"

            self.is_streaming = False
            self.has_signal = False
            self.other_port_has_signal = None
            self.current_width = 1920
            self.current_height = 1080
            self.signal_desc = "Standby"

            self._current_qimage = None
            self._rgb_buffer = None
            self._last_seq = 0
            self._shm_file = None
            self._shm_mmap = None
            self._is_gc573_shm = False
            self._toast_message = ""
            self._toast_start = 0.0
            self._toast_duration = 3.0
            self._prev_has_signal = False
            self._hover_fs = False
            self._fs_btn_rect = QRect()

            self.setMouseTracking(True)
            self.setCursor(Qt.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.setMinimumHeight(400)
            self.setMinimumWidth(712)
            self.setStyleSheet("background-color: #080a10; border-radius: 8px;")

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.poll_frame)
            self.timer.start(16)

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            return max(400, int(round(width * 9.0 / 16.0)))

        def sizeHint(self) -> QSize:
            return QSize(720, 405)

        def minimumSizeHint(self) -> QSize:
            return QSize(712, 400)

        def set_port(self, port: int):
            self.port = 1 if port == 1 else 0
            self._last_seq = 0
            self.update()

        def set_aspect(self, aspect: str):
            self.aspect = aspect or "auto"
            self.update()

        def set_scale_mode(self, scale: str):
            self.scale_mode = scale or "fit"
            self.update()

        def show_toast(self, message: str, duration: float = 3.0):
            """Show a semi-transparent floating OSD toast pill on the canvas."""
            self._toast_message = str(message)
            self._toast_start = time.monotonic()
            self._toast_duration = max(0.5, float(duration))
            self.update()

        def poll_frame(self):
            target_shm = SHM_GC573_NAME if (not self.duo_mode and os.path.exists(SHM_GC573_NAME)) else SHM_NAME
            if self._shm_mmap is None:
                if not os.path.exists(target_shm):
                    if self.is_streaming:
                        self.is_streaming = False
                        self.has_signal = False
                        self._current_qimage = None
                        if self._prev_has_signal:
                            self._prev_has_signal = False
                            self.show_toast("HDMI Signal: Disconnected")
                        self.update()
                    return
                try:
                    self._shm_file = open(target_shm, "rb")
                    self._shm_mmap = mmap.mmap(
                        self._shm_file.fileno(), 0, prot=mmap.PROT_READ)
                    self._is_gc573_shm = (target_shm == SHM_GC573_NAME)
                except Exception:
                    self._shm_file = None
                    self._shm_mmap = None
                    return

            try:
                magic = struct.unpack_from("<I", self._shm_mmap, 0)[0]
                if magic != SHM_MAGIC:
                    return

                if self._is_gc573_shm:
                    port_off = 40
                    seq = struct.unpack_from("<Q", self._shm_mmap, port_off)[0]
                    if seq & 1 != 0:
                        return
                    if seq == self._last_seq and self._current_qimage is not None:
                        return
                    width, height, no_signal, streaming, fps_num, fps_den = struct.unpack_from(
                        "<IIIIII", self._shm_mmap, port_off + 16)
                    yuy2_off = port_off + 40
                else:
                    # In DUO mode, monitor the alternate port for active HDMI signal
                    if self.duo_mode:
                        other_port = 1 if self.port == 0 else 0
                        other_off = SHM_HEADER_SIZE + other_port * SHM_PORT_SIZE
                        other_w, other_h, other_ns, other_st = struct.unpack_from(
                            "<IIII", self._shm_mmap, other_off + 16)
                        if other_st != 0 and other_ns == 0 and other_w > 0:
                            self.other_port_has_signal = other_port
                        else:
                            self.other_port_has_signal = None

                    port_off = SHM_HEADER_SIZE + self.port * SHM_PORT_SIZE
                    seq = struct.unpack_from("<Q", self._shm_mmap, port_off)[0]
                    if seq & 1 != 0:
                        return
                    if seq == self._last_seq and self._current_qimage is not None:
                        return

                    width, height, no_signal, streaming = struct.unpack_from(
                        "<IIII", self._shm_mmap, port_off + 16)
                    yuy2_off = port_off + 32

                if streaming == 0:
                    if self.is_streaming:
                        self.is_streaming = False
                        self.has_signal = False
                        self._current_qimage = None
                        if self._prev_has_signal:
                            self._prev_has_signal = False
                            self.show_toast("HDMI Signal: Disconnected")
                        self.update()
                    return

                self.is_streaming = True
                if no_signal != 0 or width == 0 or height == 0:
                    if self.has_signal or self._current_qimage is not None:
                        self.has_signal = False
                        self._current_qimage = None
                        self.signal_desc = f"No HDMI Signal (Port {self.port + 1})" if self.duo_mode else "No HDMI Signal"
                        if self._prev_has_signal:
                            self._prev_has_signal = False
                            self.show_toast("HDMI Signal: Disconnected")
                        self.update()
                    return

                self.has_signal = True
                self.current_width = width
                self.current_height = height
                self.signal_desc = f"{width}×{height} @ 60 Hz"
                if not self._prev_has_signal:
                    self._prev_has_signal = True
                    self.show_toast(f"HDMI Signal: {self.signal_desc} (Locked)")

                payload_len = width * height * 2
                yuy2_slice = memoryview(self._shm_mmap)[yuy2_off : yuy2_off + payload_len]

                post_seq = struct.unpack_from("<Q", self._shm_mmap, port_off)[0]
                if post_seq != seq:
                    return

                rgb_len = width * height * 3
                if self._rgb_buffer is None or len(self._rgb_buffer) != rgb_len:
                    self._rgb_buffer = bytearray(rgb_len)

                yuy2_addr = _get_buffer_addr(yuy2_slice)
                dst_addr = _get_buffer_addr(self._rgb_buffer)

                if _lib_core is not None and yuy2_addr and dst_addr:
                    _lib_core.everlive_yuy2_to_rgb(yuy2_addr, dst_addr, width, height)
                    qimg = QImage(self._rgb_buffer, width, height, width * 3, QImage.Format_RGB888).copy()
                elif HAS_NUMPY:
                    raw = np.frombuffer(yuy2_slice, dtype=np.uint8).reshape((height, width, 2))
                    y = raw[:, :, 0].astype(np.int32)
                    u = np.repeat(raw[:, 0::2, 1], 2, axis=1).astype(np.int32) - 128
                    v = np.repeat(raw[:, 1::2, 1], 2, axis=1).astype(np.int32) - 128
                    r = np.clip(y + ((359 * v) >> 8), 0, 255).astype(np.uint8)
                    g = np.clip(y - ((88 * u + 183 * v) >> 8), 0, 255).astype(np.uint8)
                    b = np.clip(y + ((454 * u) >> 8), 0, 255).astype(np.uint8)
                    rgb_arr = np.ascontiguousarray(np.dstack((r, g, b)))
                    qimg = QImage(rgb_arr.data, width, height, width * 3, QImage.Format_RGB888).copy()
                elif _lib_core is not None and dst_addr:
                    src_copy = bytearray(yuy2_slice)
                    src_addr = _get_buffer_addr(src_copy)
                    if src_addr:
                        _lib_core.everlive_yuy2_to_rgb(src_addr, dst_addr, width, height)
                        qimg = QImage(self._rgb_buffer, width, height, width * 3, QImage.Format_RGB888).copy()
                    else:
                        return
                else:
                    return

                self._last_seq = seq
                self._current_qimage = qimg
                self.update()
            except Exception as e:
                if not getattr(self, "_logged_poll_err", False):
                    print(f"[EmbeddedVideoCanvas] poll_frame error: {e}")
                    self._logged_poll_err = True
                if self._shm_mmap:
                    try: self._shm_mmap.close()
                    except Exception: pass
                    self._shm_mmap = None
                if self._shm_file:
                    try: self._shm_file.close()
                    except Exception: pass
                    self._shm_file = None

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)

            w = self.width()
            h = self.height()
            # Canvas letterbox / background
            painter.fillRect(0, 0, w, h, QColor("#0b0d13"))

            # Calculate strict 16:9 embed geometry
            embed_aspect = 16.0 / 9.0
            viewport_aspect = float(w) / float(h if h > 0 else 1)
            if viewport_aspect > embed_aspect:
                embed_h = h
                embed_w = int(round(embed_h * embed_aspect))
                embed_x = (w - embed_w) // 2
                embed_y = 0
            else:
                embed_w = w
                embed_h = int(round(embed_w / embed_aspect))
                embed_x = 0
                embed_y = (h - embed_h) // 2
            embed_rect = QRect(embed_x, embed_y, embed_w, embed_h)

            # Draw 16:9 enclosure screen
            painter.fillRect(embed_rect, QColor("#080a10"))
            painter.setPen(QPen(QColor("#182030"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(embed_rect.adjusted(0, 0, -1, -1), 8, 8)

            if (self._current_qimage and not self._current_qimage.isNull() and
                    self.is_streaming and self.has_signal):
                img_w = self._current_qimage.width()
                img_h = self._current_qimage.height()

                # Determine target aspect ratio
                if self.aspect == "4:3":
                    target_ratio = 4.0 / 3.0
                elif self.aspect == "16:9" or self.aspect == "stretch":
                    target_ratio = 16.0 / 9.0
                else:  # auto
                    if img_h > 0 and (abs(float(img_w) / img_h - 4.0 / 3.0) < 0.08 or
                                      (img_w in (640, 720) and img_h in (480, 576))):
                        target_ratio = 4.0 / 3.0
                    else:
                        target_ratio = 16.0 / 9.0

                if self.scale_mode == "integer":
                    mult = max(1, min(embed_w // img_w, embed_h // img_h)) if (img_w > 0 and img_h > 0) else 1
                    dw = img_w * mult
                    dh = img_h * mult
                    dx = embed_x + (embed_w - dw) // 2
                    dy = embed_y + (embed_h - dh) // 2
                    target_rect = QRect(dx, dy, dw, dh)
                else:
                    if abs(target_ratio - (4.0 / 3.0)) < 0.01:
                        # 4:3 content displayed centered inside 16:9 embed with symmetric pillarboxing
                        dh = embed_h
                        dw = int(round(dh * (4.0 / 3.0)))
                        dx = embed_x + (embed_w - dw) // 2
                        dy = embed_y
                        target_rect = QRect(dx, dy, dw, dh)
                    else:
                        # 16:9 fills the embed exactly
                        target_rect = embed_rect

                painter.drawImage(target_rect, self._current_qimage)

                # Top-left signal badge inside 16:9 embed
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(15, 20, 30, 210))
                painter.drawRoundedRect(embed_x + 12, embed_y + 12, 190, 28, 6, 6)
                painter.setPen(QColor("#7fd1ad"))
                painter.setFont(QFont("sans-serif", 9, QFont.Bold))
                port_name = f"HDMI {self.port + 1}" if self.duo_mode else "HDMI Live"
                painter.drawText(embed_x + 22, embed_y + 30, f"● {port_name} · {self.signal_desc}")
            else:
                # Standby / No signal screen inside 16:9 embed
                painter.setPen(QColor("#5c6880"))
                painter.setFont(QFont("sans-serif", 32))
                painter.drawText(QRect(embed_x, embed_y + embed_h // 2 - 68, embed_w, 48), Qt.AlignCenter, "🎮")

                if self.is_streaming and not self.has_signal:
                    if self.other_port_has_signal is not None:
                        title = f"No Signal on HDMI {self.port + 1} · HDMI {self.other_port_has_signal + 1} Active"
                        subtitle = f"Active feed on HDMI {self.other_port_has_signal + 1} — Click here to switch"
                    else:
                        title = f"No HDMI Signal on HDMI {self.port + 1}" if self.duo_mode else "No HDMI Signal"
                        subtitle = "Power on console or connect HDMI source to display feed"
                else:
                    title = "EverLive Live Console Display"
                    subtitle = "Waiting for HDMI input signal..."

                painter.setPen(QColor("#e6e9ef"))
                painter.setFont(QFont("sans-serif", 13, QFont.Bold))
                painter.drawText(QRect(embed_x, embed_y + embed_h // 2 - 14, embed_w, 24), Qt.AlignCenter, title)

                painter.setPen(QColor("#8a94a6" if self.other_port_has_signal is None else "#7fd1ad"))
                painter.setFont(QFont("sans-serif", 10, QFont.Bold if self.other_port_has_signal is not None else QFont.Normal))
                painter.drawText(QRect(embed_x, embed_y + embed_h // 2 + 14, embed_w, 20), Qt.AlignCenter, subtitle)

            # Top-right hover button inside 16:9 embed
            btn_w, btn_h = 135, 28
            btn_x = embed_x + embed_w - btn_w - 14
            btn_y = embed_y + 12
            self._fs_btn_rect = QRect(btn_x, btn_y, btn_w, btn_h)

            btn_bg = QColor(43, 79, 110, 230) if self._hover_fs else QColor(26, 32, 48, 195)
            btn_border = QColor(70, 88, 122) if self._hover_fs else QColor(42, 50, 66)
            painter.setPen(QPen(btn_border, 1))
            painter.setBrush(btn_bg)
            painter.drawRoundedRect(self._fs_btn_rect, 6, 6)

            painter.setPen(QColor("#ffffff" if self._hover_fs else "#d7dde8"))
            painter.setFont(QFont("sans-serif", 9, QFont.Bold))
            painter.drawText(self._fs_btn_rect, Qt.AlignCenter, "⛶ Fullscreen (F)")

            # Floating toast notification pill inside 16:9 embed (bottom-center)
            if self._toast_message:
                elapsed = time.monotonic() - self._toast_start
                if elapsed < self._toast_duration:
                    if elapsed < 0.25:
                        alpha = int((elapsed / 0.25) * 235)
                    elif elapsed > self._toast_duration - 0.35:
                        alpha = int(((self._toast_duration - elapsed) / 0.35) * 235)
                    else:
                        alpha = 235
                    alpha = max(0, min(235, alpha))

                    font = QFont("sans-serif", 10, QFont.Bold)
                    fm = QFontMetrics(font)
                    text_w = fm.horizontalAdvance(self._toast_message)
                    pill_w = text_w + 36
                    pill_h = 32
                    pill_x = embed_x + (embed_w - pill_w) // 2
                    pill_y = embed_y + embed_h - pill_h - 20
                    pill_rect = QRect(pill_x, pill_y, pill_w, pill_h)

                    painter.setPen(QPen(QColor(64, 160, 255, alpha), 1))
                    painter.setBrush(QColor(14, 20, 32, alpha))
                    painter.drawRoundedRect(pill_rect, 16, 16)

                    painter.setPen(QColor(240, 245, 255, alpha))
                    painter.setFont(font)
                    painter.drawText(pill_rect, Qt.AlignCenter, self._toast_message)

        def mouseMoveEvent(self, event):
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            hover = self._fs_btn_rect.contains(pos)
            if hover != self._hover_fs:
                self._hover_fs = hover
                self.update()

        def mousePressEvent(self, event):
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if self._fs_btn_rect.contains(pos):
                self.fullscreen_requested.emit()
            elif self.is_streaming and not self.has_signal and self.other_port_has_signal is not None:
                self.set_port(self.other_port_has_signal)
                self.port_requested.emit(self.other_port_has_signal)
            elif not self.is_streaming:
                self.toggle_play_requested.emit()

        def mouseDoubleClickEvent(self, event):
            self.fullscreen_requested.emit()

    class FullscreenVideoWindow(QWidget):
        """Borderless edge-to-edge fullscreen presentation window."""
        def __init__(self, canvas: EmbeddedVideoCanvas, main_window):
            super().__init__()
            self.canvas = canvas
            self.main_window = main_window
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
            self.setStyleSheet("background-color: #000000;")
            self.setWindowTitle("EverLive Fullscreen")

            self.cursor_timer = QTimer(self)
            self.cursor_timer.setSingleShot(True)
            self.cursor_timer.timeout.connect(self._hide_osd)

            self.osd_visible = True
            self.setMouseTracking(True)

            self.osd_bar = QFrame(self)
            self.osd_bar.setStyleSheet("""
                QFrame {
                    background-color: rgba(14, 19, 29, 220);
                    border: 1px solid #2a3448;
                    border-radius: 8px;
                }
            """)
            h_osd = QHBoxLayout(self.osd_bar)
            h_osd.setContentsMargins(14, 6, 14, 6)
            h_osd.setSpacing(10)

            self.lbl_info = QLabel("HDMI Feed")
            self.lbl_info.setStyleSheet(
                "color: #7fd1ad; font-weight: bold; font-size: 12px; border: none; background: transparent;")
            h_osd.addWidget(self.lbl_info)

            h_osd.addStretch(1)

            btn_aspect = QPushButton("Aspect")
            btn_aspect.setStyleSheet(
                "background: #1a2030; color: #d7dde8; border: 1px solid #34425a; padding: 4px 10px; border-radius: 4px;")
            btn_aspect.clicked.connect(self._cycle_aspect)
            h_osd.addWidget(btn_aspect)

            btn_exit = QPushButton("✕ Exit Fullscreen (Esc)")
            btn_exit.setStyleSheet(
                "background: #2b4f6e; color: #ffffff; border: 1px solid #38658c; padding: 4px 12px; font-weight: bold; border-radius: 4px;")
            btn_exit.clicked.connect(self.close)
            h_osd.addWidget(btn_exit)

            self.rep_timer = QTimer(self)
            self.rep_timer.timeout.connect(self.update)
            self.rep_timer.start(16)

        def _cycle_aspect(self):
            order = ["auto", "16:9", "4:3", "stretch"]
            cur = getattr(self.canvas, "aspect", "auto")
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
            self.canvas.set_aspect(nxt)
            if hasattr(self.main_window, "combo_aspect"):
                idx = self.main_window.combo_aspect.findData(nxt)
                if idx >= 0:
                    self.main_window.combo_aspect.blockSignals(True)
                    self.main_window.combo_aspect.setCurrentIndex(idx)
                    self.main_window.combo_aspect.blockSignals(False)
            self.update()

        def _hide_osd(self):
            self.osd_visible = False
            self.osd_bar.setVisible(False)
            self.setCursor(Qt.BlankCursor)

        def mouseMoveEvent(self, event):
            if not self.osd_visible:
                self.osd_visible = True
                self.osd_bar.setVisible(True)
                self.unsetCursor()
            self.cursor_timer.start(2500)

        def keyPressEvent(self, event):
            key = event.key()
            if key in (Qt.Key_Escape, Qt.Key_F, Qt.Key_F11, Qt.Key_Q):
                self.close()
            elif key == Qt.Key_A:
                self._cycle_aspect()

        def mouseDoubleClickEvent(self, event):
            self.close()

        def resizeEvent(self, event):
            w = self.width()
            h = self.height()
            bar_w = min(560, w - 40)
            self.osd_bar.setGeometry((w - bar_w) // 2, h - 56, bar_w, 42)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)

            w = self.width()
            h = self.height()
            painter.fillRect(0, 0, w, h, Qt.black)

            qimg = self.canvas._current_qimage
            if (qimg and not qimg.isNull() and
                    self.canvas.is_streaming and self.canvas.has_signal):
                img_w = qimg.width()
                img_h = qimg.height()
                aspect = getattr(self.canvas, "aspect", "auto")
                scale_mode = getattr(self.canvas, "scale_mode", "fit")

                if aspect == "4:3":
                    target_ratio = 4.0 / 3.0
                elif aspect == "16:9":
                    target_ratio = 16.0 / 9.0
                elif aspect == "stretch":
                    target_ratio = float(w) / float(h if h > 0 else 1)
                else:
                    target_ratio = float(img_w) / float(img_h if img_h > 0 else 1)

                canvas_ratio = float(w) / float(h if h > 0 else 1)
                if scale_mode == "integer":
                    mult = max(1, min(w // img_w, h // img_h)) if (img_w > 0 and img_h > 0) else 1
                    dw = img_w * mult
                    dh = img_h * mult
                    dx = (w - dw) // 2
                    dy = (h - dh) // 2
                else:
                    if canvas_ratio > target_ratio:
                        dh = h
                        dw = int(dh * target_ratio)
                        dx = (w - dw) // 2
                        dy = 0
                    else:
                        dw = w
                        dh = int(dw / target_ratio)
                        dx = 0
                        dy = (h - dh) // 2
                painter.drawImage(QRect(dx, dy, dw, dh), qimg)
                self.lbl_info.setText(
                    f"{self.canvas.signal_desc} · Aspect: {aspect.upper()}")
            else:
                painter.setPen(QColor("#7fd1ad"))
                painter.setFont(QFont("sans-serif", 16, QFont.Bold))
                painter.drawText(
                    QRect(0, 0, w, h), Qt.AlignCenter, "Waiting for HDMI Stream...")

    class MainWindow(QMainWindow):
        def __init__(self, streamer: GameStreamer, vsync: bool = True, streamer_mode: bool = False,
                     headless_start: bool = False, v4l2_device: Optional[str] = None):
            super().__init__()
            self.streamer = streamer
            self.vfio_mode = streamer.vfio_mode
            self.duo_mode = getattr(streamer, "duo_mode", False)
            self.model = getattr(streamer, "model", MODEL_GC573)
            self.enable_vsync = vsync
            self.muted = False
            self.saved_cfg = load_settings()
            self.is_advanced_mode = (self.saved_cfg.get("ui_mode") in ("advanced", "pro"))
            self._headless_start = headless_start
            self._v4l2_device = v4l2_device
            # Remote control: set once we attach to a streamer we did not
            # start ourselves (it owns the card, we only observe it).
            self._remote = False
            self._remote_status: Dict = {}
            self._ipc = None
            self._ipc_timer = None
            self._ipc_shutdown = False
            self._vfio_notice_until = 0.0
            self._vfio_volume_applies = 0
            self._launch_mode: Optional[str] = None
            self._last_launch_mode = "gaming"
            # Target mode of an in-flight gaming <-> studio handover.
            self._switch_pending: Optional[str] = None
            self._handoff_tries = 0
            if streamer_mode:
                self.saved_cfg["streamer_mode"] = True
            if not self.vfio_mode and self.saved_cfg.get("engine", "native") == "mpv":
                self.streamer.force_mpv = True

            self._fullscreen_window = None
            self.init_ui()
            QShortcut(QKeySequence("F"), self, self.toggle_fullscreen)
            QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)

            if v4l2_device:
                self.chk_v4l2.setChecked(True)
                if isinstance(v4l2_device, str) and v4l2_device != V4L2_DEFAULT_DEVICE:
                    # The compact layout has no path field; surface a custom
                    # node through the tooltip instead of a phantom widget.
                    self.chk_v4l2.setToolTip(
                        f"Virtual camera output to {v4l2_device} "
                        "(custom path from the command line).")

            # Signal monitor timer (1s polling)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.update_status)
            self.timer.start(1000)

            # Remote channel to a streamer that already owns the device.
            # QSocketNotifier keeps this on the GUI thread: no worker threads
            # and no cross-thread signals.
            self._ipc = EverLiveIpcClient(self)
            self._ipc.connectedChanged.connect(self._on_remote_connection)
            self._ipc.statusReceived.connect(self._on_remote_status)
            self._ipc.errorRaised.connect(lambda msg: print(f"[IPC] {msg}"))
            self._ipc_timer = QTimer(self)
            self._ipc_timer.setInterval(1000)
            self._ipc_timer.timeout.connect(self._poll_remote_status)

            # Auto-attach to already running capture engine or start automatically on launch
            if _vfio_streamer_running():
                self.streamer.is_running = True
                self._launch_mode = "gaming"
                self._vfio_live = True
                print("[*] Userspace capture engine already active; attaching live feed.")
            elif headless_start:
                QTimer.singleShot(150, self.start_studio)
            elif self.saved_cfg.get("autostart", True) or getattr(streamer, "_cli_autostart", False):
                QTimer.singleShot(150, lambda: self.start_gaming_screen(detached=False))

            QTimer.singleShot(300, self, self._ipc_try_attach)

        def init_ui(self):
            self.setWindowTitle("EverLive - Console Hub")
            # Compact console-hub layout: sized so the cards below never
            # squash on Live Gamer DUO (with secondary action bar) or GC573.
            self.setMinimumSize(880, 640)
            self.resize(920, 940 if self.is_advanced_mode else 680)

            # Application & Window Icon
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "everlive.png")
            if os.path.isfile(icon_path):
                self.setWindowIcon(QIcon(icon_path))

            # EverLive Clean Dark Studio Theme
            self.setStyleSheet("""
                QMainWindow {
                    background-color: #0b0d13;
                }
                QFrame#cardFrame {
                    background-color: #12151f;
                    border: 1px solid #1f2537;
                    border-radius: 8px;
                    padding: 14px;
                }
                QFrame#bottomBar {
                    background-color: #080a0e;
                    border-top: 1px solid #171b26;
                    padding: 5px 12px;
                }
                QLabel {
                    color: #e2e8f0;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                }
                QLabel#logoTitle {
                    color: #ffffff;
                    font-size: 15px;
                    font-weight: 700;
                    letter-spacing: 0.5px;
                }
                QLabel#logoVersion {
                    color: #64748b;
                    font-size: 11px;
                    font-weight: 500;
                }
                QLabel#badgeModel {
                    color: #94a3b8;
                    background-color: #1a202c;
                    border: 1px solid #2d3748;
                    border-radius: 4px;
                    padding: 2px 7px;
                    font-size: 10px;
                    font-weight: 600;
                    letter-spacing: 0.5px;
                }
                QLabel#pillLive {
                    border-radius: 12px;
                    padding: 4px 12px;
                    font-size: 11px;
                    font-weight: 600;
                }
                QLabel#sectionTitle {
                    color: #64748b;
                    font-size: 11px;
                    font-weight: 700;
                    letter-spacing: 0.8px;
                }
                QLabel#heroSignal {
                    color: #ffffff;
                    font-size: 18px;
                    font-weight: 700;
                    padding-bottom: 3px;
                    min-height: 26px;
                }
                QLabel#heroMeta {
                    color: #94a3b8;
                    font-size: 12px;
                    padding-bottom: 2px;
                    min-height: 18px;
                }
                QLabel#engineBadge {
                    background-color: #0d2318;
                    color: #34d399;
                    border: 1px solid #164e32;
                    border-radius: 4px;
                    padding: 3px 8px;
                    font-size: 11px;
                    font-weight: 600;
                }
                QComboBox {
                    background-color: #171b26;
                    color: #f1f5f9;
                    border: 1px solid #2b3348;
                    border-radius: 6px;
                    padding: 6px 10px;
                    font-size: 12px;
                    min-height: 22px;
                }
                QComboBox:hover {
                    border-color: #3b82f6;
                }
                QComboBox::drop-down {
                    border: none;
                    width: 20px;
                }
                QDoubleSpinBox {
                    background-color: #171b26;
                    color: #f1f5f9;
                    border: 1px solid #2b3348;
                    border-radius: 6px;
                    padding: 6px 10px;
                    font-size: 12px;
                    min-height: 22px;
                }
                QDoubleSpinBox QLineEdit {
                    padding-left: 10px;
                    padding-right: 10px;
                    background: transparent;
                    color: #f1f5f9;
                    border: none;
                }
                QCheckBox {
                    color: #94a3b8;
                    font-size: 12px;
                    spacing: 8px;
                }
                QCheckBox:hover {
                    color: #f1f5f9;
                }
                QCheckBox::indicator {
                    width: 15px;
                    height: 15px;
                    border-radius: 3px;
                    border: 1px solid #2d3748;
                    background-color: #171b26;
                }
                QCheckBox::indicator:hover {
                    border-color: #3b82f6;
                }
                QCheckBox::indicator:checked {
                    background-color: #2563eb;
                    border-color: #3b82f6;
                }
                QPushButton {
                    background-color: #1a202c;
                    color: #e2e8f0;
                    border: 1px solid #2d3748;
                    border-radius: 6px;
                    padding: 6px 14px;
                    font-size: 12px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background-color: #252d3d;
                    border-color: #3b82f6;
                }
                QPushButton:pressed {
                    background-color: #141822;
                }
                QPushButton#btnDiscord {
                    background-color: #5865F2;
                    color: #ffffff;
                    border: none;
                    border-radius: 4px;
                    padding: 4px 10px;
                    font-size: 11px;
                    font-weight: 600;
                }
                QPushButton#btnDiscord:hover {
                    background-color: #4752c4;
                }
                QPushButton#btnMute {
                    background-color: #1a202c;
                    color: #e2e8f0;
                    border: 1px solid #2d3748;
                    border-radius: 5px;
                    padding: 4px 6px;
                    font-size: 11px;
                    font-weight: 600;
                }
                QPushButton#btnMute:hover {
                    background-color: #252d3d;
                    border-color: #3b82f6;
                }
                QPushButton#btnLaunchStart {
                    background-color: #10b981;
                    color: #ffffff;
                    border: none;
                    font-size: 14px;
                    font-weight: 600;
                    border-radius: 6px;
                    padding: 11px 18px;
                }
                QPushButton#btnLaunchStart:hover {
                    background-color: #059669;
                }
                QPushButton#btnLaunchStop {
                    background-color: #ef4444;
                    color: #ffffff;
                    border: none;
                    font-size: 14px;
                    font-weight: 600;
                    border-radius: 6px;
                    padding: 11px 18px;
                }
                QPushButton#btnLaunchStop:hover {
                    background-color: #dc2626;
                }
                QPushButton#btnStudioStart {
                    background-color: #1d4ed8;
                    color: #ffffff;
                    border: none;
                    font-size: 13px;
                    font-weight: 600;
                    border-radius: 6px;
                    padding: 9px 16px;
                }
                QPushButton#btnStudioStart:hover {
                    background-color: #2563eb;
                }
                QPushButton#btnStudioStop {
                    background-color: #ef4444;
                    color: #ffffff;
                    border: none;
                    font-size: 13px;
                    font-weight: 600;
                    border-radius: 6px;
                    padding: 9px 16px;
                }
                QPushButton#btnStudioStop:hover {
                    background-color: #dc2626;
                }
                QPushButton#btnLaunchSwitch, QPushButton#btnStudioSwitch {
                    background-color: #0891b2;
                    color: #ffffff;
                    border: none;
                    font-weight: 600;
                    border-radius: 6px;
                }
                QPushButton#btnLaunchSwitch {
                    font-size: 14px;
                    padding: 11px 18px;
                }
                QPushButton#btnStudioSwitch {
                    font-size: 13px;
                    padding: 9px 16px;
                }
                QPushButton#btnLaunchSwitch:hover, QPushButton#btnStudioSwitch:hover {
                    background-color: #06b6d4;
                }
                QPushButton#btnLaunchSwitch:pressed, QPushButton#btnStudioSwitch:pressed {
                    background-color: #0e7490;
                }
                QPushButton#btnLaunchSwitch:disabled, QPushButton#btnStudioSwitch:disabled {
                    background-color: #334155;
                    color: #94a3b8;
                }
                QLabel#audioFormat {
                    color: #34d399;
                    font-size: 11px;
                    font-weight: 600;
                }
                QSlider::groove:horizontal {
                    height: 4px;
                    background: #1e2537;
                    border-radius: 2px;
                }
                QSlider::sub-page:horizontal {
                    background: #3b82f6;
                    border-radius: 2px;
                }
                QSlider::handle:horizontal {
                    background: #e2e8f0;
                    width: 14px;
                    margin-top: -5px;
                    margin-bottom: -5px;
                    border-radius: 7px;
                }
                QSlider::handle:horizontal:hover {
                    background: #ffffff;
                }

                /* -----------------------------------------------------
                 * Layout refresh: calm hardware-console palette.
                 * These rules come last on purpose and override the
                 * legacy neon palette above without touching behaviour.
                 * ----------------------------------------------------- */
                QMainWindow { background-color: #0e1116; }
                QLabel { color: #e6e9ef; font-size: 12px; }
                QFrame#cardFrame {
                    background-color: #151a23;
                    border: 1px solid #232a37;
                    border-radius: 10px;
                }
                QFrame#bottomBar {
                    background: transparent;
                    border: none;
                }
                QLabel#logoTitle {
                    font-size: 14px;
                    font-weight: 700;
                    letter-spacing: 0.6px;
                    color: #f2f4f8;
                }
                QLabel#logoVersion { color: #6b7488; font-size: 11px; }
                QLabel#badgeModel {
                    color: #9aa3b5;
                    background-color: #1a2030;
                    border: 1px solid #2a3242;
                    border-radius: 5px;
                    padding: 0px 9px;
                    font-size: 10px;
                    font-weight: 600;
                    letter-spacing: 0.6px;
                }
                QLabel#pillLive {
                    border-radius: 14px;
                    padding: 0px 12px;
                    font-size: 11px;
                    font-weight: 600;
                }
                QLabel#sectionTitle {
                    color: #6b7488;
                    font-size: 10px;
                    font-weight: 700;
                    letter-spacing: 1.0px;
                }
                QLabel#fieldLabel { color: #8b93a7; font-size: 12px; }
                QLabel#heroSignal {
                    color: #f2f4f8;
                    font-size: 20px;
                    font-weight: 600;
                    padding-bottom: 4px;
                    min-height: 28px;
                }
                QLabel#heroMeta {
                    color: #8b93a7;
                    font-size: 12px;
                    padding-bottom: 2px;
                    min-height: 18px;
                }
                QLabel#engineBadge {
                    background-color: #12261f;
                    color: #8fd6b4;
                    border: 1px solid #1d4034;
                    border-radius: 5px;
                    padding: 0px 10px;
                    font-size: 11px;
                    font-weight: 600;
                }
                QLabel#audioFormat {
                    color: #a9c7dd;
                    background-color: #16202c;
                    border: 1px solid #243447;
                    border-radius: 6px;
                    padding: 0px 9px;
                    font-size: 11px;
                    font-weight: 600;
                }
                QLabel#footerText { color: #5f6878; font-size: 11px; }

                QComboBox {
                    min-height: 24px;
                    padding: 4px 10px;
                    font-size: 12px;
                }
                QDoubleSpinBox {
                    min-height: 24px;
                    padding: 4px 10px;
                    font-size: 12px;
                    background-color: #171d29;
                    color: #e6e9ef;
                    border: 1px solid #2a3242;
                    border-radius: 6px;
                }
                QDoubleSpinBox QLineEdit {
                    padding-left: 10px;
                    padding-right: 10px;
                    background: transparent;
                    color: #e6e9ef;
                    border: none;
                }
                QDoubleSpinBox:hover { border-color: #3a465e; }
                QDoubleSpinBox:focus { border-color: #4b6ea8; }
                QDoubleSpinBox:disabled {
                    color: #5f6878;
                    background-color: #121722;
                }
                QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
                    subcontrol-origin: border;
                    width: 0px;
                    border: none;
                }
                QPushButton#btnStepper {
                    padding: 0px;
                    font-size: 15px;
                    font-weight: 600;
                    background-color: #1a2030;
                    color: #d7dde8;
                    border: 1px solid #2a3242;
                    border-radius: 6px;
                }
                QPushButton#btnStepper:hover {
                    background-color: #232c3f;
                    border-color: #3a465e;
                    color: #ffffff;
                }
                QPushButton#btnStepper:pressed {
                    background-color: #151b28;
                }
                QPushButton#btnStepper:disabled {
                    background-color: #121722;
                    color: #4b5363;
                    border-color: #1f2633;
                }

                QPushButton {
                    padding: 6px 14px;
                    font-size: 12px;
                    font-weight: 500;
                    border-radius: 6px;
                    background-color: #1a2030;
                    color: #e6e9ef;
                    border: 1px solid #2a3242;
                }
                QPushButton:hover {
                    background-color: #212a3a;
                    border-color: #3a465e;
                }
                QPushButton:pressed { background-color: #151b28; }
                QPushButton:disabled { color: #5f6878; border-color: #232a37; }

                QPushButton#btnGhost {
                    background-color: transparent;
                    color: #9aa3b5;
                    border: 1px solid #2a3242;
                    font-weight: 500;
                }
                QPushButton#btnGhost:hover {
                    background-color: #1a2030;
                    color: #e6e9ef;
                    border-color: #3a465e;
                }

                QPushButton#btnLaunchStart {
                    background-color: #2f7d63;
                    border: 1px solid #3a9174;
                    color: #ffffff;
                    font-size: 13px;
                    font-weight: 600;
                }
                QPushButton#btnLaunchStart:hover { background-color: #378d71; }

                QPushButton#btnLaunchStop {
                    background-color: #a83f3a;
                    border: 1px solid #bd4a44;
                    color: #ffffff;
                    font-size: 13px;
                    font-weight: 600;
                }
                QPushButton#btnLaunchStop:hover { background-color: #b64a44; }

                QPushButton#btnStudioStart,
                QPushButton#btnStudioSwitch {
                    background-color: transparent;
                    border: 1px solid #34425a;
                    color: #b9c4d6;
                    font-size: 12px;
                    font-weight: 600;
                }
                QPushButton#btnStudioStart:hover,
                QPushButton#btnStudioSwitch:hover {
                    background-color: #1a2030;
                    border-color: #46587a;
                    color: #e6e9ef;
                }

                QPushButton#btnLaunchSwitch {
                    background-color: #2b4f6e;
                    border: 1px solid #38658c;
                    color: #eaf2fa;
                    font-size: 13px;
                    font-weight: 600;
                }
                QPushButton#btnLaunchSwitch:hover { background-color: #326087; }

                QPushButton#btnStudioStop {
                    background-color: #a83f3a;
                    border: 1px solid #bd4a44;
                    color: #ffffff;
                    font-size: 12px;
                    font-weight: 600;
                }
                QPushButton#btnStudioStop:hover { background-color: #b64a44; }

                QCheckBox { color: #b9c0cf; font-size: 12px; spacing: 8px; }
                QCheckBox:hover { color: #e6e9ef; }
                QCheckBox:disabled { color: #5f6878; }
                QCheckBox::indicator {
                    width: 15px; height: 15px;
                    border-radius: 4px;
                    border: 1px solid #34425a;
                    background-color: #171d29;
                }
                QCheckBox::indicator:hover { border-color: #4b6ea8; }
                QCheckBox::indicator:checked {
                    background-color: #2f7d63;
                    border-color: #3a9174;
                }
                QSlider::sub-page:horizontal { background: #3f9d7f; }
                QSlider::handle:horizontal {
                    background: #d7dde8;
                    width: 14px;
                    margin-top: -5px;
                    margin-bottom: -5px;
                    border-radius: 7px;
                }
                QSlider::handle:horizontal:hover { background: #ffffff; }
            """)

            central_widget = QWidget()
            self.setCentralWidget(central_widget)
            root_layout = QVBoxLayout(central_widget)
            root_layout.setContentsMargins(16, 14, 16, 10)
            root_layout.setSpacing(10)

            # -------------------------------------------------------------
            # Header: brand left, community + status right
            # -------------------------------------------------------------
            header_widget = QWidget()
            header_widget.setFixedHeight(32)
            top_bar = QHBoxLayout(header_widget)
            top_bar.setContentsMargins(0, 0, 0, 0)
            top_bar.setSpacing(10)

            if os.path.isfile(icon_path):
                lbl_icon = QLabel()
                pix = QPixmap(icon_path).scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                lbl_icon.setPixmap(pix)
                lbl_icon.setFixedSize(20, 20)
                top_bar.addWidget(lbl_icon, 0, Qt.AlignVCenter)

            lbl_logo = QLabel("EVERLIVE")
            lbl_logo.setObjectName("logoTitle")
            top_bar.addWidget(lbl_logo, 0, Qt.AlignVCenter)

            model_label = MODEL_NAMES.get(
                self.model, MODEL_NAMES[MODEL_GC573])
            lbl_badge = QLabel(model_label if self.vfio_mode
                               else model_label.split(" · ", 1)[0])
            lbl_badge.setObjectName("badgeModel")
            lbl_badge.setFixedHeight(24)
            lbl_badge.setAlignment(Qt.AlignCenter)
            top_bar.addWidget(lbl_badge, 0, Qt.AlignVCenter)

            lbl_ver = QLabel(f"v{__version__}")
            lbl_ver.setObjectName("logoVersion")
            top_bar.addWidget(lbl_ver, 0, Qt.AlignVCenter)

            top_bar.addStretch(1)

            self.btn_advanced = QPushButton("Advanced ⚙" if not self.is_advanced_mode else "Hide Advanced ✕")
            self.btn_advanced.setObjectName("btnGhost")
            self.btn_advanced.setFixedHeight(28)
            self.btn_advanced.setStyleSheet("padding: 0px 12px; font-weight: 600;")
            self.btn_advanced.setCursor(Qt.PointingHandCursor)
            self.btn_advanced.setToolTip("Toggle between Simple View (clean zero-friction console hub) and Advanced Settings (granular EDID, exposure, and OBS controls)")
            self.btn_advanced.clicked.connect(self.toggle_advanced_mode)
            self.btn_pro_mode = self.btn_advanced
            top_bar.addWidget(self.btn_advanced, 0, Qt.AlignVCenter)

            btn_discord = QPushButton("Discord")
            btn_discord.setObjectName("btnGhost")
            btn_discord.setFixedHeight(28)
            btn_discord.setStyleSheet("padding: 0px 12px;")
            btn_discord.setCursor(Qt.PointingHandCursor)
            btn_discord.setToolTip("Join the EverLive Discord community (Feedback & Hardware Support)")
            btn_discord.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://discord.com/invite/D2XFhkPMmc")))
            top_bar.addWidget(btn_discord, 0, Qt.AlignVCenter)

            self.lbl_pill = QLabel(ENGINE_VFIO_TEXT if self.vfio_mode else "○ Standby")
            self.lbl_pill.setObjectName("pillLive")
            self.lbl_pill.setFixedHeight(28)
            self.lbl_pill.setAlignment(Qt.AlignCenter)
            if self.vfio_mode:
                self.lbl_pill.setStyleSheet("background-color: #122a22; color: #7fd1ad; border: 1px solid #1e3c31;")
            else:
                self.lbl_pill.setStyleSheet("background-color: #1a2030; color: #9aa3b5; border: 1px solid #2a3242;")
            top_bar.addWidget(self.lbl_pill, 0, Qt.AlignVCenter)

            root_layout.addWidget(header_widget)

            # -------------------------------------------------------------
            # Hero: live signal, telemetry and runtime actions
            # -------------------------------------------------------------
            card_hero = QFrame()
            card_hero.setObjectName("cardFrame")
            v_hero = QVBoxLayout(card_hero)
            v_hero.setSpacing(10)
            v_hero.setContentsMargins(14, 12, 14, 14)

            h_sig_info = QHBoxLayout()
            h_sig_info.setSpacing(12)
            v_sig_text = QVBoxLayout()
            v_sig_text.setSpacing(4)

            self.lbl_hero_signal = QLabel("Waiting for HDMI signal...")
            self.lbl_hero_signal.setObjectName("heroSignal")
            self.lbl_hero_signal.setMinimumHeight(28)
            v_sig_text.addWidget(self.lbl_hero_signal)

            self.lbl_hero_meta = QLabel("Checking input source (Console / PC)...")
            self.lbl_hero_meta.setObjectName("heroMeta")
            self.lbl_hero_meta.setMinimumHeight(18)
            v_sig_text.addWidget(self.lbl_hero_meta)

            h_sig_info.addLayout(v_sig_text, 1)

            self.lbl_audio_fmt = QLabel("No Audio")
            self.lbl_audio_fmt.setObjectName("audioFormat")
            self.lbl_audio_fmt.setFixedHeight(26)
            self.lbl_audio_fmt.setAlignment(Qt.AlignCenter)
            self.lbl_audio_fmt.setToolTip(
                "Audio format detected by the userspace engine from the "
                "IT6805 / SDL2 stream log.")
            h_sig_info.addWidget(self.lbl_audio_fmt, 0, Qt.AlignVCenter)

            self.lbl_engine_badge = QLabel(
                "Userspace VFIO" if self.vfio_mode
                else "Direct Wayland (< 2ms)")
            self.lbl_engine_badge.setObjectName("engineBadge")
            self.lbl_engine_badge.setFixedHeight(26)
            self.lbl_engine_badge.setAlignment(Qt.AlignCenter)
            if self.vfio_mode:
                self.lbl_engine_badge.setToolTip(
                    "Blob-free Ring-3 VFIO engine • SDL2 video and HDMI audio")
            h_sig_info.addWidget(self.lbl_engine_badge, 0, Qt.AlignVCenter)

            v_hero.addLayout(h_sig_info)

            # Embedded Live Video Feed Canvas
            self.video_canvas = EmbeddedVideoCanvas(self, duo_mode=self.duo_mode)
            saved_input = load_settings().get("duo_input", 0)
            self.video_canvas.set_port(saved_input)
            self.video_canvas.fullscreen_requested.connect(self.toggle_fullscreen)
            self.video_canvas.toggle_play_requested.connect(self.toggle_stream)
            if self.duo_mode:
                self.video_canvas.port_requested.connect(
                    lambda p: self.combo_duo_input.setCurrentIndex(1 if p == 1 else 0) if hasattr(self, "combo_duo_input") else None)
            v_hero.addWidget(self.video_canvas, 1)

            # Bottom toolbar for Hero Card: hardware controls & engine actions
            h_hero_toolbar = QHBoxLayout()
            h_hero_toolbar.setSpacing(8)

            if self.duo_mode:
                lbl_duo = QLabel("DUO Input")
                lbl_duo.setObjectName("fieldLabel")
                h_hero_toolbar.addWidget(lbl_duo, 0, Qt.AlignVCenter)

                self.combo_duo_input = QComboBox()
                self.combo_duo_input.addItem("HDMI 1", 0)
                self.combo_duo_input.addItem("HDMI 2", 1)
                self.combo_duo_input.setCurrentIndex(1 if saved_input == 1 else 0)
                self.combo_duo_input.setToolTip(
                    "Select which Live Gamer DUO HDMI input is shown in the "
                    "gaming window and primary capture stream.")
                self.combo_duo_input.currentIndexChanged.connect(
                    self.on_duo_input_changed)
                h_hero_toolbar.addWidget(self.combo_duo_input, 0, Qt.AlignVCenter)

                btn_hpd = QPushButton("Pulse HPD")
                btn_hpd.setObjectName("btnGhost")
                btn_hpd.setCursor(Qt.PointingHandCursor)
                btn_hpd.setMinimumHeight(32)
                btn_hpd.setToolTip(
                    "Re-trigger the HDMI hot-plug handshake on the selected input.")
                btn_hpd.clicked.connect(self._duo_hpd_action)
                h_hero_toolbar.addWidget(btn_hpd, 0, Qt.AlignVCenter)

                btn_bridge = QPushButton("EDID: Bridge")
                btn_bridge.setObjectName("btnGhost")
                btn_bridge.setCursor(Qt.PointingHandCursor)
                btn_bridge.setMinimumHeight(32)
                btn_bridge.setToolTip(
                    "Advertise the internal 4K60 bridge EDID to HDMI IN 1. "
                    "Required for standalone capture without a downstream TV.")
                btn_bridge.clicked.connect(
                    lambda: self._duo_edid_action(True))
                h_hero_toolbar.addWidget(btn_bridge, 0, Qt.AlignVCenter)

                btn_copy = QPushButton("EDID: Copy")
                btn_copy.setObjectName("btnGhost")
                btn_copy.setCursor(Qt.PointingHandCursor)
                btn_copy.setMinimumHeight(32)
                btn_copy.setToolTip(
                    "Copy the EDID of the display/AVR connected to HDMI OUT.")
                btn_copy.clicked.connect(
                    lambda: self._duo_edid_action(False))
                h_hero_toolbar.addWidget(btn_copy, 0, Qt.AlignVCenter)

            h_hero_toolbar.addStretch(1)

            self.btn_resync = QPushButton(
                "Restart Engine" if self.vfio_mode else "Re-sync")
            self.btn_resync.setObjectName("btnGhost")
            self.btn_resync.setCursor(Qt.PointingHandCursor)
            self.btn_resync.setMinimumHeight(32)
            self.btn_resync.setToolTip(
                "Restart the userspace VFIO engine. HDMI hot-switching is automatic."
                if self.vfio_mode else
                "Trigger hardware HDMI handshake (HPD pulse) - also key 'H' in window")
            self.btn_resync.clicked.connect(self.force_resync)
            h_hero_toolbar.addWidget(self.btn_resync, 0, Qt.AlignVCenter)

            v_hero.addLayout(h_hero_toolbar)
            root_layout.addWidget(card_hero, 2)

            # -------------------------------------------------------------
            # Settings: three calm, thematically grouped sections
            # -------------------------------------------------------------
            def _section(title):
                frame = QFrame()
                frame.setObjectName("cardFrame")
                lay = QVBoxLayout(frame)
                lay.setContentsMargins(14, 10, 14, 12)
                lay.setSpacing(8)
                head = QLabel(title)
                head.setObjectName("sectionTitle")
                lay.addWidget(head)
                grid = QGridLayout()
                grid.setHorizontalSpacing(14)
                grid.setVerticalSpacing(6)
                lay.addLayout(grid)
                return frame, grid

            def _field(text, tip=""):
                lbl = QLabel(text)
                lbl.setObjectName("fieldLabel")
                if tip:
                    lbl.setToolTip(tip)
                return lbl

            # --- Section A: video & color -------------------------------
            card_video, grid_video = _section("VIDEO & COLOR")
            grid_video.setColumnMinimumWidth(0, 148)
            grid_video.setColumnStretch(1, 1)
            grid_video.setColumnMinimumWidth(2, 92)
            grid_video.setColumnStretch(3, 1)

            grid_video.addWidget(_field(
                "Hardware EDID Profile" if self.vfio_mode else "Format / Preset"),
                0, 0)

            self.combo_presets = QComboBox()
            if self.duo_mode:
                duo_formats = [
                    ("Auto (HDMI Native Sync)", "auto"),
                    ("1080p @ 60 FPS (1920×1080)", "1080p60"),
                    ("1080p @ 50 FPS (PAL)", "1080p50"),
                    ("1080p @ 30 FPS", "1080p30"),
                    ("1080p @ 24 FPS (Cinema)", "1080p24"),
                    ("1080i @ 60/50 FPS (Interlaced)", "1080i"),
                    ("720p @ 60 FPS (1280×720)", "720p60"),
                    ("720p @ 50 FPS (PAL)", "720p50"),
                    ("720x576 @ 50 FPS (576p PAL)", "576p"),
                    ("720x480 @ 60 FPS (480p NTSC)", "480p"),
                    ("640x480 @ 60 FPS (VGA)", "640x480"),
                ]
                for text, key in duo_formats:
                    self.combo_presets.addItem(text, key)
                self.combo_presets.setEnabled(True)
                self.combo_presets.setToolTip(
                    "Hardware capture modes supported by Live Gamer DUO. "
                    "In Auto mode, the card automatically locks onto the incoming HDMI clock and timing.")
            elif self.vfio_mode:
                for text, key in EDID_PROFILES:
                    self.combo_presets.addItem(text, key)
                saved_profile = self.saved_cfg.get("edid_profile", "default")
                custom_path = self.saved_cfg.get("edid_custom_path", "")
                if saved_profile == "custom":
                    if custom_path and os.path.isfile(custom_path):
                        idx = self.combo_presets.findData("custom")
                        if idx >= 0:
                            self.combo_presets.setItemText(
                                idx, f"Custom: {os.path.basename(custom_path)}")
                    else:
                        saved_profile = "default"
                idx = self.combo_presets.findData(saved_profile)
                if idx >= 0:
                    self.combo_presets.setCurrentIndex(idx)
                self.combo_presets.setToolTip(
                    "256-byte EDID advertised to the console. Every profile "
                    "supports L-PCM, Dolby Digital (AC-3) and DTS 5.1; "
                    "compressed audio is decoded automatically. "
                    "Applies on the next Start Gaming Screen.")
            else:
                self.combo_presets.addItems(list(PRESETS.keys()))
                if "preset" in self.saved_cfg and self.saved_cfg["preset"] in PRESETS:
                    self.combo_presets.setCurrentText(self.saved_cfg["preset"])
            # activated (not currentIndexChanged) so re-picking the custom
            # entry opens the file dialog again.
            self.combo_presets.activated.connect(self.on_preset_changed)
            h_presets = QHBoxLayout()
            h_presets.setContentsMargins(0, 0, 0, 0)
            h_presets.setSpacing(6)
            h_presets.addWidget(self.combo_presets, 1)
            self.btn_import_modeline = QPushButton("Import Modeline...")
            self.btn_import_modeline.setToolTip("Import custom X11/xrandr modeline and generate EDID profile")
            self.btn_import_modeline.clicked.connect(self.on_import_modeline)
            h_presets.addWidget(self.btn_import_modeline)
            grid_video.addLayout(h_presets, 0, 1)

            grid_video.addWidget(_field(
                "Aspect Ratio",
                "Overrides SDL2 presentation. Auto follows the native HDMI "
                "timing; 16:9 fixes anamorphic RetroTINK/OSSC modes."), 0, 2)

            self.combo_aspect = QComboBox()
            for text, key in ASPECT_OPTIONS:
                self.combo_aspect.addItem(text, key)
            saved_aspect = self.saved_cfg.get("aspect_ratio", "auto")
            idx = self.combo_aspect.findData(saved_aspect)
            if idx >= 0:
                self.combo_aspect.setCurrentIndex(idx)
            self.combo_aspect.setToolTip(
                "Forwarded as --aspect to gc573_user_streamer unless "
                "'Auto (Native HDMI)' is selected.")
            self.combo_aspect.currentIndexChanged.connect(self.on_aspect_changed)
            grid_video.addWidget(self.combo_aspect, 0, 3)

            grid_video.addWidget(_field(
                "HDR Tonemapping",
                "Maps BT.2020 PQ/HLG signals to BT.709 SDR for both the "
                "gaming window and the OBS loopback feed (hotkey: H)."), 1, 0)

            h_hdr = QHBoxLayout()
            h_hdr.setContentsMargins(0, 0, 0, 0)
            h_hdr.setSpacing(8)
            self.chk_hdr = QCheckBox("HDR10 / HLG → SDR")
            self.chk_hdr.setChecked(bool(self.saved_cfg.get("hdr_tonemap", True)))
            self.chk_hdr.setToolTip(
                "Forwarded as --no-hdr when unchecked. Applies live over IPC "
                "while the engine runs.")
            self.chk_hdr.toggled.connect(self.on_hdr_toggled)
            h_hdr.addWidget(self.chk_hdr)

            self.chk_hdr_passthrough = QCheckBox("Passthrough")
            self.chk_hdr_passthrough.setChecked(bool(self.saved_cfg.get("hdr_passthrough", False)))
            self.chk_hdr_passthrough.setToolTip(
                "Direct BT.2020 PQ/HLG passthrough to HDR-capable displays (bypasses SDR tonemapping).")
            self.chk_hdr_passthrough.toggled.connect(self.on_hdr_passthrough_toggled)
            h_hdr.addWidget(self.chk_hdr_passthrough)
            grid_video.addLayout(h_hdr, 1, 1)

            grid_video.addWidget(_field(
                "Exposure", "Linear exposure bias (-2.0 .. +2.0 EV)."), 1, 2)

            h_exposure = QHBoxLayout()
            h_exposure.setContentsMargins(0, 0, 0, 0)
            h_exposure.setSpacing(4)

            self.spin_hdr_exposure = QDoubleSpinBox()
            self.spin_hdr_exposure.setButtonSymbols(QAbstractSpinBox.NoButtons)
            self.spin_hdr_exposure.setFixedHeight(34)
            self.spin_hdr_exposure.setRange(-2.0, 2.0)
            self.spin_hdr_exposure.setSingleStep(0.1)
            self.spin_hdr_exposure.setDecimals(1)
            self.spin_hdr_exposure.setSuffix(" EV")
            self.spin_hdr_exposure.setValue(
                float(self.saved_cfg.get("hdr_exposure", 0.0)))
            self.spin_hdr_exposure.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.spin_hdr_exposure.lineEdit().setTextMargins(10, 0, 10, 0)
            self.spin_hdr_exposure.setToolTip(
                "Linear exposure bias for the tonemapper (-2.0 .. +2.0 EV).")
            self.spin_hdr_exposure.setEnabled(self.chk_hdr.isChecked())
            self.spin_hdr_exposure.valueChanged.connect(
                self.on_hdr_exposure_changed)
            h_exposure.addWidget(self.spin_hdr_exposure, 1)

            self.btn_hdr_exp_minus = QPushButton("−")
            self.btn_hdr_exp_minus.setObjectName("btnStepper")
            self.btn_hdr_exp_minus.setFixedSize(34, 34)
            self.btn_hdr_exp_minus.setCursor(Qt.PointingHandCursor)
            self.btn_hdr_exp_minus.setFocusPolicy(Qt.NoFocus)
            self.btn_hdr_exp_minus.setAutoRepeat(True)
            self.btn_hdr_exp_minus.setToolTip("Decrease HDR exposure (-0.1 EV)")
            self.btn_hdr_exp_minus.setEnabled(self.chk_hdr.isChecked())
            self.btn_hdr_exp_minus.clicked.connect(
                self.spin_hdr_exposure.stepDown)
            h_exposure.addWidget(self.btn_hdr_exp_minus)

            self.btn_hdr_exp_plus = QPushButton("+")
            self.btn_hdr_exp_plus.setObjectName("btnStepper")
            self.btn_hdr_exp_plus.setFixedSize(34, 34)
            self.btn_hdr_exp_plus.setCursor(Qt.PointingHandCursor)
            self.btn_hdr_exp_plus.setFocusPolicy(Qt.NoFocus)
            self.btn_hdr_exp_plus.setAutoRepeat(True)
            self.btn_hdr_exp_plus.setToolTip("Increase HDR exposure (+0.1 EV)")
            self.btn_hdr_exp_plus.setEnabled(self.chk_hdr.isChecked())
            self.btn_hdr_exp_plus.clicked.connect(
                self.spin_hdr_exposure.stepUp)
            h_exposure.addWidget(self.btn_hdr_exp_plus)

            grid_video.addLayout(h_exposure, 1, 3)

            grid_video.addWidget(_field(
                "Scaling Mode",
                "Integer Scale picks the largest whole multiple of the "
                "source (crisp pixels for retro/720p). Falls back to Aspect "
                "Fit when the monitor is smaller or the content is anamorphic."),
                2, 0)

            self.combo_scale = QComboBox()
            for text, key in SCALE_OPTIONS:
                self.combo_scale.addItem(text, key)
            saved_scale = self.saved_cfg.get("scale_mode", "fit")
            scale_idx = self.combo_scale.findData(saved_scale)
            if scale_idx >= 0:
                self.combo_scale.setCurrentIndex(scale_idx)
            self.combo_scale.setToolTip(
                "Forwarded as --scale. Applies live over IPC while the "
                "engine runs. Hotkey 'L' in the gaming window cycles it.")
            self.combo_scale.currentIndexChanged.connect(self.on_scale_changed)
            grid_video.addWidget(self.combo_scale, 2, 1)

            # --- Section: frame generation --------------------
            card_fg, grid_fg = _section(
                "EXPERIMENTAL · LSFG-VK FRAME GENERATION")
            grid_fg.setVerticalSpacing(8)
            grid_fg.setColumnMinimumWidth(0, 148)
            grid_fg.setColumnStretch(1, 1)
            grid_fg.setColumnMinimumWidth(2, 140)
            grid_fg.setColumnStretch(3, 1)

            self._fg_ready_ok = False
            grid_fg.addWidget(_field(
                "Installation",
                "Lossless Scaling Frame Generation 3.x through the native "
                "lsfg-vk 2.0 Vulkan layer (Steam Beta branch 'lsfg-vk')."),
                0, 0)

            self.lbl_fg_ready = QLabel("Checking lsfg-vk…")
            self.lbl_fg_ready.setObjectName("fieldLabel")
            self.lbl_fg_ready.setWordWrap(True)
            grid_fg.addWidget(self.lbl_fg_ready, 0, 1, 1, 2, Qt.AlignVCenter)

            self.btn_fg_recheck = QPushButton("Re-check")
            self.btn_fg_recheck.setObjectName("btnGhost")
            self.btn_fg_recheck.setFixedSize(96, 26)
            self.btn_fg_recheck.setStyleSheet("padding: 0px 12px;")
            self.btn_fg_recheck.setCursor(Qt.PointingHandCursor)
            self.btn_fg_recheck.setToolTip(
                "Re-scan for the lsfg-vk layer and lsfg-vk.dll without "
                "restarting the Hub.")
            self.btn_fg_recheck.clicked.connect(self.on_fg_recheck)
            grid_fg.addWidget(
                self.btn_fg_recheck, 0, 3, Qt.AlignRight | Qt.AlignVCenter)
            self._refresh_fg_readiness()

            grid_fg.addWidget(_field(
                "Frame Generation",
                "lsfg-vk 2x. The 30→60 mode drops the repeated HDMI frame a "
                "PS5 sends for a 30 FPS game before the layer interpolates."),
                1, 0)

            self.combo_framegen = QComboBox()
            for text, key in FRAMEGEN_OPTIONS:
                self.combo_framegen.addItem(text, key)
            fg_idx = self.combo_framegen.findData(
                self.saved_cfg.get("framegen_mode", "off"))
            if fg_idx >= 0:
                self.combo_framegen.setCurrentIndex(fg_idx)
            self.combo_framegen.setToolTip(
                "Forwarded as --framegen. Applies live over IPC while the "
                "engine runs (hotkey 'g' cycles it in the game window).")
            self.combo_framegen.currentIndexChanged.connect(
                self.on_framegen_changed)
            grid_fg.addWidget(self.combo_framegen, 1, 1, 1, 3)

            grid_fg.addWidget(_field(
                "Flow Scale",
                "Optical-flow resolution used by LSFG. 100% is full quality; "
                "lower values cost less GPU time on 1440p/4K captures."),
                2, 0)

            h_flow = QHBoxLayout()
            h_flow.setContentsMargins(0, 0, 0, 0)
            h_flow.setSpacing(8)
            self.slider_flow = QSlider(Qt.Horizontal)
            self.slider_flow.setMinimumHeight(24)
            self.slider_flow.setRange(50, 100)
            self.slider_flow.setValue(int(round(
                float(self.saved_cfg.get("fg_flow_scale", 1.0)) * 100)))
            self.slider_flow.setToolTip("LSFGVK_FLOW_SCALE: 50% to 100%.")
            self.slider_flow.valueChanged.connect(self.on_flow_scale_changed)
            self.slider_flow.sliderReleased.connect(
                lambda: save_settings(self._framegen_config()))
            h_flow.addWidget(self.slider_flow, 1)

            self.lbl_flow = QLabel(f"{self.slider_flow.value()}%")
            self.lbl_flow.setObjectName("fieldLabel")
            self.lbl_flow.setFixedWidth(38)
            h_flow.addWidget(self.lbl_flow, 0, Qt.AlignVCenter)
            grid_fg.addLayout(h_flow, 2, 1)

            self.chk_fg_perf = QCheckBox("Performance Mode")
            self.chk_fg_perf.setMinimumHeight(24)
            self.chk_fg_perf.setChecked(
                bool(self.saved_cfg.get("fg_perf_mode", False)))
            self.chk_fg_perf.setToolTip(
                "LSFGVK_PERFORMANCE_MODE: lower latency and less GPU load, "
                "at the cost of some interpolation quality.")
            self.chk_fg_perf.toggled.connect(self.on_fg_perf_toggled)
            grid_fg.addWidget(self.chk_fg_perf, 2, 2, Qt.AlignVCenter)

            self.chk_fg_ll = QCheckBox("Low-Latency Swapchain")
            self.chk_fg_ll.setMinimumHeight(24)
            self.chk_fg_ll.setChecked(
                bool(self.saved_cfg.get("fg_low_latency", True)))
            self.chk_fg_ll.setToolTip(
                "Sets LSFGVK_PRESERVE_SWAPCHAIN_IMAGE_COUNT=1 so lsfg-vk keeps "
                "the minimum Vulkan swapchain queue depth (saves 8-17 ms at "
                "120 Hz). Uncheck if frame pacing ever stutters.")
            self.chk_fg_ll.toggled.connect(self.on_fg_low_latency_toggled)
            grid_fg.addWidget(self.chk_fg_ll, 2, 3, Qt.AlignVCenter)

            self.lbl_fg_status = QLabel(
                "Frame generation is off (native zero-latency path).")
            self.lbl_fg_status.setObjectName("fieldLabel")
            grid_fg.addWidget(self.lbl_fg_status, 3, 0, 1, 4)
            self._refresh_idle_fg_status()

            if self.duo_mode:
                card_fg.setVisible(False)
            # --- Section B: audio & hardware ----------------------------
            card_audio, grid_audio = _section("AUDIO & HARDWARE")
            grid_audio.setColumnMinimumWidth(0, 148)
            grid_audio.setColumnStretch(1, 1)
            grid_audio.setColumnMinimumWidth(2, 92)
            grid_audio.setColumnStretch(3, 1)

            grid_audio.addWidget(_field(
                "Volume",
                "Set console audio to 2-channel Linear PCM (Stereo) to "
                "prevent audio dropouts / clicks."), 0, 0)

            h_vol = QHBoxLayout()
            h_vol.setSpacing(8)
            self.btn_mute = QPushButton("Mute")
            self.btn_mute.setObjectName("btnMute")
            self.btn_mute.setFixedWidth(70)
            self.btn_mute.setCursor(Qt.PointingHandCursor)
            self.btn_mute.clicked.connect(self.toggle_mute)
            h_vol.addWidget(self.btn_mute)

            self.slider_vol = QSlider(Qt.Horizontal)
            self.slider_vol.setRange(0, 150)
            self.slider_vol.setToolTip(
                "Volume 0-150%. Note: Set console audio to 2-channel Linear "
                "PCM (Stereo) to avoid buffer underruns.")
            initial_vol = int(self.saved_cfg.get("volume", 1.0) * 100)
            self.slider_vol.setValue(initial_vol)
            self.slider_vol.valueChanged.connect(self.on_volume_changed)
            h_vol.addWidget(self.slider_vol, 1)

            self.lbl_vol = QLabel(f"{initial_vol}%")
            self.lbl_vol.setFixedWidth(36)
            self.lbl_vol.setObjectName("fieldLabel")
            h_vol.addWidget(self.lbl_vol)

            if self.vfio_mode:
                self.btn_mute.setToolTip(
                    "Mutes the SDL2 HDMI audio stream via PipeWire/PulseAudio.")
                self.slider_vol.setToolTip(
                    "Sets the PipeWire volume of the userspace SDL2 HDMI audio stream.")
            grid_audio.addLayout(h_vol, 0, 1)

            grid_audio.addWidget(_field("Card LED"), 0, 2)

            self.combo_led = QComboBox()
            self.combo_led.setIconSize(QSize(14, 14))
            for name, val in LED_MODES.items():
                icon = create_led_icon(val)
                if icon:
                    self.combo_led.addItem(icon, name, val)
                else:
                    self.combo_led.addItem(name, val)

            saved_led = self.saved_cfg.get("led_mode", None)
            if self.vfio_mode:
                # everlive_userd has no readback path; use the persisted mode
                # and default to the verified cyan profile.
                led_mode = saved_led if isinstance(saved_led, int) and 0 <= saved_led <= 9 else 4
                idx = self.combo_led.findData(led_mode)
                if idx >= 0:
                    self.combo_led.setCurrentIndex(idx)
                will_autostart = self._headless_start or self.saved_cfg.get("autostart", True) or getattr(self.streamer, "_cli_autostart", False)
                if isinstance(saved_led, int) and 0 <= saved_led <= 9 and not will_autostart:
                    if self.duo_mode:
                        # The DUO has no standby LED daemon; only talk to a
                        # running engine, otherwise the mode is applied by the
                        # next launch.
                        if _vfio_streamer_running():
                            set_gc570d_led(saved_led)
                    else:
                        set_vfio_led(saved_led)
            else:
                if saved_led is not None and isinstance(saved_led, int) and 0 <= saved_led <= 7:
                    write_led_mode(saved_led)
                    idx = self.combo_led.findData(saved_led)
                    if idx >= 0:
                        self.combo_led.setCurrentIndex(idx)
                else:
                    curr_led = read_led_mode()
                    if curr_led >= 0:
                        idx = self.combo_led.findData(curr_led)
                        if idx >= 0:
                            self.combo_led.setCurrentIndex(idx)
                    else:
                        self.combo_led.setEnabled(False)
            self.combo_led.currentIndexChanged.connect(self.on_led_changed)
            h_led = QHBoxLayout()
            h_led.setContentsMargins(0, 0, 0, 0)
            h_led.setSpacing(6)
            h_led.addWidget(self.combo_led, 1)

            self.btn_custom_led = QPushButton("🎨")
            self.btn_custom_led.setToolTip("Pick custom RGB color for card LEDs")
            self.btn_custom_led.setFixedSize(34, 30)
            self.btn_custom_led.clicked.connect(self.on_pick_custom_led)
            saved_hex = self.saved_cfg.get("led_custom_hex", "")
            if saved_hex:
                self.btn_custom_led.setStyleSheet(f"background-color: {saved_hex}; border-radius: 4px;")
            h_led.addWidget(self.btn_custom_led)
            grid_audio.addLayout(h_led, 0, 3)

            # --- Section C: outputs, overlay & app ----------------------
            card_out, grid_out = _section("OBS OUTPUTS, OVERLAY & APP")
            grid_out.setColumnMinimumWidth(0, 108)
            grid_out.setColumnStretch(1, 1)
            grid_out.setColumnStretch(2, 1)

            grid_out.addWidget(_field("OBS Routing"), 0, 0)

            has_loopback_node = os.path.exists(V4L2_DEFAULT_DEVICE)
            self.chk_v4l2 = QCheckBox(
                f"Legacy Loopback ({V4L2_DEFAULT_DEVICE})")
            self.chk_v4l2.setChecked(bool(self.saved_cfg.get("v4l2_loopback", False) and has_loopback_node))
            self.chk_v4l2.toggled.connect(self.on_v4l2_toggled)
            if self.vfio_mode:
                tip = (
                    "Optional fallback: publishes native frames to a v4l2loopback kernel node. "
                    "Modern OBS and Discord can connect directly via PipeWire (gc570d_hdmi1_video) "
                    "or libgc570d-v4l2.so without any kernel modules."
                )
                if not has_loopback_node:
                    tip += f"\n(Note: {V4L2_DEFAULT_DEVICE} is not loaded; module-free capture is active)"
                self.chk_v4l2.setToolTip(tip)
            else:
                self.chk_v4l2.setChecked(False)
                self.chk_v4l2.setEnabled(False)
                self.chk_v4l2.setToolTip(
                    "Virtual camera output requires the userspace VFIO engine.")
            grid_out.addWidget(self.chk_v4l2, 0, 1)

            self.chk_audio_tap = QCheckBox("Virtual Audio Track")
            self.chk_audio_tap.setToolTip(
                "Publishes the mixed HDMI audio (L-PCM and decoded AC-3/DTS) "
                f"to the PipeWire sink '{AUDIO_TAP_NODE}'. In OBS add "
                "'Monitor of EverLive Capture Audio' as its own audio source.")
            self.chk_audio_tap.setChecked(bool(self.saved_cfg.get("audio_tap", False)))
            self.chk_audio_tap.toggled.connect(
                lambda val: save_settings({"audio_tap": val}))
            if not self.vfio_mode:
                self.chk_audio_tap.setChecked(False)
                self.chk_audio_tap.setEnabled(False)
            grid_out.addWidget(self.chk_audio_tap, 0, 2)

            grid_out.addWidget(_field("Display & HUD"), 1, 0)

            self.chk_fullscreen = QCheckBox("Fullscreen")
            self.chk_fullscreen.setChecked(self.saved_cfg.get("fullscreen", True))
            self.chk_fullscreen.toggled.connect(lambda val: save_settings({"fullscreen": val}))
            grid_out.addWidget(self.chk_fullscreen, 1, 1)

            self.chk_vsync = QCheckBox("VSync")
            self.chk_vsync.setChecked(self.saved_cfg.get("vsync", self.enable_vsync))
            self.chk_vsync.toggled.connect(lambda val: save_settings({"vsync": val}))
            grid_out.addWidget(self.chk_vsync, 1, 2)

            grid_out.addWidget(_field(""), 2, 0)

            self.chk_streamer = QCheckBox("Streamer Mode (Clean Feed)")
            self.chk_streamer.setToolTip(
                "Hides mouse control bar, HUD, and overlays for clean recordings "
                "(Hotkey: 'S')")
            self.chk_streamer.setChecked(self.saved_cfg.get("streamer_mode", False))
            self.chk_streamer.toggled.connect(lambda val: save_settings({"streamer_mode": val}))
            grid_out.addWidget(self.chk_streamer, 2, 1)

            h_osd = QHBoxLayout()
            h_osd.setSpacing(8)
            self.chk_osd = QCheckBox("On-Screen Stats")
            self.chk_osd.setToolTip(
                "Live resolution, FPS and audio format drawn by the engine "
                "itself. Hotkeys in the game window: Tab/o toggles, p cycles "
                "the corner.")
            self.chk_osd.setChecked(bool(self.saved_cfg.get("osd_enabled", True)))
            self.chk_osd.toggled.connect(self.on_osd_changed)
            h_osd.addWidget(self.chk_osd)

            self.combo_osd_pos = QComboBox()
            for text, key in OSD_POSITION_OPTIONS:
                self.combo_osd_pos.addItem(text, key)
            saved_pos = self.saved_cfg.get("osd_position", "tr")
            pos_idx = self.combo_osd_pos.findData(saved_pos)
            if pos_idx >= 0:
                self.combo_osd_pos.setCurrentIndex(pos_idx)
            self.combo_osd_pos.setToolTip(
                "Forwarded as --osd-pos. Top Right is the default so the "
                "overlay never covers console navigation.")
            self.combo_osd_pos.setEnabled(self.chk_osd.isChecked())
            self.combo_osd_pos.currentIndexChanged.connect(self.on_osd_pos_changed)
            h_osd.addWidget(self.combo_osd_pos, 1)
            grid_out.addLayout(h_osd, 2, 2)

            grid_out.addWidget(_field("App Behavior"), 3, 0)

            self.chk_autostart = QCheckBox("Auto-start on launch")
            self.chk_autostart.setChecked(self.saved_cfg.get("autostart", True))
            self.chk_autostart.toggled.connect(lambda val: save_settings({"autostart": val}))
            grid_out.addWidget(self.chk_autostart, 3, 1)

            self.chk_tray = QCheckBox("Close to Tray")
            self.chk_tray.setToolTip(
                "Closing with [X] hides the Console Hub window into the system "
                "tray so it does not clutter Alt-Tab.")
            self.chk_tray.setChecked(bool(self.saved_cfg.get("minimize_to_tray", True)))
            self.chk_tray.toggled.connect(lambda val: save_settings({"minimize_to_tray": val}))
            grid_out.addWidget(self.chk_tray, 3, 2)

            if self.vfio_mode:
                self.chk_fullscreen.setToolTip(
                    "Starts the userspace SDL2 window in fullscreen. "
                    "Press F in the game window to toggle it at runtime.")
                self.chk_vsync.setToolTip(
                    "Paces SDL2 presentation to the display refresh rate.")
                self.chk_streamer.setToolTip(
                    "Clean feed: hides the on-screen stats overlay and the "
                    "mouse cursor in the game window (Tab/'o' toggles at runtime).")

            grid_out.addWidget(_field("Detached Window"), 4, 0)
            self.btn_detached = QPushButton("⧉ Launch Detached SDL Window")
            self.btn_detached.setObjectName("btnGhost")
            self.btn_detached.setCursor(Qt.PointingHandCursor)
            self.btn_detached.setMinimumHeight(32)
            self.btn_detached.setToolTip(
                "Launch the classic detached SDL2 floating window instead of the integrated UI player")
            self.btn_detached.clicked.connect(self.start_detached_window)
            grid_out.addWidget(self.btn_detached, 4, 1)

            # Advanced Settings Scroll Area
            self.scroll_advanced = QScrollArea()
            self.scroll_advanced.setWidgetResizable(True)
            self.scroll_advanced.setFrameShape(QFrame.NoFrame)
            self.scroll_advanced.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.scroll_advanced.setStyleSheet("""
                QScrollArea {
                    background: transparent;
                    border: none;
                }
                QScrollBar:vertical {
                    background: #0e1116;
                    width: 8px;
                    margin: 0px;
                    border-radius: 4px;
                }
                QScrollBar::handle:vertical {
                    background: #2a3448;
                    min-height: 20px;
                    border-radius: 4px;
                }
                QScrollBar::handle:vertical:hover {
                    background: #3b82f6;
                }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                    height: 0px;
                }
            """)

            scroll_content = QWidget()
            scroll_content.setStyleSheet("background: transparent;")
            v_advanced = QVBoxLayout(scroll_content)
            v_advanced.setContentsMargins(0, 0, 0, 0)
            v_advanced.setSpacing(10)

            v_advanced.addWidget(card_video)
            v_advanced.addWidget(card_fg)
            v_advanced.addWidget(card_audio)
            v_advanced.addWidget(card_out)

            self.scroll_advanced.setWidget(scroll_content)
            root_layout.addWidget(self.scroll_advanced, 1)

            # -------------------------------------------------------------
            # Footer: device path left, hotkeys right
            # -------------------------------------------------------------
            bottom_box = QFrame()
            bottom_box.setObjectName("bottomBar")
            b_layout = QHBoxLayout(bottom_box)
            b_layout.setContentsMargins(2, 2, 2, 0)
            b_layout.setSpacing(12)

            if self.vfio_mode:
                pci = self.streamer.hw_info.get("pci") or "auto"
                self.lbl_vdev = QLabel(
                    f"VFIO {pci} · EDID {self._edid_profile_label()} · "
                    "SDL2 audio")
                hints_text = "F Fullscreen · Tab OSD · H HDR · P Corner · Esc Exit"
            else:
                if self.streamer.pw_node:
                    audio_str = f"PipeWire ({self.streamer.pw_node})"
                else:
                    audio_str = f"ALSA ({self.streamer.adev})"
                self.lbl_vdev = QLabel(
                    f"V4L2 {self.streamer.vdev} · Audio {audio_str}")
                hints_text = "F Fullscreen · Tab OSD · M Mute · H Re-sync · Esc Exit"
            self.lbl_vdev.setObjectName("footerText")
            b_layout.addWidget(self.lbl_vdev)

            b_layout.addStretch(1)

            lbl_hints = QLabel(hints_text)
            lbl_hints.setObjectName("footerText")
            b_layout.addWidget(lbl_hints)

            root_layout.addWidget(bottom_box)

            self.card_video = card_video
            self.card_fg = card_fg
            self.card_audio = card_audio
            self.card_out = card_out
            self._apply_advanced_mode()

            # Initial status poll
            self.update_status()

            # System Tray Integration
            self._init_tray()

        @property
        def is_pro_mode(self):
            return self.is_advanced_mode

        @is_pro_mode.setter
        def is_pro_mode(self, val):
            self.is_advanced_mode = val

        def toggle_advanced_mode(self):
            self.is_advanced_mode = not self.is_advanced_mode
            self._apply_advanced_mode()
            save_settings({"ui_mode": "advanced" if self.is_advanced_mode else "simple"})

        def _apply_advanced_mode(self):
            if hasattr(self, "scroll_advanced"):
                self.scroll_advanced.setVisible(self.is_advanced_mode)
            if hasattr(self, "btn_advanced"):
                self.btn_advanced.setText("Hide Advanced ✕" if self.is_advanced_mode else "Advanced ⚙")
            target_h = (940 if self.duo_mode else 900) if self.is_advanced_mode else (680 if self.duo_mode else 640)
            self.resize(self.width(), target_h)

        toggle_pro_mode = toggle_advanced_mode
        _apply_pro_mode = _apply_advanced_mode

        def _on_quick_preset_changed(self, index: int = 0):
            pass

        def _set_launch_running(self, running: bool, mode: Optional[str] = None):
            if running:
                self._launch_mode = mode or "gaming"
                self._last_launch_mode = self._launch_mode
            else:
                self._launch_mode = None

            if hasattr(self, "btn_detached"):
                if self._launch_mode == "detached":
                    self.btn_detached.setText("Stop Detached Window")
                else:
                    self.btn_detached.setText("⧉ Launch Detached SDL Window")
                self.btn_detached.setStyle(self.btn_detached.style())

            if hasattr(self, "lbl_engine_badge"):
                if self._launch_mode == "detached":
                    self.lbl_engine_badge.setText("Detached Window")
                elif self._launch_mode == "gaming" or self.streamer.is_running:
                    self.lbl_engine_badge.setText("Userspace VFIO")
                elif self.vfio_mode:
                    self.lbl_engine_badge.setText("Userspace VFIO")
                elif self.vfio_mode:
                    self.lbl_engine_badge.setText("Userspace VFIO")
                else:
                    self.lbl_engine_badge.setText("Direct Wayland (< 2ms)")
            self._update_vdev_label()
            self._update_tray_menu()

        def _retry_audio_props(self):
            # SDL creates its sink-input only once the first HDMI audio
            # format arrives, so apply volume/mute twice.
            for delay in (1200, 3000):
                QTimer.singleShot(
                    delay, lambda: set_audio_volume(self.slider_vol.value()))
                QTimer.singleShot(delay, lambda: set_audio_mute(self.muted))

        def _check_audio_sink(self):
            hint = default_sink_issue()
            if hint:
                print(f"[!] Audio: {hint}; the SDL2 stream will be silent.")
                self._set_vfio_notice(
                    "Audio Muted",
                    hint.capitalize() + " — unmute the system output",
                    seconds=4.0)

        def _vfio_preflight(self) -> bool:
            if not self.vfio_mode:
                self._set_vfio_notice(
                    "Userspace Engine Required",
                    "Studio / virtual camera output needs the VFIO userspace engine")
                return False
            if not (os.path.isfile(self.streamer.streamer_bin) and
                    os.access(self.streamer.streamer_bin, os.X_OK)):
                folder = ("opengc570d-userd" if self.duo_mode
                          else "opengc573-userd")
                self._set_vfio_notice(
                    "Userspace Engine Missing",
                    f"Build it with: make -C tools/{folder}")
                return False
            if self._remote or ipc_probe() is not None:
                self.streamer.is_running = True
                self._launch_mode = "gaming"
                self._vfio_live = True
                return True
            if _vfio_streamer_running():
                # Userspace streamer is already active and owns the card; attach to it
                self.streamer.is_running = True
                self._launch_mode = "gaming"
                self._vfio_live = True
                return True
            return True

        # -----------------------------------------------------------------
        # Remote control channel
        # -----------------------------------------------------------------
        def _ipc_try_attach(self):
            """Attach to a running streamer (local or external)."""
            if self.duo_mode or self._ipc is None or self._ipc_shutdown:
                return
            if self._ipc.connected:
                return
            if self._ipc.try_connect():
                return
            # If a local streamer is running, or if idle watching for external streamers, retry
            if self.streamer.is_running or getattr(self, "_launch_mode", None):
                QTimer.singleShot(400, self._ipc_try_attach)
            else:
                QTimer.singleShot(2500, self, self._ipc_try_attach)

        def _on_remote_connection(self, connected: bool):
            is_local = self.streamer.is_running or bool(getattr(self, "_launch_mode", None))
            self._remote = bool(connected and not is_local)
            if connected:
                self._ipc_timer.start()
                print(f"[IPC] Attached to running streamer "
                      f"v{self._ipc.version or '?'}")
                if self._remote:
                    self._set_remote_ui(True)
                self._poll_remote_status()
            else:
                self._ipc_timer.stop()
                self._remote_status = {}
                if self._remote:
                    self._set_remote_ui(False)
                self._remote = False
                # The streamer may come back; keep watching quietly.
                if not self._ipc_shutdown and not self.streamer.is_running:
                    QTimer.singleShot(2500, self, self._ipc_try_attach)

        def _set_remote_ui(self, remote: bool):
            """Mirror the hub between local control and remote observation."""
            if remote:
                if hasattr(self, "lbl_engine_badge"):
                    self.lbl_engine_badge.setText("● Remote Session")
                if hasattr(self, "btn_detached"):
                    self.btn_detached.setEnabled(False)
                if hasattr(self, "combo_led"):
                    self.combo_led.setEnabled(True)
                self._set_vfio_notice(
                    "Remote Session",
                    "Attached to the userspace streamer that owns the card",
                    seconds=6.0)
            else:
                if hasattr(self, "lbl_engine_badge") and self.vfio_mode:
                    self.lbl_engine_badge.setText("Userspace VFIO")
                if hasattr(self, "btn_detached"):
                    self.btn_detached.setEnabled(True)
                self._set_launch_running(False)

        def _poll_remote_status(self):
            if self._ipc is not None and self._ipc.connected:
                self._ipc.request_status()

        def _on_remote_status(self, status: Dict):
            """Render a snapshot published by the remote streamer."""
            self._remote_status = status
            self._update_framegen_status(status.get("framegen") or {},
                                         bool(status.get("headless")))
            signal_info = status.get("signal") or {}
            video = status.get("video") or {}
            audio = status.get("audio") or {}
            v4l2 = status.get("v4l2") or {}

            # Always sync engine controls (volume, mute, aspect, scale) into the GUI
            self._sync_remote_controls(status, audio)

            if not self._remote or self.streamer.is_running:
                return

            if hasattr(self, "lbl_audio_fmt"):
                self.lbl_audio_fmt.setText(audio.get("label") or "No Audio")

            if signal_info.get("locked"):
                interlace = "i" if signal_info.get("interlaced") else "p"
                self.lbl_hero_signal.setText(
                    f"{signal_info.get('width', 0)} × "
                    f"{signal_info.get('height', 0)}{interlace} @ "
                    f"{signal_info.get('rate_hz', 0.0):.2f} Hz")
                parts = [
                    f"{video.get('fps', 0.0):.1f} FPS",
                    f"ring {video.get('ring_buffers', 0)}/"
                    f"{video.get('ring_channels', 0)}ch",
                ]
                if audio.get("label"):
                    parts.append(audio["label"])
                if v4l2.get("configured"):
                    parts.append(
                        f"V4L2 {v4l2.get('width', 0)}×{v4l2.get('height', 0)}")
                self.lbl_hero_meta.setText(" • ".join(parts))
            else:
                self.lbl_hero_signal.setText("No HDMI Signal")
                self.lbl_hero_meta.setText(
                    "Remote streamer running • waiting for input")

        def _sync_remote_controls(self, status: Dict, audio: Dict):
            """Mirror the engine's settings into the local controls.

            Signals are blocked so the 1 Hz refresh cannot bounce the value
            straight back to the streamer.
            """
            gain = audio.get("gain")
            if isinstance(gain, (int, float)) and not self.slider_vol.isSliderDown():
                pct = max(0, min(150, int(round(float(gain) * 100))))
                if pct != self.slider_vol.value():
                    self.slider_vol.blockSignals(True)
                    self.slider_vol.setValue(pct)
                    self.slider_vol.blockSignals(False)
                self.lbl_vol.setText(f"{pct}%")

            muted = bool(audio.get("muted"))
            if muted != self.muted:
                self._set_mute_visual(muted)

            aspect = status.get("aspect")
            if aspect in ASPECT_KEYS:
                idx = self.combo_aspect.findData(aspect)
                if idx >= 0 and idx != self.combo_aspect.currentIndex():
                    self.combo_aspect.blockSignals(True)
                    self.combo_aspect.setCurrentIndex(idx)
                    self.combo_aspect.blockSignals(False)

            scale = status.get("scale")
            if scale in SCALE_MODES:
                idx = self.combo_scale.findData(scale)
                if idx >= 0 and idx != self.combo_scale.currentIndex():
                    self.combo_scale.blockSignals(True)
                    self.combo_scale.setCurrentIndex(idx)
                    self.combo_scale.blockSignals(False)

            osd = status.get("osd") or {}
            if (isinstance(osd.get("enabled"), bool) and
                    osd["enabled"] != self.chk_osd.isChecked()):
                self.chk_osd.blockSignals(True)
                self.chk_osd.setChecked(osd["enabled"])
                self.chk_osd.blockSignals(False)

            pos = osd.get("position")
            if pos in OSD_POSITIONS:
                idx = self.combo_osd_pos.findData(pos)
                if idx >= 0 and idx != self.combo_osd_pos.currentIndex():
                    self.combo_osd_pos.blockSignals(True)
                    self.combo_osd_pos.setCurrentIndex(idx)
                    self.combo_osd_pos.blockSignals(False)

            # Experimental lsfg-vk: mirror the engine's live state.
            fg = status.get("framegen") or {}
            headless = bool(status.get("headless"))
            if hasattr(self, "combo_framegen"):
                mode = FRAMEGEN_ALIASES.get(str(fg.get("mode", "")).lower(),
                                            fg.get("mode"))
                # A headless engine can legitimately report "off" (old build or
                # started without --framegen).  Never let that silently
                # downgrade the user's saved selection in the Hub.
                if mode in FRAMEGEN_KEYS and not (headless and mode == "off"):
                    idx = self.combo_framegen.findData(mode)
                    if idx >= 0 and idx != self.combo_framegen.currentIndex():
                        self.combo_framegen.blockSignals(True)
                        self.combo_framegen.setCurrentIndex(idx)
                        self.combo_framegen.blockSignals(False)

                if hasattr(self, "chk_fg_perf"):
                    value = fg.get("perf_mode")
                    if isinstance(value, bool) and value != self.chk_fg_perf.isChecked():
                        self.chk_fg_perf.blockSignals(True)
                        self.chk_fg_perf.setChecked(value)
                        self.chk_fg_perf.blockSignals(False)

                if hasattr(self, "chk_fg_ll"):
                    value = fg.get("low_latency")
                    if isinstance(value, bool) and value != self.chk_fg_ll.isChecked():
                        self.chk_fg_ll.blockSignals(True)
                        self.chk_fg_ll.setChecked(value)
                        self.chk_fg_ll.blockSignals(False)

                if hasattr(self, "slider_flow") and not self.slider_flow.isSliderDown():
                    flow = fg.get("flow_scale")
                    if isinstance(flow, (int, float)):
                        flow_pct = max(50, min(100, int(round(float(flow) * 100))))
                        if flow_pct != self.slider_flow.value():
                            self.slider_flow.blockSignals(True)
                            self.slider_flow.setValue(flow_pct)
                            self.slider_flow.blockSignals(False)
                            self.lbl_flow.setText(f"{flow_pct}%")

        def _restore_led_after_stream(self):
            """The streamer hands off; restore the hub's saved colour."""
            if not self.vfio_mode:
                return
            if self.duo_mode:
                # The DUO engine keeps the last LED effect in hardware; there
                # is no separate standby daemon to hand the colour back to.
                return
            mode = load_settings().get("led_mode",
                                       self.saved_cfg.get("led_mode"))
            if not isinstance(mode, int) or not 0 <= mode <= 9:
                mode = 4
            # If the streamer failed before its handoff there may be no
            # standby daemon at all; start one even for the cyan default.
            if _proc_running("everlive_userd") and mode == 4:
                return
            set_vfio_led(mode)

        def apply_led(self, mode):
            if mode is None:
                return False
            mode = int(mode)
            self.saved_cfg["led_mode"] = mode
            save_settings({"led_mode": mode})
            if self._ipc is not None and self._ipc.connected:
                return self._send_ipc_led(mode)
            if self.vfio_mode:
                if self.duo_mode:
                    if self.streamer.is_running:
                        return self.streamer.apply_duo_led(mode)
                    print("[*] LED mode saved; it is applied when the DUO engine starts.")
                    return True
                ok = set_vfio_led(mode, async_apply=True)
                if self.streamer.is_running:
                    print("[*] LED colour saved; the active userspace stream keeps cyan until it exits.")
                return ok
            return write_led_mode(mode)

        def _send_ipc_led(self, mode: int) -> bool:
            """Forward a LED mode to the running streamer via IPC.

            Modes 8/9 are animations the engine renders itself; 0 turns the
            LEDs off; 1-7 are static colours sent as an explicit RGB triple.
            """
            if self._ipc is None or not self._ipc.connected:
                return False
            if mode == 8:
                return self._ipc.set_led_mode("wave")
            if mode == 9:
                return self._ipc.set_led_mode("rainbow")
            if mode == 0:
                return self._ipc.set_led_mode("off")

            color = LED_COLORS.get(mode)
            if not color or len(color) != 7:
                return False
            return self._ipc.set_led(int(color[1:3], 16),
                                     int(color[3:5], 16),
                                     int(color[5:7], 16))

        def toggle_fullscreen(self):
            if not hasattr(self, "_fullscreen_window") or self._fullscreen_window is None:
                self._fullscreen_window = FullscreenVideoWindow(self.video_canvas, self)
            if self._fullscreen_window.isVisible():
                self._fullscreen_window.close()
            else:
                self._fullscreen_window.showFullScreen()

        def toggle_stream(self):
            if self._switch_pending:
                return
            if self._launch_mode in ("studio", "detached"):
                self._switch_mode("gaming")
            elif self._launch_mode == "gaming" or self.streamer.is_running:
                self._stop_stream()
            else:
                self.start_gaming_screen(detached=False)

        def start_detached_window(self):
            if self._switch_pending:
                return
            if self._launch_mode == "detached":
                self._stop_stream()
            elif self.streamer.is_running:
                self._switch_mode("detached")
            else:
                self.start_gaming_screen(detached=True)

        def toggle_studio(self):
            if self._switch_pending:
                return
            if self._launch_mode in ("gaming", "detached"):
                self._switch_mode("studio")
            elif self._launch_mode == "studio" or self.streamer.is_running:
                self._stop_stream()
            else:
                self.start_studio()

        def _switch_mode(self, target: str):
            """Restart the streamer in the other mode without a manual stop."""
            if self._launch_mode == target or self._switch_pending:
                return
            if target == "studio" and not self.vfio_mode:
                return

            # Fast path: the engine swaps its presentation layer in place, so
            # VFIO stays bound and the DMA ring / OBS feed never stop.
            if (self._ipc is not None and self._ipc.connected and
                    self.streamer.is_running):
                mode = "gaming" if target in ("gaming", "detached") else "headless"
                if self._ipc.set_mode(mode):
                    self.streamer.headless = target == "studio"
                    self._set_launch_running(True, target)
                    self._vfio_notice_until = 0.0
                    if target == "studio":
                        self._vfio_volume_applies = 0
                        self._set_vfio_notice(
                            "Studio Mode Active",
                            "Output switched live - DMA ring and OBS feed "
                            "uninterrupted", seconds=6.0)
                    else:
                        self._set_vfio_notice(
                            "Gaming Mode Active",
                            "Output switched live - capture kept running",
                            seconds=6.0)
                    print(f"[*] Live mode switch to {target} (no restart)")
                    return

            self._switch_pending = target
            self._handoff_tries = 0
            if self._ipc is not None:
                self._ipc.close()
            self.streamer.stop()
            self._set_launch_running(False)
            if hasattr(self, "lbl_engine_badge"):
                self.lbl_engine_badge.setText("Switching Mode…")

            if hasattr(self, "btn_detached"):
                self.btn_detached.setEnabled(False)

            if target == "detached":
                self._set_vfio_notice(
                    "Opening Detached Window",
                    "Launching external SDL2 floating presentation",
                    seconds=5.0)
            else:
                self._set_vfio_notice(
                    "Switching to UI Feed",
                    "Displaying video directly inside Console Hub",
                    seconds=5.0)
            QTimer.singleShot(250, lambda: self._handoff_launch(target))

        def _handoff_launch(self, target: str):
            """Start the target mode once the old streamer released the card."""
            if self._switch_pending != target:
                return  # superseded by an explicit stop/start

            if _vfio_streamer_running() and self._handoff_tries < 8:
                self._handoff_tries += 1
                QTimer.singleShot(150, lambda: self._handoff_launch(target))
                return

            timed_out = _vfio_streamer_running()
            self._switch_pending = None
            self._handoff_tries = 0

            self._set_launch_running(False)

            if timed_out:
                print("[!] Mode switch timed out; the previous streamer still owns the card.")
                self._set_vfio_notice(
                    "Handover Failed",
                    "Previous streamer still owns the VFIO device",
                    seconds=8.0)
                return

            if target == "studio":
                self.start_studio()
            elif target == "detached":
                self.start_gaming_screen(detached=True)
            else:
                self.start_gaming_screen(detached=False)

        def _stop_stream(self):
            self._switch_pending = None
            self._handoff_tries = 0
            if hasattr(self, "_fullscreen_window") and self._fullscreen_window and self._fullscreen_window.isVisible():
                self._fullscreen_window.close()
            if self._ipc is not None:
                self._ipc.close()
            self.streamer.stop()
            self._set_launch_running(False)
            if hasattr(self, "video_canvas"):
                self.video_canvas._last_seq = 0
                self.video_canvas.update()
            if self.vfio_mode:
                QTimer.singleShot(900, self._restore_led_after_stream)

        def _set_vfio_notice(self, title: str, detail: str, seconds: float = 8.0):
            self.lbl_hero_signal.setText(title)
            self.lbl_hero_meta.setText(detail)
            self._vfio_notice_until = time.time() + seconds

        def start_gaming_screen(self, detached: bool = False):
            if self.streamer.is_running:
                return

            if self.vfio_mode:
                if not self._vfio_preflight():
                    return

                want_fullscreen = self.chk_fullscreen.isChecked()
                want_vsync = self.chk_vsync.isChecked()
                want_streamer = self.chk_streamer.isChecked()
                want_v4l2 = self.chk_v4l2.isChecked()
                v4l2_device = V4L2_DEFAULT_DEVICE if want_v4l2 else None
                aspect = self.combo_aspect.currentData() or "auto"
                if aspect not in ASPECT_KEYS:
                    aspect = "auto"
                want_osd = self.chk_osd.isChecked()
                osd_pos = self.combo_osd_pos.currentData() or "tr"
                if osd_pos not in OSD_POSITIONS:
                    osd_pos = "tr"
                audio_tap = (AUDIO_TAP_NODE
                             if self.chk_audio_tap.isChecked() else None)
                want_hdr = self.chk_hdr.isChecked()
                hdr_exposure = float(self.spin_hdr_exposure.value())
                scale_mode = self.combo_scale.currentData() or "fit"
                if scale_mode not in SCALE_MODES:
                    scale_mode = "fit"
                vol = self.slider_vol.value() / 100.0
                fg_cfg = self._framegen_config()

                if v4l2_device and not os.path.exists(v4l2_device):
                    v4l2_device = None
                    self._set_vfio_notice(
                        "Module-Free Mode",
                        f"{V4L2_DEFAULT_DEVICE} absent — running pure userspace "
                        "(SDL2 + PipeWire / Userspace SHM)",
                        seconds=4.0)

                save_settings({
                    "fullscreen": want_fullscreen,
                    "vsync": want_vsync,
                    "streamer_mode": want_streamer,
                    "v4l2_loopback": want_v4l2,
                    "aspect_ratio": aspect,
                    "osd_enabled": want_osd,
                    "osd_position": osd_pos,
                    "audio_tap": bool(audio_tap),
                    "hdr_tonemap": want_hdr,
                    "hdr_exposure": hdr_exposure,
                    "scale_mode": scale_mode,
                    **fg_cfg,
                })
                self.streamer.aspect = aspect
                success = self.streamer.start(
                    fullscreen=want_fullscreen if detached else False,
                    vsync=want_vsync,
                    streamer_mode=want_streamer,
                    v4l2_device=v4l2_device,
                    headless=not detached,
                    aspect=aspect,
                    osd=want_osd,
                    osd_pos=osd_pos,
                    audio_tap=audio_tap,
                    hdr=want_hdr,
                    hdr_exposure=hdr_exposure,
                    scale=scale_mode,
                    volume=vol,
                    framegen=fg_cfg["framegen_mode"],
                    fg_flow_scale=fg_cfg["fg_flow_scale"],
                    fg_perf_mode=fg_cfg["fg_perf_mode"],
                    fg_low_latency=fg_cfg["fg_low_latency"])
                if success:
                    mode_name = "detached" if detached else "gaming"
                    self._set_launch_running(True, mode_name)
                    self._vfio_notice_until = 0.0
                    self._vfio_volume_applies = 0
                    self._retry_audio_props()
                    self._check_audio_sink()
                    if not detached and want_fullscreen:
                        QTimer.singleShot(250, self.toggle_fullscreen)
                    QTimer.singleShot(350, self._ipc_try_attach)
                else:
                    self._set_vfio_notice(
                        "Userspace Engine Failed",
                        "See ~/.config/everlive/streamer.log for the hardware error")
                    QTimer.singleShot(300, self._restore_led_after_stream)
                return

            preset = self.combo_presets.currentText()
            vol = self.slider_vol.value() / 100.0
            is_fs = self.chk_fullscreen.isChecked()
            is_vsync = self.chk_vsync.isChecked()
            is_streamer = self.chk_streamer.isChecked()
            engine = "mpv" if self.streamer.force_mpv else "native"
            save_settings({
                "preset": preset,
                "volume": vol,
                "fullscreen": is_fs,
                "vsync": is_vsync,
                "streamer_mode": is_streamer,
                "engine": engine,
            })
            success = self.streamer.start(
                preset_name=preset, fullscreen=is_fs, volume=vol,
                vsync=is_vsync, streamer_mode=is_streamer)
            if success:
                self._set_launch_running(True, "gaming")
                set_audio_volume(self.slider_vol.value())
                QTimer.singleShot(600, lambda: set_audio_volume(self.slider_vol.value()))

        def start_studio(self):
            if self.streamer.is_running:
                return
            if not self._vfio_preflight():
                return

            want_v4l2 = self.chk_v4l2.isChecked()
            v4l2_device = V4L2_DEFAULT_DEVICE if (want_v4l2 and os.path.exists(V4L2_DEFAULT_DEVICE)) else None
            if want_v4l2 and not v4l2_device:
                self._set_vfio_notice(
                    "Module-Free Studio",
                    f"{V4L2_DEFAULT_DEVICE} absent — publishing to PipeWire & Userspace SHM",
                    seconds=4.0)

            aspect = self.combo_aspect.currentData() or "auto"
            if aspect not in ASPECT_KEYS:
                aspect = "auto"
            audio_tap = (AUDIO_TAP_NODE
                         if self.chk_audio_tap.isChecked() else None)
            want_hdr = self.chk_hdr.isChecked()
            hdr_exposure = float(self.spin_hdr_exposure.value())
            scale_mode = self.combo_scale.currentData() or "fit"
            if scale_mode not in SCALE_MODES:
                scale_mode = "fit"
            vol = self.slider_vol.value() / 100.0
            fg_cfg = self._framegen_config()
            self.streamer.aspect = aspect
            save_settings({
                "v4l2_loopback": want_v4l2,
                "aspect_ratio": aspect,
                "audio_tap": bool(audio_tap),
                "hdr_tonemap": want_hdr,
                "hdr_exposure": hdr_exposure,
                "scale_mode": scale_mode,
                **fg_cfg,
            })
            success = self.streamer.start(
                fullscreen=False,
                vsync=False,
                streamer_mode=True,
                v4l2_device=v4l2_device,
                headless=True,
                aspect=aspect,
                audio_tap=audio_tap,
                hdr=want_hdr,
                hdr_exposure=hdr_exposure,
                scale=scale_mode,
                volume=vol,
                framegen=fg_cfg["framegen_mode"],
                fg_flow_scale=fg_cfg["fg_flow_scale"],
                fg_perf_mode=fg_cfg["fg_perf_mode"],
                fg_low_latency=fg_cfg["fg_low_latency"])
            if success:
                self._set_launch_running(True, "studio")
                self._vfio_notice_until = 0.0
                self._vfio_volume_applies = 0
                self._retry_audio_props()
                QTimer.singleShot(350, self._ipc_try_attach)
                if v4l2_device:
                    self._set_vfio_notice(
                        "Studio Mode Active",
                        f"Headless capture → {V4L2_DEFAULT_DEVICE} "
                        "('EverLive Capture')",
                        seconds=6.0)
                else:
                    self._set_vfio_notice(
                        "Headless Active",
                        "Pure Userspace: PipeWire & SHM active in background",
                        seconds=6.0)
                self._check_audio_sink()
            else:
                self._set_vfio_notice(
                    "Studio Engine Failed",
                    "See ~/.config/everlive/streamer.log for the hardware error")
                QTimer.singleShot(300, self._restore_led_after_stream)

        def _duo_edid_action(self, bridge: bool):
            if not self.duo_mode:
                return
            save_settings({"duo_edid_bridge": bool(bridge)})
            self._update_vdev_label()
            if not self.streamer.is_running:
                self._set_vfio_notice(
                    "EDID Policy Saved",
                    "Start the engine to apply it", seconds=4.0)
                return
            ok = self.streamer.duo_edid_policy(bridge)
            self._set_vfio_notice(
                "EDID Policy Applied" if ok else "EDID Policy Failed",
                "Internal bridge" if bridge else "Downstream copy",
                seconds=5.0)

        def _duo_hpd_action(self):
            if not self.duo_mode or not self.streamer.is_running:
                self._set_vfio_notice(
                    "HPD Unavailable",
                    "Start the engine before pulsing hot-plug", seconds=4.0)
                return
            port = 0
            if hasattr(self, "combo_duo_input"):
                port = 1 if self.combo_duo_input.currentData() == 1 else 0
            ok = self.streamer.duo_hotplug(port)
            self._set_vfio_notice(
                "HPD Pulse Sent" if ok else "HPD Pulse Failed",
                "HDMI 1" if port == 0 else "HDMI 2", seconds=4.0)

        def on_duo_input_changed(self, index: int):
            if not self.duo_mode:
                return
            port = 1 if self.combo_duo_input.itemData(index) == 1 else 0
            self.streamer.set_duo_input(port)
            if hasattr(self, "video_canvas"):
                self.video_canvas.set_port(port)
            label = "HDMI 1" if port == 0 else "HDMI 2"
            if self.streamer.is_running:
                self._set_vfio_notice(
                    f"DUO Input: {label}",
                    "Switch applied to the running engine", seconds=4.0)
            else:
                self._set_vfio_notice(
                    f"DUO Input: {label}",
                    "The next launch will use this input", seconds=4.0)

        def on_aspect_changed(self, *_):
            aspect = self.combo_aspect.currentData() or "auto"
            if aspect not in ASPECT_KEYS:
                aspect = "auto"
            self.saved_cfg["aspect_ratio"] = aspect
            self.streamer.aspect = aspect
            save_settings({"aspect_ratio": aspect})
            if hasattr(self, "video_canvas"):
                self.video_canvas.set_aspect(aspect)
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_aspect(aspect)
            elif self.duo_mode and self.streamer.is_running:
                self.streamer.ctl("aspect", aspect)
            elif self.streamer.is_running:
                print("[*] Aspect ratio saved; restart the stream to apply it.")

        def on_scale_changed(self, *_):
            mode = self.combo_scale.currentData() or "fit"
            if mode not in SCALE_MODES:
                mode = "fit"
            self.saved_cfg["scale_mode"] = mode
            self.streamer.scale_mode = mode
            save_settings({"scale_mode": mode})
            if hasattr(self, "video_canvas"):
                self.video_canvas.set_scale_mode(mode)
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_scale(mode)
            elif self.duo_mode and self.streamer.is_running:
                self.streamer.ctl("scale", mode)
            elif self.streamer.is_running:
                print("[*] Scaling mode saved; restart the stream to apply it.")

        def on_osd_changed(self, enabled: bool):
            self.saved_cfg["osd_enabled"] = bool(enabled)
            save_settings({"osd_enabled": bool(enabled)})
            if hasattr(self, "combo_osd_pos"):
                self.combo_osd_pos.setEnabled(bool(enabled))
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_osd(bool(enabled))
            elif self.duo_mode and self.streamer.is_running:
                self.streamer.ctl("osd", "1" if enabled else "0")
            elif self.streamer.is_running:
                print("[*] OSD setting saved; use Tab/o in the gaming window "
                      "to toggle the running overlay.")

        def on_osd_pos_changed(self, *_):
            pos = self.combo_osd_pos.currentData() or "tr"
            if pos not in OSD_POSITIONS:
                pos = "tr"
            self.saved_cfg["osd_position"] = pos
            save_settings({"osd_position": pos})
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_osd_position(pos)
            elif self.duo_mode and self.streamer.is_running:
                self.streamer.ctl("osd_pos", pos)

        def on_v4l2_toggled(self, enabled: bool):
            """Persist the preference and apply it live when an engine runs."""
            save_settings({"v4l2_loopback": bool(enabled)})
            self.saved_cfg["v4l2_loopback"] = bool(enabled)

            if self._ipc is None or not self._ipc.connected:
                return  # applied by the next start

            if enabled and not os.path.exists(V4L2_DEFAULT_DEVICE):
                self._set_vfio_notice(
                    "Loopback Node Absent",
                    f"{V4L2_DEFAULT_DEVICE} not found. PipeWire and Userspace SHM (libgc570d-v4l2.so) are already active module-free.",
                    seconds=6.0)
                self.chk_v4l2.blockSignals(True)
                self.chk_v4l2.setChecked(False)
                self.chk_v4l2.blockSignals(False)
                save_settings({"v4l2_loopback": False})
                self.saved_cfg["v4l2_loopback"] = False
                return

            if self._ipc.set_v4l2(bool(enabled), V4L2_DEFAULT_DEVICE):
                self._set_vfio_notice(
                    "Virtual Camera",
                    (f"Dynamically attached to {V4L2_DEFAULT_DEVICE}"
                     if enabled else "Dynamically detached"),
                    seconds=5.0)
                print("[*] Virtual Camera "
                      f"{'attached' if enabled else 'detached'} live")
            else:
                self._set_vfio_notice(
                    "Virtual Camera",
                    "Live switch refused; it applies on the next start",
                    seconds=8.0)

        def on_hdr_toggled(self, enabled: bool):
            save_settings({"hdr_tonemap": bool(enabled)})
            self.saved_cfg["hdr_tonemap"] = bool(enabled)
            pt = getattr(self, "chk_hdr_passthrough", None)
            is_pt = pt.isChecked() if pt else False
            if hasattr(self, "spin_hdr_exposure"):
                self.spin_hdr_exposure.setEnabled(bool(enabled) and not is_pt)
            if hasattr(self, "btn_hdr_exp_minus"):
                self.btn_hdr_exp_minus.setEnabled(bool(enabled) and not is_pt)
            if hasattr(self, "btn_hdr_exp_plus"):
                self.btn_hdr_exp_plus.setEnabled(bool(enabled) and not is_pt)
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_hdr(bool(enabled),
                                  float(self.spin_hdr_exposure.value()),
                                  passthrough=is_pt)
            elif self.streamer.is_running:
                print("[*] HDR setting saved; applies on the next start.")
            self.show_toast(f"HDR Tonemap: {'Auto' if enabled else 'Bypass'}")

        def on_hdr_passthrough_toggled(self, enabled: bool):
            save_settings({"hdr_passthrough": bool(enabled)})
            self.saved_cfg["hdr_passthrough"] = bool(enabled)
            if hasattr(self, "spin_hdr_exposure"):
                self.spin_hdr_exposure.setEnabled(self.chk_hdr.isChecked() and not enabled)
            if hasattr(self, "btn_hdr_exp_minus"):
                self.btn_hdr_exp_minus.setEnabled(self.chk_hdr.isChecked() and not enabled)
            if hasattr(self, "btn_hdr_exp_plus"):
                self.btn_hdr_exp_plus.setEnabled(self.chk_hdr.isChecked() and not enabled)
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_hdr(self.chk_hdr.isChecked(),
                                  float(self.spin_hdr_exposure.value()),
                                  passthrough=bool(enabled))
            self.show_toast(f"HDR Passthrough: {'Active' if enabled else 'Off'}")

        def on_hdr_exposure_changed(self, value: float):
            save_settings({"hdr_exposure": float(value)})
            self.saved_cfg["hdr_exposure"] = float(value)
            pt = getattr(self, "chk_hdr_passthrough", None)
            is_pt = pt.isChecked() if pt else False
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_hdr(self.chk_hdr.isChecked(), float(value), passthrough=is_pt)

        def on_import_modeline(self):
            dlg = CustomModelineDialog(self)
            if dlg.exec() == QDialog.Accepted and dlg.generated_edid_path:
                custom_path = dlg.generated_edid_path
                clean_name = dlg.profile_name
                if dlg.chk_activate.isChecked():
                    self._commit_edid_selection("custom", custom_path)
                self.show_toast(f"Modeline Imported: {clean_name}")

        def on_pick_custom_led(self):
            saved_hex = self.saved_cfg.get("led_custom_hex", "#00d8d8")
            initial = QColor(saved_hex)
            color = QColorDialog.getColor(initial, self, "Select Card LED Color")
            if color.isValid():
                r, g, b = color.red(), color.green(), color.blue()
                hex_color = color.name()
                self.saved_cfg["led_custom_hex"] = hex_color
                self.saved_cfg["led_custom_rgb"] = [r, g, b]
                save_settings({"led_custom_hex": hex_color, "led_custom_rgb": [r, g, b]})
                self.btn_custom_led.setStyleSheet(
                    f"background-color: {hex_color}; border-radius: 4px;"
                )
                if self._ipc is not None and self._ipc.connected:
                    self._ipc.set_led(r, g, b)
                elif self.vfio_mode:
                    set_vfio_led(4)
                self.show_toast(f"LED Color: {hex_color.upper()}")

        def show_toast(self, message: str, duration: float = 3.0):
            """Trigger an OSD toast notification on both GUI embedded canvas and gaming window."""
            if hasattr(self, "canvas") and self.canvas is not None:
                try:
                    self.canvas.show_toast(message, duration)
                except Exception:
                    pass
            if self._ipc is not None and self._ipc.connected:
                try:
                    self._ipc.show_toast(message)
                except Exception:
                    pass

        # -----------------------------------------------------------------
        # Experimental frame generation & CAS
        # -----------------------------------------------------------------
        def _framegen_config(self) -> Dict:
            mode = self.combo_framegen.currentData() or "off"
            if mode not in FRAMEGEN_KEYS:
                mode = "off"
            return {
                "framegen_mode": mode,
                "fg_flow_scale": max(0.5, min(1.0,
                                             self.slider_flow.value() / 100.0)),
                "fg_perf_mode": self.chk_fg_perf.isChecked(),
                "fg_low_latency": self.chk_fg_ll.isChecked(),
            }

        def _apply_framegen(self, persist: bool = True):
            if not hasattr(self, "combo_framegen"):
                return
            cfg = self._framegen_config()
            self.saved_cfg.update(cfg)
            if persist:
                save_settings(cfg)
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_framegen(
                    cfg["framegen_mode"], cfg["fg_flow_scale"],
                    cfg["fg_perf_mode"], cfg["fg_low_latency"])
            else:
                self._refresh_idle_fg_status()
                if self.streamer.is_running:
                    print("[*] Frame generation saved; applies on the next start.")

        def on_framegen_changed(self, *_):
            self._apply_framegen()

        def on_fg_perf_toggled(self, _enabled: bool):
            self._apply_framegen()

        def on_fg_low_latency_toggled(self, _enabled: bool):
            self._apply_framegen()

        def on_flow_scale_changed(self, value: int):
            self.lbl_flow.setText(f"{value}%")
            # Live while dragging, persisted once the handle is released.
            self._apply_framegen(persist=not self.slider_flow.isSliderDown())

        def _set_fg_readiness(self, layer_found: bool, dll_found: bool,
                              dll_path: str = "",
                              legacy_dll_found: Optional[bool] = None):
            """Colour the readiness line green/amber with the exact fix."""
            self._fg_ready_ok = bool(layer_found and dll_found)
            if not hasattr(self, "lbl_fg_ready"):
                return
            if layer_found and dll_found:
                name = os.path.basename(dll_path) if dll_path else "lsfg-vk.dll"
                self.lbl_fg_ready.setText(
                    f"✓ LSFG-VK Ready · {name} found")
                self.lbl_fg_ready.setStyleSheet("color: #5fd38d;")
                self.lbl_fg_ready.setToolTip(dll_path)
                return

            if not layer_found:
                message = ("Missing lsfg-vk package "
                           "(sudo pacman -S lsfg-vk)")
            else:
                if legacy_dll_found is None:
                    try:
                        legacy_dll_found = bool(
                            probe_lsfg_vk().get("legacy_dll_found"))
                    except Exception:
                        legacy_dll_found = False
                if legacy_dll_found:
                    message = ("Select 'lsfg-vk' Beta in Steam "
                               "(Lossless Scaling -> Properties -> Betas)")
                else:
                    message = ("Missing Lossless Scaling in Steam "
                               "(enable 'lsfg-vk' Beta)")
            self.lbl_fg_ready.setText(f"⚠ {message}")
            self.lbl_fg_ready.setStyleSheet("color: #e0a33a;")
            self.lbl_fg_ready.setToolTip(
                "Install the lsfg-vk Vulkan layer and opt Lossless Scaling "
                "into the 'lsfg-vk' Beta in Steam, then press Re-check.")

        def _refresh_fg_readiness(self):
            """Offline probe so readiness is visible before any stream starts."""
            try:
                probe = probe_lsfg_vk()
            except Exception:
                probe = {"layer_found": False, "dll_found": False,
                         "legacy_dll_found": False, "dll_path": ""}
            self._set_fg_readiness(
                bool(probe.get("layer_found")), bool(probe.get("dll_found")),
                str(probe.get("dll_path") or ""),
                bool(probe.get("legacy_dll_found")))

        def on_fg_recheck(self):
            self._refresh_fg_readiness()
            if not (self._ipc is not None and self._ipc.connected):
                self._refresh_idle_fg_status()

        def _refresh_idle_fg_status(self):
            """Reflect the selected Frame Generation preset while idle."""
            if not hasattr(self, "lbl_fg_status"):
                return
            cfg = self._framegen_config()
            mode = cfg["framegen_mode"]
            if mode == "off":
                self.lbl_fg_status.setText(
                    "Frame generation is off (native zero-latency path).")
                return
            if not getattr(self, "_fg_ready_ok", False):
                self.lbl_fg_status.setText(
                    "lsfg-vk unavailable - presenting without 2x "
                    "(check the readiness line above).")
                return
            label = ("30→60 FPS (PS5 30 FPS dedup)" if mode == "30to60"
                     else "60→120 FPS")
            text = (f"Standby: {label} · "
                    f"Flow {int(round(cfg['fg_flow_scale'] * 100))}%")
            if cfg.get("fg_perf_mode"):
                text += " · Performance"
            if cfg.get("fg_low_latency"):
                text += " · Low-Latency"
            text += " · activates on Start Gaming Screen"
            self.lbl_fg_status.setText(text)

        def _update_framegen_status(self, fg: Dict, headless: bool = False):
            """Render the engine's live cadence in the lab card."""
            if not hasattr(self, "lbl_fg_status") or not isinstance(fg, dict):
                return
            mode = FRAMEGEN_ALIASES.get(str(fg.get("mode", "off")).lower(),
                                        fg.get("mode", "off"))
            present = float(fg.get("present_fps") or 0.0)
            output = float(fg.get("output_fps") or 0.0)
            unique = float(fg.get("unique_fps") or 0.0)

            if mode == "off":
                self.lbl_fg_status.setText(
                    "Frame generation is off (native zero-latency path).")
                return

            if not (fg.get("layer_found") and fg.get("dll_found")):
                self.lbl_fg_status.setText(
                    "lsfg-vk unavailable - presenting without 2x "
                    "(check the readiness line above).")
                return

            label = ("30→60 FPS (PS5 30 FPS dedup)" if mode == "30to60"
                     else "60→120 FPS")

            # Studio mode has no window, so no swapchain presents exist yet.
            # The mode is armed and activates on the next Gaming switch.
            if headless:
                self.lbl_fg_status.setText(
                    f"Armed: {label} · activates when the Gaming Screen opens")
                return

            if fg.get("vulkan_active") is False:
                self.lbl_fg_status.setText(
                    "Vulkan renderer unavailable - presenting through "
                    "OpenGL without frame generation.")
                return

            text = f"Live: {label} · {present:.1f} → {output:.1f} FPS"
            if unique > 0.0:
                text += f" · source {unique:.1f} FPS"
            flow = fg.get("flow_scale")
            if isinstance(flow, (int, float)):
                text += f" · Flow {int(round(float(flow) * 100))}%"
            if fg.get("perf_mode"):
                text += " · Performance"
            self.lbl_fg_status.setText(text)

        def on_preset_changed(self, *_):
            if self.vfio_mode:
                self._on_edid_profile_changed()
                return
            preset = self.combo_presets.currentText()
            save_settings({"preset": preset})
            if self.streamer.is_running:
                self.streamer.stop()
                QTimer.singleShot(150, self.start_gaming_screen)

        def _edid_profile_label(self) -> str:
            profile = self.saved_cfg.get("edid_profile", "default")
            if profile == "custom":
                path = self.saved_cfg.get("edid_custom_path", "")
                return os.path.basename(path) if path else "default"
            return profile

        def _update_vdev_label(self):
            if not self.vfio_mode or not hasattr(self, "lbl_vdev"):
                return
            pci = self.streamer.hw_info.get("pci") or "auto"
            if self.duo_mode:
                if self.streamer.v4l2_device:
                    out = (f"{self.streamer.v4l2_device}"
                           f" + {self.streamer.v4l2_device_2 or V4L2_DUO_SECOND_DEVICE}")
                else:
                    out = "PipeWire & SHM (Module-free)"
                edid = ("internal bridge"
                        if load_settings().get("duo_edid_bridge", True)
                        else "downstream copy")
                self.lbl_vdev.setText(
                    f"VFIO: {pci} • EDID: {edid} • Audio: SDL2/PipeWire "
                    f"(Userspace) • Out: {out}")
                return
            loop = (f" • Out: {self.streamer.v4l2_device}"
                    if getattr(self.streamer, "v4l2_device", None) else " • Out: Pure Userspace")
            self.lbl_vdev.setText(
                f"VFIO: {pci} • EDID: {self._edid_profile_label()} • "
                f"Audio: SDL2 (Userspace){loop}")

        def _restore_edid_selection(self):
            idx = self.combo_presets.findData(
                self.saved_cfg.get("edid_profile", "default"))
            if idx < 0:
                idx = self.combo_presets.findData("default")
            if idx >= 0:
                self.combo_presets.blockSignals(True)
                self.combo_presets.setCurrentIndex(idx)
                self.combo_presets.blockSignals(False)

        def _commit_edid_selection(self, profile: str, custom_path: str = ""):
            self.saved_cfg["edid_profile"] = profile
            settings = {"edid_profile": profile}
            if custom_path:
                self.saved_cfg["edid_custom_path"] = custom_path
                settings["edid_custom_path"] = custom_path
            save_settings(settings)

            if profile == "custom":
                idx = self.combo_presets.findData("custom")
                if idx >= 0 and custom_path:
                    self.combo_presets.blockSignals(True)
                    self.combo_presets.setItemText(
                        idx, f"Custom: {os.path.basename(custom_path)}")
                    self.combo_presets.setCurrentIndex(idx)
                    self.combo_presets.blockSignals(False)

            self._update_vdev_label()
            if self.streamer.is_running:
                print("[*] EDID profile saved; restart the Gaming Screen to apply it.")

        def _on_edid_profile_changed(self):
            key = self.combo_presets.currentData()
            if key != "custom":
                self._commit_edid_selection(key or "default")
                return

            path, _ = QFileDialog.getOpenFileName(
                self, "Load custom EDID",
                self.saved_cfg.get("edid_custom_path", ""),
                "EDID files (*.bin *.edid);;All files (*)")
            if not path:
                self._restore_edid_selection()
                return
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size < 256:
                QMessageBox.warning(
                    self, "Invalid EDID",
                    "The selected file must contain at least 256 bytes "
                    "(one base block plus one CEA-861 extension block).")
                self._restore_edid_selection()
                return
            self._commit_edid_selection("custom", path)

        def on_led_changed(self, index: int):
            mode = self.combo_led.currentData()
            if mode is not None:
                self.apply_led(mode)

        def toggle_mute(self):
            self._set_mute_visual(not self.muted)
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_mute(self.muted)
                return
            if self.duo_mode and self.streamer.is_running:
                self.streamer.ctl("mute", "1" if self.muted else "0")
            set_audio_mute(self.muted)
            if self.vfio_mode:
                # Re-arm the retry window if the sink-input is not there yet.
                self._vfio_volume_applies = 0

        def _set_mute_visual(self, muted: bool):
            self.muted = bool(muted)
            if self.muted:
                self.btn_mute.setText("Unmute")
                self.btn_mute.setStyleSheet("background-color: #36171d; color: #f87171; border: 1px solid #7f1d1d;")
            else:
                self.btn_mute.setText("Mute")
                self.btn_mute.setStyleSheet("")

        def on_volume_changed(self, val: int):
            self.lbl_vol.setText(f"{val}%")
            save_settings({"volume": val / 100.0})
            if self.muted:
                return
            if self._ipc is not None and self._ipc.connected:
                self._ipc.set_volume(val / 100.0)
                return
            if self.duo_mode and self.streamer.is_running:
                self.streamer.ctl("volume", str(val))
            set_audio_volume(val)
            if self.vfio_mode:
                self._vfio_volume_applies = 0

        def force_resync(self, *args):
            """Kernel: HPD re-sync. VFIO: restart the userspace engine."""
            if self.vfio_mode:
                was_running = self.streamer.is_running
                if was_running:
                    self.streamer.stop()
                    self._set_launch_running(False)
                restart = (self.start_studio
                           if self._last_launch_mode == "studio"
                           else self.start_gaming_screen)
                QTimer.singleShot(500 if was_running else 0, restart)
                QTimer.singleShot(3000, self._restore_led_after_stream)
                return

            if hasattr(self, "_resyncing") and self._resyncing:
                return
            self._resyncing = True
            if hasattr(self, "btn_resync"):
                self.btn_resync.setEnabled(False)
                self.btn_resync.setText("Synchronizing...")
            try:
                was_running = self.streamer.is_running
                if was_running:
                    self.streamer.stop()
                    self._set_launch_running(False)
                self.streamer.resync()
                self.update_status()
                if was_running:
                    QTimer.singleShot(600, lambda: self._wait_and_restart_after_resync(12))
            except Exception as e:
                print(f"[!] Error during HDMI re-sync: {e}")
            finally:
                QTimer.singleShot(2500, lambda: (
                    setattr(self, "_resyncing", False),
                    hasattr(self, "btn_resync") and self.btn_resync.setEnabled(True),
                    hasattr(self, "btn_resync") and self.btn_resync.setText("Re-sync Signal")
                ))

        def _wait_and_restart_after_resync(self, attempts=12):
            if self.streamer.is_running:
                return
            sig = self.streamer.get_signal_info()
            if sig.get("locked") or sig.get("cached") or attempts <= 1:
                self.start_gaming_screen()
            else:
                QTimer.singleShot(250, lambda: self._wait_and_restart_after_resync(attempts - 1))

        def _show_live(self, info):
            res = info.get("resolution", "No Signal")
            fps = info.get("framerate", "")
            res_clean = re.sub(r"(\d+)x(\d+)([pi]?)", r"\1 × \2\3", res)

            if self.vfio_mode:
                self.lbl_pill.setText(ENGINE_VFIO_TEXT)
                self.lbl_pill.setStyleSheet(
                    "background-color: #0d2318; color: #34d399; border: 1px solid #164e32;")
                self.lbl_hero_signal.setText(f"{res_clean} @ {fps}".strip())
                chroma_val = info.get("chroma") or "YUV 4:2:2"
                audio = self.streamer.audio_state()
                if audio.get("state") == "on":
                    audio_str = f"SDL2 HDMI Audio ({audio.get('detail')})"
                elif audio.get("state") in ("absent", "unavailable",
                                            "unsupported", "error"):
                    audio_str = "No HDMI audio"
                else:
                    audio_str = "SDL2 HDMI Audio"
                route = (f" • OBS: {self.streamer.v4l2_device}"
                         if getattr(self.streamer, "v4l2_device", None) else "")
                # Truthful HDR status instead of the old hardcoded
                # "Color: Pass-through" text.
                hdr_state = ("HDR Tonemap: Auto" if self.chk_hdr.isChecked()
                             else "HDR Bypass")
                self.lbl_hero_meta.setText(
                    f"Signal locked • Chroma: {chroma_val}{route} • "
                    f"{hdr_state} • {audio_str}")
                if hasattr(self, "lbl_audio_fmt"):
                    self.lbl_audio_fmt.setText(self.streamer.audio_format_label())
                return

            hdr_str = f" • {info['hdr']}" if info.get("hdr") else ""
            chroma_str = f" • {info['chroma']}" if info.get("chroma") else ""
            self.lbl_pill.setText(f"● {res} @ {fps}{chroma_str}{hdr_str}")
            self.lbl_pill.setStyleSheet("background-color: #0d2318; color: #34d399; border: 1px solid #164e32;")
            self.lbl_hero_signal.setText(f"{res_clean} @ {fps}".strip())
            hdr_val = info.get('hdr', 'SDR')
            chroma_val = info.get('chroma', 'YUV 4:2:2')
            self.lbl_hero_meta.setText(f"Signal locked • Chroma: {chroma_val} • Color: {hdr_val} • Stereo LPCM (PipeWire)")

        def _set_vfio_standby(self, info):
            self.lbl_pill.setText(ENGINE_VFIO_TEXT)
            self.lbl_pill.setStyleSheet(
                "background-color: #0d2318; color: #34d399; border: 1px solid #164e32;")
            if info.get("cached"):
                res = info.get("resolution", "No Signal")
                fps = info.get("framerate", "")
                res_clean = re.sub(r"(\d+)x(\d+)([pi]?)", r"\1 × \2\3", res)
                self.lbl_hero_signal.setText(f"Last signal: {res_clean} @ {fps}".strip())
                self.lbl_hero_meta.setText(
                    "Userspace VFIO engine ready • Waiting for HDMI input")
            else:
                self.lbl_hero_signal.setText("Ready for Gaming")
                self.lbl_hero_meta.setText(
                    "Userspace VFIO engine ready • Waiting for HDMI input")
            if hasattr(self, "lbl_audio_fmt"):
                self.lbl_audio_fmt.setText("No Audio")

        def _update_status_vfio(self):
            if self._remote and not self.streamer.is_running:
                # The remote streamer owns these values; its 1 Hz snapshot
                # already drives the hero labels through _on_remote_status.
                return
            if hasattr(self, "lbl_audio_fmt"):
                self.lbl_audio_fmt.setText(
                    self.streamer.audio_format_label()
                    if self.streamer.is_running else "No Audio")
            if self.streamer.is_running:
                if not self.streamer.poll():
                    self._set_launch_running(False)
                    QTimer.singleShot(900, self._restore_led_after_stream)
                    if not self.streamer._vfio_saw_output:
                        self._set_vfio_notice(
                            "Userspace Engine Stopped",
                            "See ~/.config/everlive/streamer.log for the hardware error")
                    else:
                        self._set_vfio_standby(self.streamer.get_signal_info())
                    return

                info = self.streamer.get_signal_info()
                self._live = info
                if info.get("locked"):
                    # Re-apply a few times: the SDL sink-input appears only after
                    # the first audio period, and the status poll is cheap.
                    if self._vfio_volume_applies < 4:
                        if self.vfio_mode:
                            set_audio_stream_unity()
                        else:
                            set_audio_volume(self.slider_vol.value())
                        set_audio_mute(self.muted)
                        self._vfio_volume_applies += 1
                    self._show_live(info)
                elif info.get("starting"):
                    self.lbl_hero_signal.setText("Starting Userspace Engine…")
                    self.lbl_hero_meta.setText(
                        "VFIO DMA ring initialising • SDL2 video/audio")
                else:
                    self.lbl_hero_signal.setText("No HDMI Signal")
                    self.lbl_hero_meta.setText(
                        "Userspace engine running • waiting for stable input")
                return

            info = self.streamer.get_signal_info()
            self._live = info
            if time.time() < self._vfio_notice_until:
                return
            self._set_vfio_standby(info)

        def update_status(self):
            if self.vfio_mode:
                self._update_status_vfio()
                return

            # While the gaming screen is open, only read the cached driver format and HDR control.
            # query-dv-timings hits the HDMI receiver and stalls the stream.
            if self.streamer.is_running:
                if not self.streamer.poll():
                    # Window was closed by user
                    self._set_launch_running(False)
                    return
                live = getattr(self, "_live", None)
                active_res, active_fps = self.streamer.get_active_format()
                hdr = self.streamer.hdr_label(fallback=False)
                chroma = self.streamer.chroma_label()
                if live and live.get("locked"):
                    updated = False
                    live = dict(live)
                    if active_res and active_res != live.get("resolution"):
                        live["resolution"] = active_res
                        updated = True
                    if active_fps and active_fps != live.get("framerate"):
                        live["framerate"] = active_fps
                        updated = True
                    if hdr and hdr != live.get("hdr"):
                        live["hdr"] = hdr
                        updated = True
                    if chroma and chroma != live.get("chroma"):
                        live["chroma"] = chroma
                        updated = True
                    if updated:
                        self._live = live
                        self._show_live(live)
                return

            info = self.streamer.get_signal_info()
            self._live = info
            if hasattr(self, "lbl_audio_fmt"):
                self.lbl_audio_fmt.setText(
                    info.get("audio", "No Audio")
                    if info.get("locked") else "No Audio")
            if info["locked"]:
                self._show_live(info)
            else:
                self.lbl_pill.setText("○ Standby")
                self.lbl_pill.setStyleSheet("background-color: #1a202c; color: #94a3b8; border: 1px solid #2d3748;")
                self.lbl_hero_signal.setText("No HDMI Signal")
                self.lbl_hero_meta.setText("Waiting for input signal (e.g. power on console or PC)...")

        def _init_tray(self):
            if not QSystemTrayIcon.isSystemTrayAvailable():
                self.tray_icon = None
                return

            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "everlive.png")
            icon = QIcon(icon_path) if os.path.isfile(icon_path) else self.windowIcon()

            self.tray_icon = QSystemTrayIcon(icon, self)
            self.tray_icon.setToolTip("EverLive - Console Hub")

            self.tray_menu = QMenu()
            self.tray_menu.setStyleSheet("""
                QMenu {
                    background-color: #12151f;
                    color: #e2e8f0;
                    border: 1px solid #1f2537;
                    padding: 4px;
                    font-size: 13px;
                }
                QMenu::item {
                    padding: 6px 20px;
                    border-radius: 4px;
                }
                QMenu::item:selected {
                    background-color: #2563eb;
                    color: #ffffff;
                }
                QMenu::separator {
                    height: 1px;
                    background-color: #1f2537;
                    margin: 4px 8px;
                }
            """)

            action_show = QAction("Open Console Hub", self)
            action_show.triggered.connect(self._restore_from_tray)
            self.tray_menu.addAction(action_show)

            self.tray_menu.addSeparator()

            self.tray_act_stream = QAction("Start Gaming Screen", self)
            self.tray_act_stream.triggered.connect(self.toggle_stream)
            self.tray_menu.addAction(self.tray_act_stream)

            self.tray_act_studio = QAction("Start Studio / Headless (OBS)", self)
            self.tray_act_studio.triggered.connect(self.toggle_studio)
            self.tray_menu.addAction(self.tray_act_studio)

            self.tray_menu.addSeparator()

            action_quit = QAction("Quit EverLive", self)
            action_quit.triggered.connect(self._force_quit)
            self.tray_menu.addAction(action_quit)

            self.tray_icon.setContextMenu(self.tray_menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.show()

        def _update_tray_menu(self):
            if not getattr(self, "tray_act_stream", None) or not getattr(self, "tray_act_studio", None):
                return
            if self._launch_mode == "studio":
                self.tray_act_stream.setText("Switch to Gaming Screen")
                self.tray_act_studio.setText("Stop Studio")
            elif self._launch_mode == "gaming":
                self.tray_act_stream.setText("Stop Gaming Screen")
                self.tray_act_studio.setText("Switch to Studio (Headless)")
            else:
                self.tray_act_stream.setText("Start Gaming Screen")
                self.tray_act_studio.setText("Start Studio / Headless (OBS)")

        def _restore_from_tray(self):
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def _on_tray_activated(self, reason):
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
                if self.isVisible() and not self.isMinimized():
                    self.hide()
                else:
                    self._restore_from_tray()

        def _force_quit(self):
            self._switch_pending = None
            self._ipc_shutdown = True
            if self._ipc is not None:
                self._ipc.close()
            was_running = self.streamer.is_running
            self.streamer.stop()
            if self.vfio_mode and was_running:
                self._restore_led_after_stream()
            if hasattr(self, "tray_icon") and self.tray_icon:
                self.tray_icon.hide()
            QApplication.quit()

        def closeEvent(self, event):
            # Do not let a pending handover timer relaunch the streamer.
            self._switch_pending = None
            was_running = self.streamer.is_running

            # Close to Tray: clicking [X] hides the window into the system tray
            if getattr(self, "chk_tray", None) and self.chk_tray.isChecked() and getattr(self, "tray_icon", None):
                event.ignore()
                self.hide()
                if not getattr(self, "_tray_notified", False):
                    self._tray_notified = True
                    self.tray_icon.showMessage(
                        "EverLive hidden to tray",
                        "The Console Hub is minimized to the system tray. Click the tray icon to reopen.",
                        QSystemTrayIcon.Information,
                        3000)
                return

            self._ipc_shutdown = True
            if self._ipc is not None:
                self._ipc.close()
            self.streamer.stop()
            if self.vfio_mode and was_running:
                self._restore_led_after_stream()
            if hasattr(self, "tray_icon") and self.tray_icon:
                self.tray_icon.hide()
            event.accept()


def main():
    parser = argparse.ArgumentParser(description="EverLive - Live Gamer 4K Console Gaming Screen")
    parser.add_argument("-f", "--fullscreen", action="store_true", help="Launch directly in fullscreen")
    parser.add_argument("-p", "--preset", default="Auto (HDMI Signal)", choices=list(PRESETS.keys()), help="Resolution preset")
    parser.add_argument("--vsync", action="store_true", help="Enable VSync for smooth tearing-free presentation")
    parser.add_argument("-s", "--streamer-mode", "--clean", action="store_true", help="Streamer Mode: Clean feed without HUD or controls")
    parser.add_argument("--cli", action="store_true", help="Launch directly in CLI mode without Qt GUI")
    parser.add_argument("--mpv", action="store_true", help="Force mpv backend instead of everlive_display")
    parser.add_argument("--led", type=str, default=None,
                        help="Change GC573 LED lighting immediately (0-7 or off/blue/green/red/cyan/magenta/yellow/white)")
    parser.add_argument("-v", "--volume", type=float, default=1.0, help="Audio volume (0.0 to 1.5)")
    parser.add_argument("-d", "--device", default=None, help="V4L2 video device (e.g. /dev/video0, default: auto-detect)")
    parser.add_argument("-a", "--audio", default=None, help="ALSA audio device (e.g. hw:CARD=A4K,DEV=0, default: auto-detect)")
    parser.add_argument("--aspect", choices=["auto", "16:9", "4:3", "stretch"], default="auto", help="Aspect ratio override")
    parser.add_argument("--v4l2", "--loopback", dest="v4l2", nargs="?",
                        const=V4L2_DEFAULT_DEVICE, default=None,
                        help="Publish native YUYV frames to a v4l2loopback device "
                             f"(default: {V4L2_DEFAULT_DEVICE})")
    parser.add_argument("--headless", "--studio", dest="headless", action="store_true",
                        help="Capture and publish/play without an SDL window")
    parser.add_argument("--sdl", action="store_true",
                        help="Open the interactive SDL2 gaming window")
    parser.add_argument("--port", type=int, choices=[1, 2], default=None,
                        help="Select initial HDMI input on Live Gamer DUO (1 or 2)")
    parser.add_argument("--pip", action="store_true",
                        help="Enable picture-in-picture on Live Gamer DUO")
    parser.add_argument("--v4l2-2", "--loopback-2", dest="v4l2_2", default=None,
                        help="Secondary v4l2loopback device for Live Gamer DUO HDMI 2")
    parser.add_argument("--copy-edid", action="store_true",
                        help="Copy downstream EDID on Live Gamer DUO")
    parser.add_argument("--scale", choices=["fit", "integer"], default=None,
                        help="Scaling mode override (fit or integer)")
    parser.add_argument("--tray", "--minimized", dest="tray", action="store_true",
                        help="Launch Console Hub directly hidden into the system tray")
    parser.add_argument("--wave", action="store_true",
                        help="Set GC573 LED to multi-zone rainbow wave")
    parser.add_argument("--rainbow", action="store_true",
                        help="Set GC573 LED to whole-card spectrum cycle")
    parser.add_argument("--no-loopback", "--skip-loopback", dest="no_loopback", action="store_true",
                        help="Run strictly module-free without v4l2loopback")
    args = parser.parse_args()

    if args.no_loopback:
        save_settings({"v4l2_loopback": False})
    if args.port in (1, 2):
        save_settings({"duo_input": args.port - 1})
    if args.copy_edid:
        save_settings({"duo_edid_bridge": False})
    if args.scale in SCALE_MODES:
        save_settings({"scale_mode": args.scale})

    if args.wave:
        args.led = "wave"
    elif args.rainbow:
        args.led = "rainbow"

    # Headless and virtual-camera launches are streamer-only unless tray is requested.
    if (args.headless or args.v4l2) and not args.tray:
        args.cli = True

    if args.led is not None:
        key = args.led.lower().strip()
        val = LED_NAME_MAP.get(key)
        if val is None:
            try:
                val = int(key)
            except ValueError:
                pass
        if val is not None and 0 <= val <= 9:
            hw = detect_gc573_driver()
            model = hw.get("model", MODEL_GC573)
            model_name = "GC570D" if model == MODEL_GC570D else "GC573"
            if hw.get("mode") == "vfio":
                save_settings({"led_mode": val})
                is_launching = args.cli or args.tray or any(
                    arg in sys.argv for arg in [
                        "-f", "--fullscreen", "-p", "--preset",
                        "--headless", "--v4l2", "--loopback",
                        "--streamer-mode", "-s"
                    ]
                )
                if model == MODEL_GC570D:
                    if _vfio_streamer_running():
                        if set_gc570d_led(val):
                            print(f"[+] {model_name} LED effect set to mode {val}.")
                        else:
                            print(f"[*] {model_name} streamer is running; LED mode {val} saved for the next session.")
                    else:
                        print(f"[*] {model_name} LED mode {val} saved; it is applied by the engine.")
                elif _vfio_streamer_running():
                    print(f"[*] Userspace streamer is running; LED mode {val} saved for the next session.")
                elif not is_launching:
                    if set_vfio_led(val):
                        print(f"[+] GC573 LED lighting set to mode {val} via everlive_userd.")
                    else:
                        print(f"[!] Failed to set GC573 LED to mode {val} via everlive_userd.")
                else:
                    print(f"[+] GC573 LED lighting set to mode {val} for stream.")
            else:
                if write_led_mode(val):
                    print(f"[+] {model_name} LED lighting set to mode {val}.")
                else:
                    print(f"[!] Failed to set {model_name} LED to mode {val}.")
        else:
            print(f"[!] Invalid LED mode '{args.led}'. Valid: {list(LED_NAME_MAP.keys())}")
        if not args.cli and not args.tray and not any(arg in sys.argv for arg in ["-f", "--fullscreen", "-p", "--preset", "--headless", "--v4l2", "--loopback"]):
            sys.exit(0)

    streamer = GameStreamer(vdev=args.device, adev=args.audio, force_mpv=args.mpv)
    streamer.aspect = args.aspect
    if args.fullscreen or args.streamer_mode:
        streamer._cli_autostart = True

    if streamer.vfio_mode:
        print(f"[*] {streamer.model_label}: Userspace VFIO engine "
              f"(PCI {streamer.hw_info.get('pci') or 'auto'}), "
              "no V4L2/ALSA device required.")
        if args.device or args.audio or args.mpv:
            print("[*] Note: --device/--audio/--mpv apply only to the kernel V4L2 path.")
    else:
        print(f"[*] {streamer.model_label} Hardware: Video={streamer.vdev}, "
              f"Audio={streamer.adev} "
              f"(driver={streamer.hw_info.get('driver') or 'unknown'})")

    # Graceful shutdown on Ctrl+C
    def sig_handler(sig, frame):
        streamer.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    if args.cli or not HAS_QT:
        print("[*] Launching Gaming Screen in low-latency mode...")
        if streamer.vfio_mode:
            engine = ("opengc570d-userd" if streamer.duo_mode
                      else "gc573_user_streamer")
            print(f"[*] Userspace VFIO engine: video and HDMI audio run inside {engine}.")
        else:
            info = streamer.get_signal_info()
            chroma_disp = f" [{info['chroma']}]" if info.get('chroma') else ""
            print(f"[*] Signal: {info['resolution']} {info['framerate']}{chroma_disp}".strip())
            if not info["locked"]:
                print(f"[!] Warning: No active HDMI signal detected on {streamer.model_label}.")
                print("    Please check if the video source is powered on and sending video.")
        cli_settings = load_settings()
        if not streamer.start(preset_name=args.preset, fullscreen=args.fullscreen, volume=args.volume, vsync=args.vsync, streamer_mode=args.streamer_mode, v4l2_device=args.v4l2, headless=args.headless, aspect=args.aspect, framegen=cli_settings.get("framegen_mode", "off"), fg_flow_scale=cli_settings.get("fg_flow_scale", 1.0), fg_perf_mode=cli_settings.get("fg_perf_mode", False), fg_low_latency=cli_settings.get("fg_low_latency", True)):
            sys.exit(1)
        if streamer.vfio_mode:
            # The SDL2 sink-input only exists once audio starts flowing.
            # Ensure host PipeWire stream is at 100% unity gain so --volume
            # applies cleanly without double-scaling.
            time.sleep(1.5)
            set_audio_stream_unity()
        print("[+] Window opened. Press 'q' in window or Ctrl+C to exit.")
        while streamer.poll():
            time.sleep(0.5)
        streamer.stop()
        if streamer.vfio_mode and not streamer.duo_mode:
            saved_led = load_settings().get("led_mode")
            mode = saved_led if isinstance(saved_led, int) and 0 <= saved_led <= 9 else 4
            if not (_proc_running("everlive_userd") and mode == 4):
                set_vfio_led(mode)
    else:
        app = QApplication(sys.argv)
        app.setApplicationName("EverLive")

        # Only register desktop file with host portal if a matching .desktop file is installed
        for candidate in ("everlive-experimental", "everlive"):
            user_path = os.path.expanduser(f"~/.local/share/applications/{candidate}.desktop")
            sys_path = f"/usr/share/applications/{candidate}.desktop"
            if os.path.isfile(user_path) or os.path.isfile(sys_path):
                app.setDesktopFileName(candidate)
                break

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "everlive.png")
        if os.path.isfile(icon_path):
            app.setWindowIcon(QIcon(icon_path))
        window = MainWindow(streamer, vsync=args.vsync, streamer_mode=args.streamer_mode,
                            headless_start=args.headless, v4l2_device=args.v4l2)
        if not args.tray:
            window.show()
        ret = app.exec()

        # Clean teardown for PySide6 on Python 3.14 to prevent double-free SEGV in Shiboken atexit
        try:
            if hasattr(window, "tray_icon") and window.tray_icon:
                window.tray_icon.hide()
                window.tray_icon.deleteLater()
            window.deleteLater()
            app.processEvents()
        except Exception:
            pass
        os._exit(ret)


if __name__ == "__main__":
    main()
