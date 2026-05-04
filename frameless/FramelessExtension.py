"""
Frameless — Krita Plugin  (Windows 10+ only)
==============================================

Replaces the native Windows titlebar with a compact, configurable
custom titlebar.  The original menubar is kept intact but cleared
of its actions; the titlebar is set as a TopLeftCorner widget
with a Resize filter forcing full width.

Component layout is driven by config.json — see components/ for
the individual section implementations.

How it works (high level)
-------------------------
1. Win32 style manipulation — remove the caption but keep the resize frame
2. DWM frame extension — drop shadows via DwmExtendFrameIntoClientArea
3. Save all QMenu objects, clear the original QMenuBar, then set the
   custom _TitleBar as TopLeftCorner + Resize filter for full width
4. Dragging on non-button areas of the _TitleBar moves the window;
   double-click toggles maximise.

Why not Qt.FramelessWindowHint?
-------------------------------
On Windows, QMainWindow.setWindowFlags(Qt.FramelessWindowHint) sets the
underlying HWND to WS_POPUP style — no WM_NCHITTEST → no resize cursors.
We manually remove only WS_CAPTION, keeping WS_THICKFRAME alive.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import partial
from typing import Dict

from krita import *
from PyQt5.QtCore import (
    Qt, QEvent, QObject, QAbstractNativeEventFilter, QCoreApplication, QTimer,
)
from PyQt5.QtGui import QMouseEvent, QPalette
from PyQt5.QtWidgets import (
    QToolButton, QWidget, QHBoxLayout, QMainWindow,
    QMenuBar, QSizePolicy, QApplication,
)

from .components import COMPONENT_REGISTRY, load_config


# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration — core constants (component-specific ones are in components/)
# ═══════════════════════════════════════════════════════════════════════════════

RESIZE_BORDER_PX       = 6
MONITOR_DEFAULTTONEAREST = 2
DRAG_THRESHOLD_PX      = 5
DWM_TOP_MARGIN         = 1
CORNER_POLL_MS         = 100
TITLEBAR_PAD_LEFT      = 4       # px left padding of the titlebar


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FRAMELESS WINDOW                                                           ║
# ║  All Windows-specific code that removes the native titlebar while           ║
# ║  keeping resize edges, Aero Snap, and DWM shadows.                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class _MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth",    ctypes.c_int),
        ("cxRightWidth",   ctypes.c_int),
        ("cyTopHeight",    ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def _dwm_extend(hwnd: int) -> bool:
    try:
        margins = _MARGINS(0, DWM_TOP_MARGIN, 0, 0)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            hwnd, ctypes.byref(margins))
        return True
    except Exception:
        return False


GWL_STYLE      = -16
GWL_EXSTYLE    = -20
WS_CAPTION     = 0x00C00000
WS_SYSMENU     = 0x00080000
WS_THICKFRAME  = 0x00040000
WS_EX_NOREDIRECTIONBITMAP = 0x00200000

SWP_FLAGS = 0x0020 | 0x0002 | 0x0001 | 0x0004

WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE    = 0x0083
WM_NCHITTEST     = 0x0084

HTLEFT = 10; HTRIGHT = 11; HTTOP = 12; HTTOPLEFT = 13
HTTOPRIGHT = 14; HTBOTTOM = 15; HTBOTTOMLEFT = 16; HTBOTTOMRIGHT = 17


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
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    style &= ~WS_CAPTION
    style |= WS_THICKFRAME
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)

    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style &= ~WS_EX_NOREDIRECTIONBITMAP
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)

    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)


class _WinFrameFilter(QAbstractNativeEventFilter):
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

        if msg.message == WM_NCCALCSIZE:
            if msg.wParam:
                return True, 0
            return False, 0

        if msg.message == WM_NCHITTEST:
            if self._qwin.isMaximized():
                return False, 0
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            g = self._qwin.geometry()
            rx, ry, rw, rh = x - g.x(), y - g.y(), g.width(), g.height()
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


def _make_frameless(qwin: QMainWindow):
    hwnd = int(qwin.winId())
    _dwm_extend(hwnd)
    _remove_caption(hwnd)
    nc_filter = _WinFrameFilter(qwin)
    QCoreApplication.instance().installNativeEventFilter(nc_filter)
    return nc_filter


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CUSTOM TITLEBAR  (QWidget — set as TopLeftCorner of the cleared menubar)   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_TITLEBAR_STYLESHEET = """
    QWidget#frameless-titlebar {
        background: palette(window);
    }
