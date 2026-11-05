#!/usr/bin/env bash
# ==============================================================================
# EverLive Experimental (1.4 Lab) - user-local, side-by-side installer
# ==============================================================================
# Installs ONLY into the current user's home directory:
#
#   ~/.local/share/applications/everlive-experimental.desktop
#
# It never touches:
#
#   ~/.local/share/applications/everlive.desktop   (stable EverLive launcher)
#   ~/.config/everlive/                            (stable settings + log)
#   the stable everlive.sock / everlive.sock.lock  (Experimental uses
#                                                   everlive-experimental.sock)
#
# No sudo is required: udev/VFIO/PipeWire rules and the memlock limits were
# already installed by the stable EverLive release.  Running the script with
# sudo is supported; the real user is then resolved from SUDO_USER.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USERD_DIR="${SCRIPT_DIR}/tools/opengc573-userd"
GC570D_DIR="${SCRIPT_DIR}/tools/opengc570d-userd"
VIEWER_DIR="${SCRIPT_DIR}/tools/everlive"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

WANT_FRAMEGEN="ask"
WANT_PRELOAD="ask"

usage() {
    cat <<EOF
Usage: ./install-experimental.sh [OPTIONS]

  --with-framegen
      Download and install the optional lsfg-vk 2.0 Vulkan layer into
      ~/.local without prompting (skipped automatically if already installed).

  --no-framegen, --skip-framegen
      Skip installing the optional lsfg-vk 2.0 Vulkan layer without prompting.

  --with-preload, --preload-apps
      Inject userspace camera LD_PRELOAD into user desktop launchers
      (Discord, OBS, Chrome, Chromium) without prompting.

  --no-preload, --skip-preload
      Skip desktop launcher LD_PRELOAD injection and show manual launch instructions.

  -h, --help
      Show this help and exit.
EOF
}

for arg in "$@"; do
    case "${arg}" in
        --with-framegen)               WANT_FRAMEGEN=1 ;;
        --no-framegen|--skip-framegen) WANT_FRAMEGEN=0 ;;
        --with-preload|--preload-apps) WANT_PRELOAD=1 ;;
        --no-preload|--skip-preload)   WANT_PRELOAD=0 ;;
        -h|--help)                     usage; exit 0 ;;
        *)
            echo "Unknown option: ${arg}" >&2
            usage >&2
            exit 2
            ;;
    esac
done

echo -e "${BLUE}===================================================================${NC}"
echo -e "${BLUE}   EverLive Experimental (1.4 Lab) - user-local installer         ${NC}"
echo -e "${BLUE}===================================================================${NC}"

# 1. Resolve the real user.  sudo changes HOME to /root, which would put the
#    launcher into the wrong application menu.
if [ "${EUID}" -eq 0 ]; then
    TARGET_USER="${SUDO_USER:-root}"
else
    TARGET_USER="${USER:-$(id -un)}"
fi
TARGET_HOME="$(getent passwd "${TARGET_USER}" 2>/dev/null | cut -d: -f6 || true)"
if [ -z "${TARGET_HOME}" ]; then
    TARGET_HOME="${HOME}"
fi

echo -e "[*] Repository : ${SCRIPT_DIR}"
echo -e "[*] Target user: ${GREEN}${TARGET_USER}${NC} (${TARGET_HOME})"

if [ "${WANT_FRAMEGEN}" = "ask" ]; then
    if [ -t 0 ]; then
        echo ""
        read -r -p "Install optional 2x Frame Generation layer (lsfg-vk 2.0 into ~/.local if missing)? [y/N] " answer || true
        case "${answer:-n}" in
            [yY]|[jJ]*) WANT_FRAMEGEN=1 ;;
            *)          WANT_FRAMEGEN=0 ;;
        esac
    else
        WANT_FRAMEGEN=0
    fi
fi

has_lsfg_vk_layer() {
    [ -f "/usr/share/vulkan/implicit_layer.d/VkLayer_LSFGVK_frame_generation.json" ] ||
    [ -f "/etc/vulkan/implicit_layer.d/VkLayer_LSFGVK_frame_generation.json" ] ||
    [ -f "${TARGET_HOME}/.local/share/vulkan/implicit_layer.d/VkLayer_LSFGVK_frame_generation.json" ]
}

