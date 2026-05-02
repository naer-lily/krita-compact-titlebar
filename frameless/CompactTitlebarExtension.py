"""
Frameless — Krita Plugin  (Windows 10+ only)
==============================================

Replaces the native Windows titlebar with a compact header:
the menu bar itself serves as the window's drag handle and carries
minimise / maximise / close buttons on its right side.

How it works (high level)
-------------------------
1. Win32 style manipulation — remove the caption but keep the resize frame
   so that Windows still sends WM_NCHITTEST (which lets us implement edge
   resizing) and Aero Snap still works.
2. DWM frame extension — tells the Desktop Window Manager "we're doing
   custom chrome" so it renders drop shadows.
3. Three event listeners / filters:
   - Native event filter   → WM_NCCALCSIZE, WM_GETMINMAXINFO, WM_NCHITTEST
   - Menubar event filter  → drag + double-click on empty menubar space
   - Window-state filter   → keep the maximise button icon in sync

Why not Qt.FramelessWindowHint?
-------------------------------
On Windows, QMainWindow.setWindowFlags(Qt.FramelessWindowHint) sets the
underlying HWND to WS_POPUP style.  A WS_POPUP window has *zero* non-client
area, so Windows **never sends WM_NCHITTEST**.  No WM_NCHITTEST = no way
to give the user resize cursors at the edges.  We work around this by
manually removing only WS_CAPTION from the window style, leaving the
WS_THICKFRAME border style in place.  The borders are invisible (WM_NCCALCSIZE
forces client rect == window rect), but they keep WM_NCHITTEST alive.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Dict

from krita import *
from PyQt5.QtCore import (
    Qt, QEvent, QObject, QAbstractNativeEventFilter, QCoreApplication,
)
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QToolButton, QWidget, QHBoxLayout, QMainWindow


# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration — all tunable constants live here
# ═══════════════════════════════════════════════════════════════════════════════

# Resize
RESIZE_BORDER_PX      = 6       # width of invisible resize edge zone
MONITOR_DEFAULTTONEAREST = 2    # MonitorFromWindow flag

# Drag
DRAG_THRESHOLD_PX     = 5       # pixels of movement before drag starts

# DWM
DWM_TOP_MARGIN        = 1       # px extended into client area (triggers shadows)

# Corner widget polling
CORNER_POLL_MS        = 100     # milliseconds between corner widget checks

# Window control buttons — visual
BTN_WIDTH             = 60      # px
BTN_TEXT_MINIMISE     = "\u2500"  # ─
BTN_TEXT_MAXIMISE     = "\u25A1"  # □
BTN_TEXT_RESTORE      = "\u2750"  # ❐
BTN_TEXT_CLOSE        = "\u2715"  # ✕
CLOSE_HOVER_BG        = "#E81123" # Windows-native red


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FRAMELESS WINDOW                                                           ║
# ║  All Windows-specific code that removes the native titlebar while           ║
# ║  keeping resize edges, Aero Snap, and DWM shadows.                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# --- DWM (Desktop Window Manager) ---------------------------------------------

class _MARGINS(ctypes.Structure):
    """DWM margin structure for DwmExtendFrameIntoClientArea."""
    _fields_ = [
        ("cxLeftWidth",    ctypes.c_int),
        ("cxRightWidth",   ctypes.c_int),
        ("cyTopHeight",    ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def _dwm_extend(hwnd: int) -> bool:
    """Request DWM shadows for the window.  Returns False on non-Windows."""
    try:
        margins = _MARGINS(0, DWM_TOP_MARGIN, 0, 0)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            hwnd, ctypes.byref(margins))
        return True
    except Exception:
        return False


# --- Win32 window style constants & helpers -----------------------------------

GWL_STYLE      = -16         # GetWindowLongW: standard style
GWL_EXSTYLE    = -20         # GetWindowLongW: extended style
WS_CAPTION     = 0x00C00000  # titlebar + system-button area
WS_SYSMENU     = 0x00080000  # system menu (Alt+Space) — we keep it
WS_THICKFRAME  = 0x00040000  # resizeable border — KEPT for hit-testing
WS_EX_NOREDIRECTIONBITMAP = 0x00200000

SWP_FLAGS = 0x0020 | 0x0002 | 0x0001 | 0x0004  # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER

WM_GETMINMAXINFO = 0x0024   # "what are the max bounds?"
WM_NCCALCSIZE    = 0x0083   # "where is the NC / client boundary?"
WM_NCHITTEST     = 0x0084   # "which NC part is the mouse on?"

HTLEFT         = 10
HTRIGHT        = 11
HTTOP          = 12
HTTOPLEFT      = 13
HTTOPRIGHT     = 14
HTBOTTOM       = 15
HTBOTTOMLEFT   = 16
HTBOTTOMRIGHT  = 17


class _MONITORINFO(ctypes.Structure):
    """rcMonitor = full monitor; rcWork = area excluding taskbar."""
    _fields_ = [
        ("cbSize",    wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork",    wintypes.RECT),
        ("dwFlags",   wintypes.DWORD),
    ]


class _MINMAXINFO(ctypes.Structure):
    """ptMaxSize / ptMaxPosition: maximum dimensions for maximised state."""
    _fields_ = [
        ("ptReserved",     wintypes.POINT),
        ("ptMaxSize",      wintypes.POINT),
        ("ptMaxPosition",  wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


def _remove_caption(hwnd: int):
    """Remove WS_CAPTION from the window style, keeping WS_THICKFRAME.

    WS_CAPTION gone      → no visible titlebar
    WS_THICKFRAME stays  → Windows still sends WM_NCHITTEST
    WS_SYSMENU stays     → Alt+Space menu still works
    """
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    style &= ~WS_CAPTION
    style |= WS_THICKFRAME
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)

    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style &= ~WS_EX_NOREDIRECTIONBITMAP
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)

    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)


# --- Native event filter ------------------------------------------------------

class _WinFrameFilter(QAbstractNativeEventFilter):
    """Intercepts WM_GETMINMAXINFO, WM_NCCALCSIZE, WM_NCHITTEST.

    WM_GETMINMAXINFO — sets max bounds to monitor work area (respects taskbar).
    WM_NCCALCSIZE    — returns 0 so client rect == window rect (no borders).
    WM_NCHITTEST     — returns HT* codes for 6px edge zone (resize cursors).
    """

    BORDER = RESIZE_BORDER_PX

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

        # -- WM_GETMINMAXINFO: maximised bounds = work area ----------------
        if msg.message == WM_GETMINMAXINFO:
            monitor = ctypes.windll.user32.MonitorFromWindow(
                self._hwnd, MONITOR_DEFAULTTONEAREST)
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(mi))
            mmi = ctypes.cast(msg.lParam, ctypes.POINTER(_MINMAXINFO)).contents
            mmi.ptMaxPosition.x = mi.rcWork.left
            mmi.ptMaxPosition.y = mi.rcWork.top
            mmi.ptMaxSize.x     = mi.rcWork.right  - mi.rcWork.left
            mmi.ptMaxSize.y     = mi.rcWork.bottom - mi.rcWork.top
            return True, 0

        # -- WM_NCCALCSIZE: client rect = window rect (no border gap) ------
        if msg.message == WM_NCCALCSIZE:
            if msg.wParam:
                return True, 0
            return False, 0

        # -- WM_NCHITTEST: custom resize edge zones ------------------------
        if msg.message == WM_NCHITTEST:
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


# --- Orchestrator -------------------------------------------------------------

def _make_frameless(qwin: QMainWindow):
    """Apply all frameless-window changes to a QMainWindow.

    1. DWM extension  →  shadows
    2. Win32 style    →  remove caption, keep thickframe
    3. Native filter  →  intercept WM_NCCALCSIZE / NCHITTEST / GETMINMAXINFO

    Returns the native event filter for later cleanup.
    """
    hwnd = int(qwin.winId())
    _dwm_extend(hwnd)
    _remove_caption(hwnd)
    nc_filter = _WinFrameFilter(qwin)
    QCoreApplication.instance().installNativeEventFilter(nc_filter)
    return nc_filter


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MENUBAR INTERACTION                                                        ║
# ║  Qt event filters installed on the QMenuBar: drag, double-click,            ║
# ║  and window-state tracking for the maximise button icon.                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _MenubarEventFilter(QObject):
    """Installed on QMenuBar.  Intercepts mouse events on *empty* menubar
    space (where no QAction sits).

    - Drag:           starts on MouseMove after a 5px threshold (not on
                      press — this preserves double-click detection).
    - Double-click:   toggles maximise / restore.
    - Menu clicks:    pass through normally (checked via actionAt()).
    """

    DRAG_THRESHOLD = DRAG_THRESHOLD_PX

    def __init__(self, qwin: QMainWindow):
        super().__init__()
        self._qwin      = qwin
        self._press_pos = None      # global position of initial mouse press

    def eventFilter(self, obj: QObject, event: QEvent):
        etype = event.type()

        # Mouse press on empty space → record position for drag detection
        if etype == QEvent.MouseButtonPress:
            if obj.actionAt(event.pos()) is None:
                self._press_pos = event.globalPos()
                return False    # pass through for double-click detection
            return super().eventFilter(obj, event)

        # Mouse move: if threshold exceeded → start native window move
        if etype == QEvent.MouseMove:
            if self._press_pos is not None:
                delta = event.globalPos() - self._press_pos
                if delta.manhattanLength() >= self.DRAG_THRESHOLD:
                    self._press_pos = None
                    # Cancel the menubar's pending press so the real mouse-up
                    # after drag doesn't trigger a menu on whatever is under
                    # the cursor (which may have shifted after un-maximise).
                    QCoreApplication.sendEvent(obj, QMouseEvent(
                        QEvent.MouseButtonRelease,
                        event.localPos(), event.screenPos(),
                        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
                    # Qt's startSystemMove → Win32 DefWindowProc(SC_MOVE|HTCAPTION)
                    # → Aero Snap works natively.
                    handle = self._qwin.windowHandle()
                    if handle is not None and hasattr(handle, 'startSystemMove'):
                        handle.startSystemMove()
                    return True
            return super().eventFilter(obj, event)

        # Mouse release → clear drag tracking
        if etype == QEvent.MouseButtonRelease:
            self._press_pos = None
            return super().eventFilter(obj, event)

        # Double-click on empty space → toggle maximise
        if etype == QEvent.MouseButtonDblClick:
            self._press_pos = None
            if obj.actionAt(event.pos()) is None:
                if self._qwin.isMaximized():
                    self._qwin.showNormal()
                else:
                    self._qwin.showMaximized()
                return True

        return super().eventFilter(obj, event)


class _WindowStateFilter(QObject):
    """Detects QEvent.WindowStateChange and calls a callback so the maximise
    button icon stays in sync with the actual window state (e.g. after
    Win+Up, double-click on the menubar, or Aero Snap).

    QMainWindow doesn't emit a "windowStateChanged" signal, so we intercept
    the raw Qt event instead.
    """

    def __init__(self, on_state_changed):
        super().__init__()
        self._cb = on_state_changed

    def eventFilter(self, obj: QObject, event: QEvent):
        if event.type() == QEvent.WindowStateChange:
            self._cb()
        return super().eventFilter(obj, event)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  WINDOW CONTROL BUTTONS                                                     ║
# ║  The minimise / maximise / close buttons embedded in the menu bar.          ║
# ║  Styling adapts to Krita's active theme via palette inheritance.            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_BTN_STYLESHEET = f"""
    QToolButton {{
        background: transparent;
        border: none;
        font-family: "Segoe MDL2 Assets", "Segoe UI Symbol", "Segoe UI", sans-serif;
        padding: 0;
        margin: 0;
    }}
    QToolButton:hover {{
        background: palette(highlight);
    }}
    QToolButton#titlebar-close:hover {{
        background: {CLOSE_HOVER_BG};
        color: white;
    }}
