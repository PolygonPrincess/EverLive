# EverLive Implementation Plan - Security, Performance, Reliability & Features

## Phase 1: Security Fixes (Critical)

### S1: Replace Hand-rolled JSON Parser
- **Files**: `tools/opengc573-userd/gc573_user_ipc.c`
- **Action**: Integrate `jsmn` (single header, ~200 LOC, MIT licensed)
- **Changes**: Replace `json_find_value`, `json_get_string`, `json_get_long`, `json_get_double`, `json_get_bool` with jsmn-based parser
- **Limits**: Max 128 tokens, max nesting depth 16, max total JSON size 16KB

### S2: Path Validation for EDID and V4L2 Device Paths
- **Files**: `tools/opengc573-userd/gc573_user_streamer.c`, `tools/opengc573-userd/gc573_user_ipc.c`
- **Action**: 
  - EDID: Validate path is under `~/.config/everlive/edids/` or `/usr/share/everlive/edids/`
  - V4L2: Allowlist `/dev/video[0-9]+`, verify major:minor matches v4l2loopback
- **Method**: Use `openat()` with `O_NOFOLLOW`, `fstat()` to verify character device

### S3: Replace `system()` Calls
- **Files**: `tools/opengc573-userd/gc573_user_streamer.c` (stop_led_daemon)
- **Action**: Replace with direct `pkill` via `/proc` scan (pattern already used in `everlive_viewer.py:_proc_running`)

### S4: DMA Stall Watchdog with Auto-Recovery
- **Files**: `tools/opengc573-userd/gc573_user_video.c`, `tools/opengc573-userd/gc573_user_streamer.c`
- **Action**: 
  - Add watchdog thread monitoring `last_complete_ms`
  - On stall > 2s: reset FPGA DMA, re-arm channels, log recovery
  - Max 3 recovery attempts before fatal error

---

## Phase 2: Performance Improvements (High)

### P1: Split Monolithic Streamer
- **Files**: `tools/opengc573-userd/gc573_user_streamer.c` (4000+ lines)
- **New Files**:
  - `capture_thread.c/h` - DMA polling, signal detection, framegen input
  - `render_thread.c/h` - SDL window, OpenGL/Vulkan, OSD, presentation
  - `audio_thread.c/h` - Audio DMA, bitstream decode, SDL audio, PipeWire tap
  - `control_thread.c/h` - IPC command processing, mode switches, LED
  - `streamer_main.c` - Thread coordination, lifecycle, shared state
- **Communication**: Lock-free rings (SPSC) between threads, condition variables for wait

### P2: SIMD Fast Path for HDR Tonemapping
- **Files**: `tools/opengc573-userd/gc573_user_hdr.c`
- **Action**: 
  - Add NEON (ARM64) and SSE2 (x86_64) implementations
  - Process 4 pixels (8 bytes YUYV) per iteration
  - Runtime CPU feature detection (`getauxval` / `cpuid`)
  - Fallback to scalar for unsupported platforms

### P3: Move Bitstream Decode to Dedicated Thread
- **Files**: `tools/opengc573-userd/gc573_user_audio.c`, `tools/opengc573-userd/gc573_user_streamer.c`
- **Action**: 
  - Audio worker thread already exists; extend to handle decode
  - Lock-free ring: capture thread → decode thread → render thread
  - Decode thread feeds PCM to SDL audio callback via ring buffer

---

## Phase 3: Reliability Improvements (High)

### R1: GitHub Actions CI with AddressSanitizer
- **Files**: `.github/workflows/ci.yml` (new)
- **Action**: 
  - Build with `-fsanitize=address,undefined -fno-omit-frame-pointer`
  - Run `make -C tests asan test` on every push/PR
  - Test on ubuntu-latest (x86_64) and arm64 runner

### R2: EDID Write Verify-Readback
- **Files**: `tools/opengc573-userd/gc573_user_edid.c`, `tools/opengc570d-userd/src/gc570d-receiver.c`
- **Action**: After I2C write, read back 256 bytes and compare; retry 3x on mismatch

### R3: Signal Change Detection via Interrupt
- **Files**: `tools/opengc573-userd/gc573_user_it6805.c`, `tools/opengc573-userd/gc573_user_streamer.c`
- **Action**: 
  - Enable IT6805 AVI/SCDT change interrupts
  - Fallback to polling if interrupt not available
  - Reduce `STATUS_POLL_STREAM_MS` from 50ms to 200ms when interrupt active

---

## Phase 4: Modular Hardware Abstraction (libeverlive-core)

### Core Library Structure
```
tools/libeverlive-core/
├── everlive_hw.h          # Unified hardware abstraction interface
├── everlive_vfio.c/h      # VFIO container/group/device/DMA (shared)
├── everlive_dma_ring.c/h  # Generic scatter-gather ring (shared)
├── everlive_edid.c/h      # EDID parse/validate/unify (shared)
├── everlive_audio.c/h     # LPCM + IEC 61937 decode (shared)
├── everlive_hdr.c/h       # PQ/HLG → SDR LUT + chroma matrix (shared)
├── everlive_framegen.c/h  # LSFG-VK glue + deduplicator (shared)
├── everlive_ipc.c/h       # JSON-RPC socket + command queue (shared)
├── gc573/
│   ├── gc573_hw.c/h       # IT6805/IT6664/FPGA registers
│   ├── gc573_it6805.c/h
│   ├── gc573_it6664.c/h
│   └── gc573_fpga.c/h
├── gc570d/
│   ├── gc570d_hw.c/h      # IT6805_1/Splitter/IT6802 registers
│   ├── gc570d_splitter.c/h
│   ├── gc570d_receiver.c/h
│   └── gc570d_it6802.c/h
└── Makefile               # Builds libeverlive_core.so + everlive-ctl
```