ensure_user_lsfg_vk() {
    echo -e "\n${BLUE}[*] Checking optional lsfg-vk 2.0 layer...${NC}"
    if has_lsfg_vk_layer; then
        echo -e "${GREEN}[+] lsfg-vk 2.0 layer already present.${NC}"
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
        echo -e "${YELLOW}[!] curl or tar missing; install lsfg-vk via package manager or https://lsfg-vk.dev/docs/installation/${NC}"
        return 0
    fi

    local archive_ver="2.0.0"
    local archive_sha256="08bdbdf373a111022df87dac7aa87e3b564bb841f961552e3ca85fea12b5aa74"
    local archive_rel="lsfg-vk-${archive_ver}.tar.xz"
    local archive_url="https://builds.lsfg-vk.dev/${archive_rel}"
    local tmp_tar=""

    echo -e "[*] Downloading official ${archive_rel} into ${TARGET_HOME}/.local..."
    tmp_tar="$(mktemp /tmp/everlive-lsfg-vk.XXXXXX.tar.xz)"
    if curl -fL --max-time 60 "${archive_url}" -o "${tmp_tar}" &&
       tar -tJf "${tmp_tar}" >/dev/null 2>&1; then
        local tar_hash=""
        tar_hash="$(sha256sum "${tmp_tar}" 2>/dev/null | awk '{print $1}')"
        if [ "${tar_hash}" != "${archive_sha256}" ]; then
            echo -e "${RED}[ERROR] Checksum verification failed for ${archive_rel}!${NC}"
            echo -e "  Expected: ${archive_sha256}"
            echo -e "  Got     : ${tar_hash}"
            rm -f "${tmp_tar}"
            return 0
        fi
        chmod 644 "${tmp_tar}"
        if [ "${EUID}" -eq 0 ] && [ "${TARGET_USER}" != "root" ]; then
            sudo -u "${TARGET_USER}" mkdir -p "${TARGET_HOME}/.local"
            sudo -u "${TARGET_USER}" tar -xJf "${tmp_tar}" -C "${TARGET_HOME}/.local" || true
        else
            mkdir -p "${TARGET_HOME}/.local"
            tar -xJf "${tmp_tar}" -C "${TARGET_HOME}/.local" || true
        fi
    fi
    rm -f "${tmp_tar}"

    if has_lsfg_vk_layer; then
        echo -e "${GREEN}[+] lsfg-vk 2.0 extracted to ${TARGET_HOME}/.local.${NC}"
    else
        echo -e "${YELLOW}[!] Automatic lsfg-vk download failed; see https://lsfg-vk.dev/docs/installation/${NC}"
    fi
}

if [ "${WANT_FRAMEGEN}" -eq 1 ]; then
    ensure_user_lsfg_vk
fi

# 2. Build the userspace VFIO engine.  This is the same blob-free engine the
#    stable release uses; the Experimental branch adds lsfg-vk frame
#    generation on top, so both can use the hardware (never at the same time).
echo -e "\n${BLUE}[1/3] Building the userspace VFIO engine...${NC}"
make -C "${USERD_DIR}" -j"$(nproc)"
chmod 0755 "${USERD_DIR}/gc573_user_streamer" "${USERD_DIR}/everlive_userd" "${USERD_DIR}/gc573_user_probe" "${USERD_DIR}/gc573_user_diag" 2>/dev/null || true
if [ -d "${GC570D_DIR}" ]; then
    echo -e "${BLUE}[*] Building the GC570D userspace engine...${NC}"
    make -C "${GC570D_DIR}" -j"$(nproc)"
    chmod 0755 "${GC570D_DIR}/opengc570d-userd" "${GC570D_DIR}/gc570d-ctl" 2>/dev/null || true
    chmod 0644 "${GC570D_DIR}/libgc570d-v4l2.so" 2>/dev/null || true
fi
chmod +x "${SCRIPT_DIR}/play.sh" 2>/dev/null || true

if [ "${EUID}" -eq 0 ]; then
    # Only meaningful when run as root; the stable install already set this.
    setcap cap_ipc_lock=+ep "${USERD_DIR}/gc573_user_streamer" 2>/dev/null || true
    if [ -d "${GC570D_DIR}" ]; then
        setcap cap_ipc_lock=+ep "${GC570D_DIR}/opengc570d-userd" 2>/dev/null || true
    fi
fi
echo -e "${GREEN}[+] Engine built.${NC}"

# 3. Icons.  Icon=everlive resolves through the stable release's icon theme
#    entries, so only install the copies that are still missing.
echo -e "\n${BLUE}[2/3] Installing application icon (missing entries only)...${NC}"
ICONS_BASE="${TARGET_HOME}/.local/share/icons/hicolor"
for sz in 16 32 48 64 128 256 512; do
    src="${VIEWER_DIR}/icons/${sz}x${sz}/apps/everlive.png"
    if [ -f "${src}" ] && [ ! -f "${ICONS_BASE}/${sz}x${sz}/apps/everlive.png" ]; then
        mkdir -p "${ICONS_BASE}/${sz}x${sz}/apps"
        cp -f "${src}" "${ICONS_BASE}/${sz}x${sz}/apps/everlive.png"
        if [ "${EUID}" -eq 0 ]; then
            chown "${TARGET_USER}:${TARGET_USER}" \
                "${ICONS_BASE}/${sz}x${sz}/apps/everlive.png" 2>/dev/null || true
        fi
    fi