"""


class _TitleBar(QWidget):
    """Custom titlebar placed as a TopLeftCorner widget of the original
    (cleared) QMenuBar.  A Resize event filter forces it to full width.

    Layout is driven by config.json → COMPONENT_REGISTRY.
    """

    DRAG_THRESHOLD = DRAG_THRESHOLD_PX

    def __init__(self, window: Window, original_menubar: QMenuBar,
                 layout_config: list, parent=None):
        super().__init__(parent)
        qwin = window.qwindow()
        bar_h = original_menubar.height()

        self._qwin = qwin
        self._original_menubar = original_menubar
        self._press_pos = None

        self.setObjectName("frameless-titlebar")
        self.setFixedHeight(bar_h)
        self.setStyleSheet(_TITLEBAR_STYLESHEET)

        # ---- Build sections from config.json ----------------------------
        layout = QHBoxLayout(self)
        layout.setContentsMargins(TITLEBAR_PAD_LEFT, 0, 0, 0)
        layout.setSpacing(0)

        for item in layout_config:
            name = item['name']
            cfg  = item.get('config', {})
            factory = COMPONENT_REGISTRY[name]
            widget = factory(window, bar_h, cfg)
            layout.addWidget(widget)

        # ---- Drag & double-click ----------------------------------------
        # (handled by mouse events on this QWidget — buttons excluded)

        # ---- Window-state tracking for maximise icon --------------------
        self._state_filter = _WindowStateFilter(self._on_state_changed)
        qwin.installEventFilter(self._state_filter)

        # ---- Palette ----------------------------------------------------
        self._apply_menubar_palette()
        QApplication.instance().paletteChanged.connect(
            self._apply_menubar_palette)

    # -- Palette ----------------------------------------------------------

    def _apply_menubar_palette(self):
        try:
            pal = self._original_menubar.palette()
            self.setPalette(pal)
            for child in self.findChildren(QWidget):
                if hasattr(child, 'apply_palette'):
                    child.apply_palette(pal)
        except Exception:
            pass

    # -- Drag & double-click ----------------------------------------------

    def _is_interactive_child(self, pos):
        child = self.childAt(pos)
        if child is None:
            return False
        return isinstance(child, QToolButton)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            if not self._is_interactive_child(event.pos()):
                self._press_pos = event.globalPos()
                event.ignore()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._press_pos is not None:
            delta = event.globalPos() - self._press_pos
            if delta.manhattanLength() >= self.DRAG_THRESHOLD:
                self._press_pos = None
                handle = self._qwin.windowHandle()
                if handle is not None and hasattr(handle, 'startSystemMove'):
                    handle.startSystemMove()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            if not self._is_interactive_child(event.pos()):
                self._press_pos = None
                if self._qwin.isMaximized():
                    self._qwin.showNormal()
                else:
                    self._qwin.showMaximized()
                return
        super().mouseDoubleClickEvent(event)

    # -- Window state callback --------------------------------------------

    def _on_state_changed(self):
        for child in self.findChildren(QWidget):
            if hasattr(child, 'update_maximize_icon'):
                child.update_maximize_icon()

    # -- Cleanup ----------------------------------------------------------

    def teardown(self):
        for child in self.findChildren(QWidget):
            if hasattr(child, 'teardown'):
                child.teardown()
        self._qwin.removeEventFilter(self._state_filter)
        try:
            QApplication.instance().paletteChanged.disconnect(
                self._apply_menubar_palette)
        except Exception:
            pass


# --- Resize filter for TopLeftCorner full-width trick -----------------------

class _CornerResizeFilter(QObject):
    def __init__(self, menubar: QMenuBar, titlebar: QWidget):
        super().__init__()
        self._menubar = menubar
        self._titlebar = titlebar

    def eventFilter(self, obj: QObject, event: QEvent):
        if obj is self._menubar and event.type() == QEvent.Resize:
            self._titlebar.setFixedWidth(self._menubar.width())
        return False


# --- Window-state event filter ------------------------------------------------

class _WindowStateFilter(QObject):
    def __init__(self, callback):
        super().__init__()
        self._cb = callback

    def eventFilter(self, obj: QObject, event: QEvent):
        if event.type() == QEvent.WindowStateChange:
            self._cb()
        return super().eventFilter(obj, event)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  KRITA PLUGIN ENTRY POINT                                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class FramelessExtension(Extension):

    def __init__(self, parent):
        super().__init__(parent)
        self._managed: Dict[str, dict] = {}

    def setup(self):
        pass

    def createActions(self, window: Window):
        win_obj_name = window.objectName()

        @partial(QTimer.singleShot, 0)
        def _():
            for w in Krita.instance().windows():
                if w.objectName() == win_obj_name:
                    window_ref = w
                    break
            else:
                return

            qwin = window_ref.qwindow()
            original_menubar = qwin.menuBar()
            if original_menubar is None:
                return

            obj_name = qwin.objectName()
            if obj_name in self._managed:
                self._teardown_window(obj_name)

            # ---- Load layout from config.json ---------------------------
            try:
                layout_config = load_config()
            except Exception as e:
                import traceback
                traceback.print_exc()
                return

            # ---- Build the custom titlebar -------------------------------
            # Components are created NOW — menubar component extracts
            # QMenu objects from original_menubar synchronously.
            titlebar = _TitleBar(window_ref, original_menubar, layout_config)

            # ---- Clear the original menubar (menus already extracted) ---
            original_menubar.clear()

            # ---- Set titlebar as TopLeftCorner + full-width Resize ------
            original_menubar.setCornerWidget(titlebar, Qt.TopLeftCorner)
            titlebar.show()
            resize_filter = _CornerResizeFilter(original_menubar, titlebar)
            original_menubar.installEventFilter(resize_filter)

            # ---- Polling guard: prevent Krita MDI from stealing corner --
            poll_timer = QTimer(qwin)
            def _poll_corner():
                cur = original_menubar.cornerWidget(Qt.TopLeftCorner)
                if cur is not titlebar:
                    if cur is not None:
                        cur.hide()
                    original_menubar.setCornerWidget(titlebar, Qt.TopLeftCorner)
                    titlebar.show()
                
                # always hide top right corner
                if tr := original_menubar.cornerWidget(Qt.TopRightCorner):
                    tr.hide()
            poll_timer.timeout.connect(_poll_corner)
            poll_timer.start(CORNER_POLL_MS)

            # ---- Frameless window ----------------------------------------
            native_filter = _make_frameless(qwin)

            # ---- Bookkeeping ---------------------------------------------
            self._managed[obj_name] = {
                'titlebar':       titlebar,
                'native_filter':  native_filter,
                'qwin':           qwin,
                'menubar':        original_menubar,
                'resize_filter':  resize_filter,
                'poll_timer':     poll_timer,
            }
            window_ref.windowClosed.connect(
                lambda on=obj_name: self._teardown_window(on))

    def _teardown_window(self, obj_name: str):
        if obj_name not in self._managed:
            return
        d = self._managed.pop(obj_name)

        d['menubar'].removeEventFilter(d['resize_filter'])
        d['menubar'].setCornerWidget(None, Qt.TopLeftCorner)
        d['poll_timer'].stop()
        d['titlebar'].teardown()
        if d['native_filter'] is not None:
            QCoreApplication.instance().removeNativeEventFilter(
                d['native_filter'])

_EXT = FramelessExtension(Krita.instance())
Krita.instance().addExtension(_EXT)