"""


def _make_window_controls(qwin: QMainWindow):
    """Create the minimise / maximise / close button widget, plus a
    window-state filter that keeps the maximise icon in sync.

    All dependencies (menubar, objectName) are derived from qwin.
    Returns (corner_widget, state_filter) — both need cleanup on teardown.
    """
    menubar = qwin.menuBar()
    obj_name = qwin.objectName()
    bar_h = menubar.height()
    btn_w = BTN_WIDTH

    w = QWidget()
    w.setObjectName("frameless-controls")
    w.setFixedHeight(bar_h)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    # -- Minimise ----------------------------------------------------------
    b_min = QToolButton(w)
    b_min.setText(BTN_TEXT_MINIMISE)
    b_min.setObjectName("titlebar-minimize")
    b_min.setFixedSize(btn_w, bar_h)
    b_min.setToolTip("Minimise")
    b_min.setStyleSheet(_BTN_STYLESHEET)
    b_min.clicked.connect(qwin.showMinimized)

    # -- Maximise / Restore -------------------------------------------------
    b_max = QToolButton(w)
    b_max.setObjectName("titlebar-maximize")
    b_max.setFixedSize(btn_w, bar_h)
    b_max.setStyleSheet(_BTN_STYLESHEET)

    def _toggle():
        if qwin.isMaximized():
            qwin.showNormal()
        else:
            qwin.showMaximized()
    b_max.clicked.connect(_toggle)

    # -- Close --------------------------------------------------------------
    def _on_close():
        for kw in Krita.instance().windows():
            if kw.qwindow().objectName() == obj_name:
                kw.qwindow().close()
                return
        qwin.close()

    b_close = QToolButton(w)
    b_close.setText(BTN_TEXT_CLOSE)
    b_close.setObjectName("titlebar-close")
    b_close.setFixedSize(btn_w, bar_h)
    b_close.setToolTip("Close")
    b_close.setStyleSheet(_BTN_STYLESHEET)
    b_close.clicked.connect(_on_close)

    lay.addWidget(b_min)
    lay.addWidget(b_max)
    lay.addWidget(b_close)

    # -- Window-state filter: keep maximise icon in sync -------------------
    def _update_max():
        if qwin.isMaximized():
            b_max.setText(BTN_TEXT_RESTORE)
            b_max.setToolTip("Restore")
        else:
            b_max.setText(BTN_TEXT_MAXIMISE)
            b_max.setToolTip("Maximize")

    state_filter = _WindowStateFilter(_update_max)
    _update_max()
    qwin.installEventFilter(state_filter)

    return w, state_filter


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  KRITA PLUGIN ENTRY POINT                                                   ║
# ║  Wires all the above pieces into Krita's Extension API.                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class CompactTitlebarExtension(Extension):

    def __init__(self, parent):
        super().__init__(parent)
        self._managed: Dict[str, dict] = {}      # key = qwin.objectName()

    # -- Extension lifecycle ---------------------------------------------------

    def setup(self):
        pass

    def createActions(self, window: Window):
        qwin    = window.qwindow()
        menubar = qwin.menuBar()
        if menubar is None:
            return

        obj_name = qwin.objectName()
        if obj_name in self._managed:
            self._teardown_window(obj_name)

        # ---- Window controls: buttons + state filter (all in one) ---------
        corner, state_filter = _make_window_controls(qwin)
        corner.setPalette(menubar.palette())
        menubar.setCornerWidget(corner, Qt.TopRightCorner)

        # ---- Polling guard: Krita MDI subwindow max replaces the corner --
        from PyQt5.QtCore import QTimer
        poll_timer = QTimer(window.qwindow())
        def _poll_corner():
            cur = menubar.cornerWidget(Qt.TopRightCorner)
            if cur is not corner:
                if cur:
                    cur.hide()
                menubar.setCornerWidget(corner, Qt.TopRightCorner)
                corner.show()
        poll_timer.timeout.connect(_poll_corner)
        poll_timer.start(CORNER_POLL_MS)

        # ---- Menubar interaction: drag + double-click --------------------
        menubar_filter = _MenubarEventFilter(qwin)
        menubar.installEventFilter(menubar_filter)

        # ---- Theme change: sync button palette from menubar --------------
        from PyQt5.QtWidgets import QApplication
        def _on_theme_changed():
            pal = menubar.palette()
            corner.setPalette(pal)
            for btn in corner.findChildren(QToolButton):
                btn.setPalette(pal)
        QApplication.instance().paletteChanged.connect(_on_theme_changed)

        # ---- Frameless window --------------------------------------------
        native_filter = _make_frameless(qwin)

        # ---- Bookkeeping -------------------------------------------------
        self._managed[obj_name] = {
            'corner':         corner,
            'menubar_filter': menubar_filter,
            'state_filter':   state_filter,
            'native_filter':  native_filter,
            'poll_timer':     poll_timer,
            'menubar':        menubar,
            'qwin':           qwin,
        }
        window.windowClosed.connect(
            lambda on=obj_name: self._teardown_window(on))

    def _teardown_window(self, obj_name: str):
        if obj_name not in self._managed:
            return
        d = self._managed.pop(obj_name)
        d['poll_timer'].stop()
        d['menubar'].removeEventFilter(d['menubar_filter'])
        d['qwin'].removeEventFilter(d['state_filter'])
        if d['native_filter'] is not None:
            QCoreApplication.instance().removeNativeEventFilter(
                d['native_filter'])


_EXT = CompactTitlebarExtension(Krita.instance())
Krita.instance().addExtension(_EXT)