done
src="${VIEWER_DIR}/icons/scalable/apps/everlive.png"
if [ -f "${src}" ] && [ ! -f "${ICONS_BASE}/scalable/apps/everlive.png" ]; then
    mkdir -p "${ICONS_BASE}/scalable/apps"
    cp -f "${src}" "${ICONS_BASE}/scalable/apps/everlive.png"
    if [ "${EUID}" -eq 0 ]; then
        chown "${TARGET_USER}:${TARGET_USER}" \
            "${ICONS_BASE}/scalable/apps/everlive.png" 2>/dev/null || true
    fi
fi
echo -e "${GREEN}[+] Icons ready.${NC}"

# 4. The launcher.  A distinct filename, a distinct WM class and a distinct
#    display name keep both apps apart in every application menu.
echo -e "\n${BLUE}[3/3] Installing the Experimental desktop starter...${NC}"
APPS_DIR="${TARGET_HOME}/.local/share/applications"
mkdir -p "${APPS_DIR}"
DESKTOP_FILE="${APPS_DIR}/everlive-experimental.desktop"

case "${SCRIPT_DIR}" in
    *" "*)
        echo -e "${YELLOW}[!] The repository path contains a space; quoting it in Exec=.${NC}"
        EXEC_CMD="\"${SCRIPT_DIR}/play.sh\""
        ;;
    *)
        EXEC_CMD="${SCRIPT_DIR}/play.sh"
        ;;
esac

cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Name=EverLive Experimental
GenericName=Console Gaming Screen (Experimental FrameGen)
Comment=EverLive 1.4 Lab - Lossless Scaling 2x Console Frame Generation
Exec=${EXEC_CMD}
Icon=everlive
Terminal=false
Type=Application
Categories=Game;AudioVideo;Video;
Keywords=EverLive;Experimental;FrameGen;LSFG;Vulkan;PS5;Capture;AVerMedia;GC573;HDMI;Gaming;Console;VFIO;
StartupWMClass=EverLiveExperimental
EOF
chmod +x "${DESKTOP_FILE}"
if [ "${EUID}" -eq 0 ]; then
    chown "${TARGET_USER}:${TARGET_USER}" "${DESKTOP_FILE}" 2>/dev/null || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APPS_DIR}" >/dev/null 2>&1 || true
fi

# 5. Never, ever remove or rewrite the stable launcher.
if [ -f "${APPS_DIR}/everlive.desktop" ]; then
    echo -e "${GREEN}[+] Stable launcher left untouched: ${APPS_DIR}/everlive.desktop${NC}"
else
    echo -e "${YELLOW}[*] No stable everlive.desktop found; this is a standalone install.${NC}"
fi

