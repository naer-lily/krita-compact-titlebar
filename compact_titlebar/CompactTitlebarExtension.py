"""
Compact Titlebar — Krita Plugin
Windows only.  Removes the native titlebar and embeds window controls
(minimise / maximise / close), drag, and double-click into the menu bar.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Dict

from krita import *
from PyQt5.QtCore import Qt, QEvent, QObject, QAbstractNativeEventFilter, QCoreApplication
from PyQt5.QtWidgets import QToolButton, QWidget, QHBoxLayout, QMainWindow


# ═══════════════════════════════════════════════════════════════════════════════
#  Windows: DWM + custom chrome via Win32 style manipulation
#
#  FramelessWindowHint → WS_POPUP → WM_NCHITTEST is never sent.  Doesn't work.
#
#  Correct approach (VS Code / Electron style):
#  1. Remove WS_CAPTION | WS_SYSMENU from the window style  (titlebar gone)
#  2. Keep WS_THICKFRAME  (resize + Snap + WM_NCHITTEST all work natively)
#  3. WM_NCCALCSIZE → return 0 so client rect = window rect  (no visible border gap)
#  4. WM_NCHITTEST   → return HT* for edge pixels  (wider resize zones)
#  5. DwmExtendFrameIntoClientArea(0,1,0,0)  → DWM shadows
# ═══════════════════════════════════════════════════════════════════════════════

class _MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth",    ctypes.c_int),
        ("cxRightWidth",   ctypes.c_int),
        ("cyTopHeight",    ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def _dwm_extend(hwnd: int) -> bool:
    """Tell DWM we are using custom chrome → shadows."""
    try:
        margins = _MARGINS(0, 1, 0, 0)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            hwnd, ctypes.byref(margins))
        return True
    except Exception:
        return False


# Win32 constants
GWL_STYLE      = -16
GWL_EXSTYLE    = -20
WS_CAPTION     = 0x00C00000    # titlebar + system menu icons
WS_SYSMENU     = 0x00080000    # Alt+Space menu
WS_THICKFRAME  = 0x00040000    # resize borders
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
SWP_FLAGS      = 0x0020 | 0x0002 | 0x0001 | 0x0004   # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER

WM_NCCALCSIZE  = 0x0083
WM_NCHITTEST   = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_GETMINMAXINFO = 0x0024

HTLEFT         = 10
HTRIGHT        = 11
HTTOP          = 12
HTTOPLEFT      = 13
HTTOPRIGHT     = 14
HTBOTTOM       = 15
HTBOTTOMLEFT   = 16
HTBOTTOMRIGHT  = 17


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork",    wintypes.RECT),
        ("dwFlags",   wintypes.DWORD),
    ]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved",     wintypes.POINT),
        ("ptMaxSize",      wintypes.POINT),
        ("ptMaxPosition",  wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


def _remove_caption(hwnd: int):
    """Remove WS_CAPTION | WS_SYSMENU but keep WS_THICKFRAME."""
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    style &= ~(WS_CAPTION | WS_SYSMENU)
    style |= WS_THICKFRAME    # ensure it's on
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)
    # Also fix the WS_EX style: remove the redirection bitmap which can cause
    # rendering artifacts with custom frame
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style &= ~WS_EX_NOREDIRECTIONBITMAP
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)


class _WinFrameFilter(QAbstractNativeEventFilter):
    """
    Per-window native event filter.
    - WM_NCCALCSIZE:  return 0 → client rect = window rect (no border gap).
    - WM_NCHITTEST:   return HT* for edge pixels → resize + Snap.
    """

    BORDER = 6

    def __init__(self, qwin: QMainWindow):
        super().__init__()
        self._hwnd = int(qwin.winId())
        self._qwin = qwin

    def nativeEventFilter(self, event_type: bytes, message):
        if event_type != b"windows_generic_MSG":
            return False, 0

        msg = wintypes.MSG.from_address(int(message))
        if msg.hWnd != self._hwnd:
            return False, 0

        # ---- WM_GETMINMAXINFO: fix maximised bounds to work area --------
        if msg.message == WM_GETMINMAXINFO:
            monitor = ctypes.windll.user32.MonitorFromWindow(
                self._hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(
                monitor, ctypes.byref(mi))

            mmi = ctypes.cast(
                msg.lParam, ctypes.POINTER(_MINMAXINFO)).contents
            mmi.ptMaxPosition.x = mi.rcWork.left
            mmi.ptMaxPosition.y = mi.rcWork.top
            mmi.ptMaxSize.x     = mi.rcWork.right - mi.rcWork.left
            mmi.ptMaxSize.y     = mi.rcWork.bottom - mi.rcWork.top
            return True, 0

        # ---- WM_NCCALCSIZE: client rect = window rect (no border gap) ----
        if msg.message == WM_NCCALCSIZE:
            if msg.wParam:
                return True, 0
            return False, 0

        # ---- WM_NCHITTEST: custom resize edge zones ----------------------
        if msg.message == WM_NCHITTEST:
            # Maximised: no edge resizing
            if self._qwin.isMaximized():
                return False, 0
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value

            g = self._qwin.geometry()
            rx = x - g.x()
            ry = y - g.y()
            rw = g.width()
            rh = g.height()

            left   = rx < self.BORDER
            right  = rx > rw - self.BORDER
            top    = ry < self.BORDER
            bottom = ry > rh - self.BORDER

            if top and left:     return True, HTTOPLEFT
            if top and right:    return True, HTTOPRIGHT
            if bottom and left:  return True, HTBOTTOMLEFT
            if bottom and right: return True, HTBOTTOMRIGHT
            if left:             return True, HTLEFT
            if right:            return True, HTRIGHT
            if top:              return True, HTTOP
            if bottom:           return True, HTBOTTOM

        return False, 0


# ═══════════════════════════════════════════════════════════════════════════════
#  Event filter: menubar drag + double-click
# ═══════════════════════════════════════════════════════════════════════════════

class _MenubarEventFilter(QObject):
    """
    Installed on QMenuBar.  Drag starts on mouse MOVE (not press) so that
    double-click to maximize still works.
    """

    DRAG_THRESHOLD = 5

    def __init__(self, qwin: QMainWindow):
        super().__init__()
        self._qwin      = qwin
        self._hwnd      = int(qwin.winId())
        self._press_pos = None       # QPoint where mouse was pressed (global)

    def eventFilter(self, obj: QObject, event: QEvent):
        etype = event.type()

        if etype == QEvent.MouseButtonPress:
            if obj.actionAt(event.pos()) is None:
                self._press_pos = event.globalPos()
                # Don't consume — double-click needs to see this press too
                return False
            return super().eventFilter(obj, event)

        if etype == QEvent.MouseMove:
            if self._press_pos is not None:
                if (event.globalPos() - self._press_pos).manhattanLength() >= self.DRAG_THRESHOLD:
                    self._press_pos = None
                    # WS_THICKFRAME is present → Aero Snap works natively
                    ctypes.windll.user32.ReleaseCapture()
                    ctypes.windll.user32.SendMessageW(
                        self._hwnd, WM_NCLBUTTONDOWN, 2, 0)
                    return True
            return super().eventFilter(obj, event)

        if etype == QEvent.MouseButtonRelease:
            self._press_pos = None
            return super().eventFilter(obj, event)

        if etype == QEvent.MouseButtonDblClick:
            self._press_pos = None
            if obj.actionAt(event.pos()) is None:
                if self._qwin.isMaximized():
                    self._qwin.showNormal()
                else:
                    self._qwin.showMaximized()
                return True

        # Right-click on empty space → native window menu
        if etype == QEvent.MouseButtonPress and event.button() == Qt.RightButton:
            if obj.actionAt(event.pos()) is None:
                self._show_system_menu(event.globalPos())
                return True

        return super().eventFilter(obj, event)

    def _show_system_menu(self, pos):
        """Show the native window system menu at the given global position."""
        user32 = ctypes.windll.user32
        hmenu = user32.GetSystemMenu(self._hwnd, False)
        cmd = user32.TrackPopupMenu(
            hmenu, 0x0000, pos.x(), pos.y(), 0, self._hwnd, None)
        if cmd:
            user32.PostMessageW(self._hwnd, WM_SYSCOMMAND, cmd, 0)


# ═══════════════════════════════════════════════════════════════════════════════
#  Event filter: detect WindowStateChange to update maximise button icon
# ═══════════════════════════════════════════════════════════════════════════════

class _WindowStateFilter(QObject):
    """Detects QEvent.WindowStateChange on the QMainWindow and emits a signal."""

    def __init__(self, on_state_changed):
        super().__init__()
        self._cb = on_state_changed

    def eventFilter(self, obj: QObject, event: QEvent):
        if event.type() == QEvent.WindowStateChange:
            self._cb()
        return super().eventFilter(obj, event)


# ═══════════════════════════════════════════════════════════════════════════════
#  Extension
# ═══════════════════════════════════════════════════════════════════════════════

class CompactTitlebarExtension(Extension):

    def __init__(self, parent):
        super().__init__(parent)
        self._managed: Dict[str, dict] = {}    # key = qwin.objectName()

    # -- lifecycle ------------------------------------------------------------

    def setup(self):
        pass

    def createActions(self, window: Window):
        qwin    = window.qwindow()
        menubar = qwin.menuBar()
        if menubar is None:
            return

        # Window is unhashable & can be GC'd — use qwin's objectName as key
        obj_name = qwin.objectName()

        # Guard against double-setup (e.g. plugin reload)
        if obj_name in self._managed:
            self._teardown_window(obj_name)

        # 1. Corner widget  —  minimise / maximise / close
        corner, btn_max = _make_window_controls(qwin, obj_name, menubar)
        menubar.setCornerWidget(corner, Qt.TopRightCorner)

        # 2. Menubar event filter  —  drag + double-click
        menubar_filter = _MenubarEventFilter(qwin)
        menubar.installEventFilter(menubar_filter)

        # 3. Window-state filter  —  keep maximise button icon in sync
        def _update_max():
            if qwin.isMaximized():
                btn_max.setText("\u2750")   # ❐
                btn_max.setToolTip("Restore")
            else:
                btn_max.setText("\u25A1")   # □
                btn_max.setToolTip("Maximize")

        state_filter = _WindowStateFilter(_update_max)
        qwin.installEventFilter(state_filter)

        # 4. Frameless  —  remove native titlebar, keep DWM shadows & resize
        native_filter = _make_frameless(qwin)

        self._managed[obj_name] = {
            'corner':         corner,
            'menubar_filter': menubar_filter,
            'state_filter':   state_filter,
            'native_filter':  native_filter,
            'menubar':        menubar,
            'qwin':           qwin,
        }

        # Cleanup when the Krita window closes  —  capture objectName by value
        window.windowClosed.connect(
            lambda on=obj_name: self._teardown_window(on))

    def _teardown_window(self, obj_name: str):
        if obj_name not in self._managed:
            return
        d = self._managed.pop(obj_name)
        d['menubar'].removeEventFilter(d['menubar_filter'])
        d['qwin'].removeEventFilter(d['state_filter'])
        if d['native_filter'] is not None:
            QCoreApplication.instance().removeNativeEventFilter(d['native_filter'])


# ═══════════════════════════════════════════════════════════════════════════════
#  Frameless helper
# ═══════════════════════════════════════════════════════════════════════════════

def _make_frameless(qwin: QMainWindow):
    """Remove native titlebar via Win32 style bits — keep WS_THICKFRAME for resize+Snap."""
    hwnd = int(qwin.winId())

    _dwm_extend(hwnd)

    # Remove WS_CAPTION but KEEP WS_THICKFRAME (critical for resize + Snap + NCHITTEST)
    _remove_caption(hwnd)

    nc_filter = _WinFrameFilter(qwin)
    QCoreApplication.instance().installNativeEventFilter(nc_filter)

    return nc_filter


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

_BTN_STYLE = """
    QToolButton {
        background: transparent;
        border: none;
        font-family: "Segoe MDL2 Assets", "Segoe UI Symbol", "Segoe UI", sans-serif;
        color: palette(window-text);
        padding: 0;
        margin: 0;
    }
    QToolButton:hover {
        background: rgba(128, 128, 128, 0.25);
    }
    QToolButton#titlebar-close:hover {
        background: #E81123;
        color: white;
    }
