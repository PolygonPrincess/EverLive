#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# EverLive AppImage Build Script
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APPDIR="${SCRIPT_DIR}/AppImage/everlive.AppDir"
OUTPUT_DIR="${SCRIPT_DIR}/dist"
APPDIR_ONLY=0

show_help() {
    cat << EOF
EverLive AppImage Builder

Usage: $0 [options]

Options:
  --appdir-only       Populate the AppDir directory without invoking appimagetool
  --clean             Clean previous build artifacts before packaging
  -h, --help          Show this help message and exit
EOF
}

for arg in "$@"; do
    case "$arg" in
        --appdir-only) APPDIR_ONLY=1 ;;
        --clean)
            echo "[*] Cleaning build artifacts..."
            rm -rf "${APPDIR}" "${OUTPUT_DIR}"
            make -C "${REPO_ROOT}/tools/opengc573-userd" clean || true
            make -C "${REPO_ROOT}/tools/opengc570d-userd" clean || true
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            show_help
            exit 1
            ;;
    esac
done

echo "=========================================================="
echo "          Building EverLive Standalone AppImage          "
echo "=========================================================="

echo "[1/4] Compiling EverLive userspace capture engines..."
make -C "${REPO_ROOT}/tools/opengc573-userd" -j"$(nproc)"
make -C "${REPO_ROOT}/tools/opengc570d-userd" CC=gcc -j"$(nproc)"

echo "[2/4] Preparing AppDir directory structure..."
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/lib"
mkdir -p "${APPDIR}/usr/lib/everlive"
mkdir -p "${APPDIR}/usr/share/applications"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/scalable/apps"
mkdir -p "${OUTPUT_DIR}"

# Install userspace executables
install -m 755 "${REPO_ROOT}/tools/opengc573-userd/gc573_user_streamer" "${APPDIR}/usr/bin/"
install -m 755 "${REPO_ROOT}/tools/opengc573-userd/everlive_userd" "${APPDIR}/usr/bin/"
install -m 755 "${REPO_ROOT}/tools/opengc573-userd/gc573_user_diag" "${APPDIR}/usr/bin/"
install -m 755 "${REPO_ROOT}/tools/opengc570d-userd/opengc570d-userd" "${APPDIR}/usr/bin/"
install -m 755 "${REPO_ROOT}/tools/opengc570d-userd/gc570d-ctl" "${APPDIR}/usr/bin/"

# Install preload virtual camera libraries
install -m 755 "${REPO_ROOT}/tools/opengc573-userd/libgc573-v4l2.so" "${APPDIR}/usr/lib/"
ln -sf libgc573-v4l2.so "${APPDIR}/usr/lib/libeverlive-v4l2.so"
install -m 755 "${REPO_ROOT}/tools/opengc570d-userd/libgc570d-v4l2.so" "${APPDIR}/usr/lib/"

# Install Python GUI viewer
cp -r "${REPO_ROOT}/tools/everlive/"* "${APPDIR}/usr/lib/everlive/"

# Install desktop entry and icons
cat << 'EOF' > "${APPDIR}/usr/share/applications/everlive.desktop"
[Desktop Entry]
Name=EverLive
GenericName=Console Gaming & Capture Hub
Comment=Userspace VFIO capture, lossless scaling frame generation, and streaming hub
Exec=everlive
Icon=everlive
Terminal=false
Type=Application
Categories=Game;AudioVideo;Video;
Keywords=EverLive;Capture;HDMI;AVerMedia;GC573;GC570D;Gaming;Console;VFIO;FrameGen;
StartupWMClass=EverLive
EOF

cp "${APPDIR}/usr/share/applications/everlive.desktop" "${APPDIR}/everlive.desktop"

# Icons
if [ -f "${REPO_ROOT}/tools/everlive/icons/scalable/apps/everlive.png" ]; then
    cp "${REPO_ROOT}/tools/everlive/icons/scalable/apps/everlive.png" "${APPDIR}/usr/share/icons/hicolor/scalable/apps/everlive.png"
    cp "${REPO_ROOT}/tools/everlive/icons/scalable/apps/everlive.png" "${APPDIR}/everlive.png"
    cp "${REPO_ROOT}/tools/everlive/icons/scalable/apps/everlive.png" "${APPDIR}/.DirIcon"
elif [ -f "${REPO_ROOT}/tools/everlive/everlive.png" ]; then
    cp "${REPO_ROOT}/tools/everlive/everlive.png" "${APPDIR}/everlive.png"
    cp "${REPO_ROOT}/tools/everlive/everlive.png" "${APPDIR}/.DirIcon"
fi

# Copy AppRun entrypoint
cp -f "${SCRIPT_DIR}/AppImage/AppRun" "${APPDIR}/AppRun"
chmod +x "${APPDIR}/AppRun"

echo "[3/4] Validating AppDir payload..."
test -x "${APPDIR}/AppRun"
test -f "${APPDIR}/everlive.desktop"
test -x "${APPDIR}/usr/bin/gc573_user_streamer"
test -f "${APPDIR}/usr/lib/libgc573-v4l2.so"
test -f "${APPDIR}/usr/lib/everlive/everlive_viewer.py"
echo "      Payload verified successfully."

if [ "${APPDIR_ONLY}" -eq 1 ]; then
    echo "[*] --appdir-only requested. AppDir created at: ${APPDIR}"
    exit 0
fi

echo "[4/4] Packaging AppImage..."
APPIMAGETOOL=""
if command -v appimagetool >/dev/null 2>&1; then
    APPIMAGETOOL="appimagetool"
elif [ -x "${SCRIPT_DIR}/appimagetool" ]; then
    APPIMAGETOOL="${SCRIPT_DIR}/appimagetool"
fi

if [ -n "${APPIMAGETOOL}" ]; then
    ARCH="$(uname -m)"
    export ARCH
    "${APPIMAGETOOL}" "${APPDIR}" "${OUTPUT_DIR}/EverLive-${ARCH}.AppImage"
    echo "[+] AppImage built successfully: ${OUTPUT_DIR}/EverLive-${ARCH}.AppImage"
else
    echo "[*] appimagetool not found on system. AppDir is fully staged at:"
    echo "    ${APPDIR}"
    echo "    To generate the final single-file AppImage, download appimagetool and run:"
    echo "    ARCH=x86_64 ./appimagetool ${APPDIR} ${OUTPUT_DIR}/EverLive-x86_64.AppImage"
fi
EOF