# 6. Optional: Userspace camera LD_PRELOAD integration for Discord, Chrome, OBS
configure_desktop_preloads() {
    local preload_lib="/usr/local/lib/libgc570d-v4l2.so"
    if [ ! -f "${preload_lib}" ] && [ -f "${GC570D_DIR}/libgc570d-v4l2.so" ]; then
        preload_lib="${GC570D_DIR}/libgc570d-v4l2.so"
    fi

    local -a candidate_patterns=(
        "discord.desktop:Discord"
        "com.obsproject.Studio.desktop:OBS Studio"
        "obs.desktop:OBS Studio"
        "google-chrome.desktop:Google Chrome"
        "google-chrome-stable.desktop:Google Chrome"
        "chromium.desktop:Chromium"
        "chromium-browser.desktop:Chromium"
        "brave-browser.desktop:Brave Browser"
    )

    local -a matched_srcs=()
    local -a matched_names=()
    local -a matched_basenames=()

    for item in "${candidate_patterns[@]}"; do
        local fname="${item%%:*}"
        local dname="${item#*:}"
        local found=""
        for search_dir in "/usr/share/applications" "/usr/local/share/applications" "${APPS_DIR}"; do
            if [ -f "${search_dir}/${fname}" ]; then
                found="${search_dir}/${fname}"
                break
            fi
        done
        if [ -n "${found}" ]; then
            local dup=0
            for existing in "${matched_basenames[@]:-}"; do
                if [ "${existing}" = "${fname}" ]; then
                    dup=1
                    break
                fi
            done
            if [ "${dup}" -eq 0 ]; then
                matched_srcs+=("${found}")
                matched_names+=("${dname}")
                matched_basenames+=("${fname}")
            fi
        fi
    done

    echo -e "\n${BLUE}[*] Zero-DKMS Userspace Camera Desktop Integration (LD_PRELOAD)...${NC}"

    if [ ${#matched_srcs[@]} -eq 0 ]; then
        echo -e "    No supported applications (Discord, OBS, Chrome) found in /usr/share/applications."
        echo -e "    To use EverLive's camera in any application manually:"
        echo -e "      ${GREEN}LD_PRELOAD=${preload_lib} <application>${NC}"
        return 0
    fi

    local do_inject=0
    if [ "${WANT_PRELOAD}" = "1" ] || [ "${WANT_PRELOAD}" = "yes" ]; then
        do_inject=1
    elif [ "${WANT_PRELOAD}" = "0" ] || [ "${WANT_PRELOAD}" = "no" ]; then
        do_inject=0
    elif [ -t 0 ]; then
        echo -e "Found the following video/voice applications on your system:"
        for ((i=0; i<${#matched_names[@]}; i++)); do
            echo -e "  - ${matched_names[$i]} (${matched_basenames[$i]})"
        done
        echo -e ""
        echo -e "EverLive can configure user overrides in ~/.local/share/applications with"
        echo -e "LD_PRELOAD so these applications automatically discover your capture card."
        echo -n "Configure desktop launchers with EverLive camera preload? [y/N] "
        read -r reply || true
        case "$reply" in
            [yY]|[yY][eE][sS]) do_inject=1 ;;
            *) do_inject=0 ;;
        esac
    fi

    if [ "${do_inject}" -eq 1 ]; then
        mkdir -p "${APPS_DIR}"
        for ((i=0; i<${#matched_srcs[@]}; i++)); do
            local src="${matched_srcs[$i]}"
            local fname="${matched_basenames[$i]}"
            local dname="${matched_names[$i]}"
            local dst="${APPS_DIR}/${fname}"

            if [ -f "${dst}" ] && grep -q "LD_PRELOAD=" "${dst}" 2>/dev/null; then
                echo -e "  ${GREEN}[✓] ${dname} already configured with LD_PRELOAD in ${dst}${NC}"
            else
                sed -E '/^Exec=/ {
                    /LD_PRELOAD/! s|^Exec=(.*)$|Exec=env LD_PRELOAD='"${preload_lib}"' \1|
                }' "${src}" > "${dst}"
                chmod +x "${dst}"
                if [ "${EUID}" -eq 0 ]; then
                    chown "${TARGET_USER}:${TARGET_USER}" "${dst}" 2>/dev/null || true
                fi
                echo -e "  ${GREEN}[+] Configured ${dname} with camera preload in ${dst}${NC}"
            fi
        done
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database "${APPS_DIR}" >/dev/null 2>&1 || true
        fi
    else
        echo -e "  ${YELLOW}[*] Desktop launchers left untouched.${NC}"
        echo -e "  To launch applications manually with the EverLive userspace camera:"
        for ((i=0; i<${#matched_names[@]}; i++)); do
            local cmd_name="$(echo "${matched_names[$i]}" | tr '[:upper:]' '[:lower:]' | awk '{print $1}')"
            echo -e "    ${GREEN}LD_PRELOAD=${preload_lib} ${cmd_name}${NC}"
        done
        echo -e "  Or add 'env LD_PRELOAD=${preload_lib}' to your custom launch scripts."
    fi
}
configure_desktop_preloads

echo -e "\n${GREEN}===================================================================${NC}"
echo -e "${GREEN}      EverLive Experimental is ready in your application menu.     ${NC}"
echo -e "${GREEN}===================================================================${NC}"
echo -e "Launcher : ${BLUE}${DESKTOP_FILE}${NC}"
echo -e "Config   : ${BLUE}${TARGET_HOME}/.config/everlive-experimental/${NC}"
echo -e "Socket   : ${BLUE}\$XDG_RUNTIME_DIR/everlive-experimental.sock${NC}"
echo -e ""
echo -e "Frame generation hotkeys in the game window:"
echo -e "  ${GREEN}g${NC}  cycle Off -> 60->120 FPS -> 30->60 FPS"
echo -e ""
echo -e "Requires the lsfg-vk 2.0 Vulkan layer and Lossless Scaling in Steam"
echo -e "(Properties -> Betas -> 'lsfg-vk'); the Hub shows a readiness line."
echo -e ""
echo -e "The stable EverLive launcher, config and socket remain untouched."