"""

def _make_window_controls(qwin: QMainWindow, obj_name: str, menubar):
    """Build minimise / maximise / close buttons and return (widget, btn_max)."""
    # Match menubar height so buttons don't overflow or get clipped
    bar_h = menubar.height() 
    btn_w = 60

    w = QWidget()
    w.setObjectName("compact-titlebar-controls")
    w.setFixedHeight(bar_h)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    # -- Minimise ---------------------------------------------------------
    b_min = QToolButton()              
    b_min.setText("\u2500") # ─
    b_min.setObjectName("titlebar-minimize")
    b_min.setFixedSize(btn_w, bar_h)
    b_min.setToolTip("Minimise")
    b_min.setStyleSheet(_BTN_STYLE)
    b_min.clicked.connect(qwin.showMinimized)

    # -- Maximise / Restore ------------------------------------------------
    b_max = QToolButton()
    b_max.setObjectName("titlebar-maximize")
    b_max.setFixedSize(btn_w, bar_h)
    b_max.setStyleSheet(_BTN_STYLE)

    def _toggle():
        if qwin.isMaximized():
            qwin.showNormal()
        else:
            qwin.showMaximized()

    b_max.clicked.connect(_toggle)

    # -- Close -------------------------------------------------------------
    # Krita Window wrappers are ephemeral — never capture them.
    # Look up the current Window via qwin.objectName() at click time.
    def _on_close():
        for w in Krita.instance().windows():
            if w.qwindow().objectName() == obj_name:
                w.close()
                return
        qwin.close()  # fallback (shouldn't normally be reached)

    b_close = QToolButton()          
    b_close.setText("\u2715") # ✕
    b_close.setObjectName("titlebar-close")
    b_close.setFixedSize(btn_w, bar_h)
    b_close.setToolTip("Close")
    b_close.setStyleSheet(_BTN_STYLE)
    b_close.clicked.connect(_on_close)

    lay.addWidget(b_min)
    lay.addWidget(b_max)
    lay.addWidget(b_close)

    return w, b_max


# ═══════════════════════════════════════════════════════════════════════════════
#  Register
# ═══════════════════════════════════════════════════════════════════════════════

_EXT = CompactTitlebarExtension(Krita.instance())
Krita.instance().addExtension(_EXT)