### Migration Strategy
1. Extract shared code from both engines into `libeverlive-core`
2. Create `everlive_hw_ops` vtable for card-specific operations
3. Update GC573 and GC570D streamers to link against `libeverlive_core.so`
4. Keep `everlive-ctl` as diagnostic tool using the library

---

## Phase 5: LD_PRELOAD for GC573 (Feature)

### Port `libgc570d-v4l2.so` to GC573
- **Files**: 
  - New: `tools/opengc573-userd/gc573_v4l2_preload.c`
  - Existing reference: `tools/opengc570d-userd/src/gc570d-v4l2-preload.c`
- **Action**: 
  - Intercept `open`, `ioctl`, `mmap`, `close`, `poll` for `/dev/video*`
  - Forward to GC573 streamer via shared memory (`/dev/shm/everlive-gc573-<pid>`)
  - Support VIDIOC_QUERYCAP, VIDIOC_G_FMT, VIDIOC_S_FMT, VIDIOC_REQBUFS, VIDIOC_QUERYBUF, VIDIOC_QBUF, VIDIOC_DQBUF, VIDIOC_STREAMON/OFF
  - Install to `/usr/local/lib/libeverlive-v4l2.so`

---

## Phase 6: Flatpak & AppImage (Feature)

### Flatpak
- **Files**: `flatpak/com.everlive.EverLive.yaml` (new)
- **Action**:
  - Base: `org.freedesktop.Platform//24.08` + `org.freedesktop.Sdk//24.08`
  - Build: Python 3.12 + PySide6 + dependencies
  - Permissions: `--device=dri`, `--socket=wayland`, `--socket=pipewire`, `--filesystem=xdg-run/everlive:create`
  - Expose: `everlive` command, `.desktop` file, icon

### AppImage
- **Files**: `packaging/AppImage/everlive.AppDir/` (new), `packaging/build_appimage.sh` (new)
- **Action**:
  - Use `appimagetool` + `linuxdeploy` + `linuxdeploy-plugin-qt`
  - Bundle: Python, PySide6, streamer binaries, libraries
  - Runtime: FUSE mount, extract to `/tmp/.mount_everliveXXXXXX`
  - Desktop integration via `AppRun` wrapper

---

## Phase 7: GUI Enhancements (Feature)

### RGB Color Picker
- **Files**: `tools/everlive/everlive_viewer.py`
- **Action**:
  - Replace LED mode dropdown with `QColorDialog` for custom colors
  - Keep predefined modes (wave, rainbow, off) as quick presets
  - Send RGB values via IPC `led.set` method

### OSD Toast (Sync Lock Notification)
- **Files**: `tools/opengc573-userd/gc573_user_streamer.c`, `tools/everlive/everlive_viewer.py`
- **Action**:
  - Add toast overlay system (separate from stats OSD)
  - Trigger on: signal lock, mode change, HDR type change, framegen mode change
  - 3-second fade-in/out, bottom-center, semi-transparent
  - IPC event `toast.show` for GUI-initiated toasts

### Custom Modeline Helper
- **Files**: `tools/everlive/everlive_viewer.py`, `tools/opengc573-userd/gc573_user_edid.c`
- **Action**:
  - GUI: "Import Custom Modeline" dialog with text area + validation
  - Parse CVT/GTF modeline format, convert to EDID detailed timing
  - Validate: pixel clock, h/v totals, sync polarities, checksum
  - Save as `.bin` in `~/.config/everlive/edids/custom/`
  - CLI: `--edid custom:<name>` to load

---

## Phase 8: HDR Passthrough to HDR Display (Feature)

### Investigation & Implementation
- **Files**: `tools/opengc573-userd/gc573_user_hdr.c`, `tools/opengc573-userd/gc573_user_streamer.c`
- **Action**:
  - Detect HDR-capable display via EDID (EOTF support: PQ/HLG)
  - SDL: Create window with `SDL_WINDOW_ALLOW_HIGHDPI` + Vulkan renderer
  - Vulkan: Use `VK_EXT_hdr_metadata` + `VK_COLOR_SPACE_HDR10_ST2084_EXT`
  - Pipeline: YUYV → PQ/HLG linear → BT.2020 → HDR swapchain (bypass tonemap)
  - Fallback: Tonemap to SDR if HDR presentation fails
  - Toggle: `hdr.passthrough` IPC method + GUI checkbox

---

## Dependencies & Order

```
Phase 1 (Security) ──► Phase 2 (Performance) ──► Phase 3 (Reliability)
       │                     │                        │
       ▼                     ▼                        ▼
Phase 4 (libeverlive-core) ◄──┴────────────────────────┘
       │
       ├──► Phase 5 (LD_PRELOAD)
       ├──► Phase 6 (Flatpak/AppImage)
       ├──► Phase 7 (GUI: RGB picker, Toast, Modeline)
       └──► Phase 8 (HDR Passthrough)
```

## Testing Checklist

- [ ] All existing tests pass (`make -C tests test`)
- [ ] ASAN clean on all binaries
- [ ] GC573: 1440p120 HDR, 4K60 HDR, 1080p240
- [ ] GC570D: Dual HDMI, HDMI 2 virtual camera
- [ ] Frame generation: 60→120, 30→60
- [ ] LD_PRELOAD: Discord, OBS, Chrome detect camera
- [ ] Flatpak: Runs on clean VM, portal permissions work
- [ ] AppImage: Runs on Ubuntu 22.04+, Fedora 39+, Arch
- [ ] HDR passthrough: Works on HDR display (if available)
- [ ] Custom EDID + modeline: Validated and loads correctly