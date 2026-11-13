# SPDX-License-Identifier: GPL-2.0-only
"""Console Hub visual tokens and stylesheets.

Keeping the Qt stylesheet out of ``everlive_viewer.py`` makes it practical to
reuse the same palette in the embedded player, advanced pane, dialogs, and
fullscreen OSD without copying colour literals.
"""

from __future__ import annotations

# Core palette
BG = "#0b0d13"
SURFACE = "#0f131a"
SURFACE_RAISED = "#151a24"
SURFACE_ALT = "#171b26"
BORDER = "#2a3448"
BORDER_SUBTLE = "#1f2537"
TEXT = "#e6e9ef"
TEXT_STRONG = "#f1f5f9"
TEXT_MUTED = "#94a3b8"
TEXT_DIM = "#64748b"
ACCENT = "#3b82f6"
ACCENT_STRONG = "#2b4f6e"
SUCCESS = "#34d399"
SUCCESS_DIM = "#7fd1ad"
WARNING = "#fbbf24"
DANGER = "#f87171"

RADIUS = 8
RADIUS_SM = 6
SPACING = 10

# Full-window stylesheet
MAIN_QSS = r"""QMainWindow {
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
    combobox-popup: 0;
    min-height: 22px;
    max-height: 22px;
    padding: 4px 10px;
    font-size: 12px;
    background-color: #171b26;
    color: #f1f5f9;
    border: 1px solid #2b3348;
    border-radius: 6px;
}
QComboBox:hover {
    border-color: #3b82f6;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView, QComboBox QListView {
    background-color: #151a24;
    color: #f1f5f9;
    border: 1px solid #2b3548;
    border-radius: 6px;
    selection-background-color: #2b4f6e;
    selection-color: #ffffff;
    outline: none;
    padding: 4px;
}
QComboBox QAbstractItemView::item, QComboBox QListView::item {
    min-height: 28px;
    padding: 4px 10px;
    border-radius: 4px;
    color: #e6e9ef;
    background-color: transparent;
}
QComboBox QAbstractItemView::item:hover, QComboBox QListView::item:hover {
    background-color: #212a3a;
    color: #ffffff;
}
QComboBox QAbstractItemView::item:selected, QComboBox QListView::item:selected {
    background-color: #2b4f6e;
    color: #ffffff;
}

QToolTip {
    background-color: #151a24;
    color: #f1f5f9;
    border: 1px solid #2b3548;
    border-radius: 5px;
    padding: 5px 8px;
    font-size: 11px;
    opacity: 255;
}

QMenu {
    background-color: #151a24;
    color: #f1f5f9;
    border: 1px solid #2b3548;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 14px;
    border-radius: 4px;
    color: #e6e9ef;
}
QMenu::item:selected {
    background-color: #2b4f6e;
    color: #ffffff;
}
QMenu::separator {
    height: 1px;
    background-color: #232a37;
    margin: 4px 8px;
}

QLineEdit, QSpinBox, QDoubleSpinBox {
    min-height: 22px;
    max-height: 22px;
    padding: 4px 10px;
    font-size: 12px;
    background-color: #171d29;
    color: #e6e9ef;
    border: 1px solid #2a3242;
    border-radius: 6px;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #3a465e; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #4b6ea8; }
QDoubleSpinBox QLineEdit {
    padding-left: 6px;
    padding-right: 6px;
    background: transparent;
    color: #e6e9ef;
    border: none;
}
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
    min-height: 30px;
    max-height: 30px;
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

QPushButton#btnCustomLed {
    padding: 0px;
    min-height: 30px;
    max-height: 30px;
}

QPushButton#btnMute {
    padding: 0px;
    min-height: 30px;
    max-height: 30px;
}

QPushButton#btnFgRecheck {
    padding: 0px 8px;
    min-height: 22px;
    max-height: 22px;
}

QPushButton {
    min-height: 22px;
    max-height: 22px;
    padding: 4px 14px;
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

QPushButton#btnAdvancedExpand {
    background-color: #141822;
    color: #cbd5e1;
    border: 1px solid #222938;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    padding: 4px 12px;
    text-align: left;
    min-height: 22px;
    max-height: 22px;
}
QPushButton#btnAdvancedExpand:hover {
    background-color: #1c2230;
    color: #ffffff;
    border-color: #3b82f6;
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
"""

# Opaque popup list used by every QComboBox.  Qt's native popup ignores parts
# of the parent palette on some platforms, so this is set explicitly.
COMBO_POPUP_QSS = r"""
    QListView {
        background-color: #151a24;
        color: #f1f5f9;
        border: 1px solid #2b3548;
        border-radius: 6px;
        outline: none;
        padding: 4px;
    }
    QListView::item {
        min-height: 28px;
        padding: 4px 10px;
        border-radius: 4px;
        color: #e6e9ef;
    }
    QListView::item:hover {
        background-color: #212a3a;
        color: #ffffff;
    }
    QListView::item:selected {
        background-color: #2b4f6e;
        color: #ffffff;
    }
"""

# Fullscreen OSD/telemetry surface.  Kept translucent so the video remains
# visible behind it.
OSD_QSS = r"""
    QFrame#osdHud {
        background-color: rgba(14, 19, 29, 235);
        border: 1px solid #2a3448;
        border-radius: 10px;
    }
    QLabel#osdTitle {
        color: #ffffff;
        font-weight: bold;
        font-size: 13px;
        border: none;
        background: transparent;
    }
    QLabel#osdMeta {
        color: #7fd1ad;
        font-weight: bold;
        font-size: 11px;
        border: none;
        background: transparent;
    }
    QLabel#osdChip {
        background-color: rgba(13, 35, 24, 220);
        color: #34d399;
        border: 1px solid #164e32;
        border-radius: 4px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: bold;
    }
    QPushButton#osdAction {
        background: #1a2030;
        color: #d7dde8;
        border: 1px solid #34425a;
        padding: 3px 9px;
        border-radius: 4px;
        font-weight: bold;
        font-size: 11px;
    }
    QPushButton#osdAction:hover {
        background: #243044;
        border-color: #3b82f6;
        color: #ffffff;
    }
    QPushButton#osdExit {
        background: #2b4f6e;
        color: #ffffff;
        border: 1px solid #38658c;
        padding: 3px 11px;
        font-weight: bold;
        font-size: 11px;
        border-radius: 4px;
    }
    QPushButton#osdExit:hover {
        background: #356085;
        border-color: #4a7fa8;
    }
"""
