"""
Frameless — Krita Plugin  (Windows 10+ only)
==============================================

Replaces the native Windows titlebar with a compact, configurable
custom titlebar that sits in the menu bar area.  The original
menubar menus are migrated into a real QMenuBar widget placed
inside the custom titlebar, so keyboard shortcuts (Alt+letter),
hover-to-switch, and keyboard navigation all work natively.

How it works (high level)
-------------------------
1. Win32 style manipulation — remove the caption but keep the resize frame
   so that Windows still sends WM_NCHITTEST (which lets us implement edge
   resizing) and Aero Snap still works.
2. DWM frame extension — tells the Desktop Window Manager "we're doing
   custom chrome" so it renders drop shadows.
3. A custom _TitleBar widget (set via QMainWindow.setMenuWidget)
   replaces the menubar area.  It contains:
   - CurrentFileName  — QLabel polling Krita.instance().activeDocument()
   - OriginalMenuBar  — a real QMenuBar with the original QMenu objects
   - Spacer           — expanding empty space
   - WindowControl    — minimise / maximise / close buttons
4. Dragging on non-button areas of the _TitleBar moves the window;
   double-click toggles maximise.

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
from functools import partial
from typing import Dict, List

from krita import *
from PyQt5.QtCore import (
    Qt, QEvent, QObject, QAbstractNativeEventFilter, QCoreApplication,
    QTimer, QSize,
)
from PyQt5.QtGui import QMouseEvent, QPalette
from PyQt5.QtWidgets import (
    QToolButton, QWidget, QHBoxLayout, QMainWindow, QLabel,
    QMenuBar, QMenu, QSizePolicy, QApplication,
)


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

# Corner widget polling (Krita MDI subwindow may steal it)
CORNER_POLL_MS        = 100     # ms between corner widget checks

# Titlebar layout — hardcoded section order (config UI in future release)
TITLE_LAYOUT = [
    'CurrentFileName',
    'Spacer',
    'OriginalMenuBar',
    'Spacer',
    'WindowControl',
]

# File name polling
FILENAME_POLL_MS      = 500     # ms between filename checks

# Window control buttons — visual
BTN_WIDTH             = 60      # px
BTN_TEXT_MINIMISE     = "\u2500"  # ─
BTN_TEXT_MAXIMISE     = "\u25A1"  # □
BTN_TEXT_RESTORE      = "\u2750"  # ❐
BTN_TEXT_CLOSE        = "\u2715"  # ✕
CLOSE_HOVER_BG        = "#E81123" # Windows-native red

# Titlebar left padding (before first widget)
TITLEBAR_PAD_LEFT     = 4       # px


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
# ║  CUSTOM TITLEBAR                                                            ║
# ║  A QWidget set via QMainWindow.setMenuWidget() that replaces the menubar    ║
# ║  area.  Its QHBoxLayout holds configurable sections.                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# --- Stylesheet for window control buttons -----------------------------------

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

_TITLEBAR_STYLESHEET = """
    QWidget#frameless-titlebar {
        background: palette(window);
    }
"""


# --- Section: CurrentFileName -------------------------------------------------

class _FileNameSection(QLabel):
    """Polls Krita for the active document name every FILENAME_POLL_MS ms."""

    def __init__(self, bar_height: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(bar_height)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(FILENAME_POLL_MS)

    def _refresh(self):
        """Read current document name from Krita."""
        try:
            doc = Krita.instance().activeDocument()
            if doc is not None:
                fname = doc.fileName()
                self.setText(fname if fname else "")
            else:
                self.setText("")
        except Exception:
            self.setText("")

    def stop(self):
        """Stop the poll timer."""
        self._timer.stop()


# --- Section: OriginalMenuBar -------------------------------------------------

class _MenuBarSection(QMenuBar):
    """A real QMenuBar that hosts the original QMenu objects from Krita's
    native menubar.

    SetSizePolicy(Maximum, Fixed) ensures it only takes as much width as
    its menus need, leaving the rest for the Spacer.
    """

    def __init__(self, menus: List[QMenu], bar_height: int, parent=None):
        super().__init__(parent)
        self.setNativeMenuBar(False)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setFixedHeight(bar_height)

        for menu in menus:
            # Reparent the QMenu so it stays alive after the original
            # menubar is replaced via setMenuWidget().
            # menu.setParent(self)
            self.addMenu(menu)


# --- Section: Spacer ----------------------------------------------------------

class _SpacerSection(QWidget):
    """Horizontally-expanding empty space between left and right sections."""

    def __init__(self, bar_height: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(bar_height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


# --- Section: WindowControl ---------------------------------------------------

class _WindowControlSection(QWidget):
    """Minimise / Maximise / Close buttons.

    Keeps the maximise icon in sync with the window state.
    """

    def __init__(self, qwin: QMainWindow, obj_name: str,
                 bar_height: int, parent=None):
        super().__init__(parent)
        self._qwin = qwin
        self._obj_name = obj_name
        self.setFixedHeight(bar_height)

        btn_w = BTN_WIDTH

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # -- Minimise ----------------------------------------------------
        self._b_min = QToolButton(self)
        self._b_min.setText(BTN_TEXT_MINIMISE)
        self._b_min.setObjectName("titlebar-minimize")
        self._b_min.setFixedSize(btn_w, bar_height)
        self._b_min.setToolTip("Minimise")
        self._b_min.setStyleSheet(_BTN_STYLESHEET)
        self._b_min.clicked.connect(qwin.showMinimized)

        # -- Maximise / Restore -------------------------------------------
        self._b_max = QToolButton(self)
        self._b_max.setObjectName("titlebar-maximize")
        self._b_max.setFixedSize(btn_w, bar_height)
        self._b_max.setStyleSheet(_BTN_STYLESHEET)

        def _toggle():
            if qwin.isMaximized():
                qwin.showNormal()
            else:
                qwin.showMaximized()
        self._b_max.clicked.connect(_toggle)

        # -- Close --------------------------------------------------------
        def _on_close():
            for kw in Krita.instance().windows():
                if kw.qwindow().objectName() == obj_name:
                    kw.qwindow().close()
                    return
            qwin.close()

        self._b_close = QToolButton(self)
        self._b_close.setText(BTN_TEXT_CLOSE)
        self._b_close.setObjectName("titlebar-close")
        self._b_close.setFixedSize(btn_w, bar_height)
        self._b_close.setToolTip("Close")
        self._b_close.setStyleSheet(_BTN_STYLESHEET)
        self._b_close.clicked.connect(_on_close)

        lay.addWidget(self._b_min)
        lay.addWidget(self._b_max)
        lay.addWidget(self._b_close)

        self.update_maximize_icon()

    def update_maximize_icon(self):
        """Sync button text/tooltip with current window state."""
        if self._qwin.isMaximized():
            self._b_max.setText(BTN_TEXT_RESTORE)
            self._b_max.setToolTip("Restore")
        else:
            self._b_max.setText(BTN_TEXT_MAXIMISE)
            self._b_max.setToolTip("Maximize")

    def apply_palette(self, pal: QPalette):
        """Propagate a palette to all child buttons."""
        self.setPalette(pal)
        for btn in self.findChildren(QToolButton):
            btn.setPalette(pal)


# --- Main: TitleBar (QWidget) -------------------------------------------------

class _TitleBar(QWidget):
    """Custom titlebar that replaces the QMainWindow menubar area.

    Layout is driven by title_layout.  Handles drag-to-move and
    double-click-to-maximise on non-interactive areas.
    """

    DRAG_THRESHOLD = DRAG_THRESHOLD_PX

    def __init__(self, qwin: QMainWindow, obj_name: str,
                 original_menubar: QMenuBar,
                 menus: List[QMenu],
                 title_layout: List[str],
                 parent=None):
        super().__init__(parent)
        self._qwin = qwin
        self._obj_name = obj_name
        self._original_menubar = original_menubar
        self._title_layout = title_layout
        self._press_pos = None
        self._filename = None       # _FileNameSection (for teardown)
        self._wc = None             # _WindowControlSection (for palette/teardown)

        bar_h = original_menubar.height()

        self.setObjectName("frameless-titlebar")
        self.setFixedHeight(bar_h)
        self.setStyleSheet(_TITLEBAR_STYLESHEET)

        # ---- Build the layout from title_layout -------------------------
        layout = QHBoxLayout(self)
        layout.setContentsMargins(TITLEBAR_PAD_LEFT, 0, 0, 0)
        layout.setSpacing(0)

        self._sections: Dict[str, QWidget] = {}

        for section_name in title_layout:
            if section_name == 'CurrentFileName':
                self._filename = _FileNameSection(bar_h, self)
                layout.addWidget(self._filename)
                self._sections[section_name] = self._filename
            elif section_name == 'OriginalMenuBar':
                w = _MenuBarSection(menus, bar_h, self)
                layout.addWidget(w)
                self._sections[section_name] = w
            elif section_name == 'Spacer':
                w = _SpacerSection(bar_h, self)
                layout.addWidget(w)
                self._sections[section_name] = w
            elif section_name == 'WindowControl':
                self._wc = _WindowControlSection(qwin, obj_name, bar_h, self)
                layout.addWidget(self._wc)
                self._sections[section_name] = self._wc

        # ---- Window-state tracking for maximise icon --------------------
        self._state_filter = _WindowStateFilter(self._on_state_changed)
        qwin.installEventFilter(self._state_filter)

        # ---- Initial palette from the original menubar ------------------
        self._apply_menubar_palette()

        # ---- Theme change → re-apply palette ----------------------------
        QApplication.instance().paletteChanged.connect(
            self._apply_menubar_palette)

    # -- Palette ----------------------------------------------------------

    def _apply_menubar_palette(self):
        """Read the palette from the saved original menubar
        and propagate to sections that need it."""
        try:
            pal = self._original_menubar.palette()
            self.setPalette(pal)
            if self._wc is not None:
                self._wc.apply_palette(pal)
        except Exception:
            pass

    # -- Drag & double-click ----------------------------------------------

    def _is_interactive_child(self, pos):
        """Return True if the position is over a button that should
        consume mouse events (not start a drag)."""
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
        if self._wc is not None:
            self._wc.update_maximize_icon()

    # -- Cleanup ----------------------------------------------------------

    def teardown(self):
        """Stop timers, disconnect signals, remove event filters."""
        if self._filename is not None:
            self._filename.stop()
        self._qwin.removeEventFilter(self._state_filter)
        try:
            QApplication.instance().paletteChanged.disconnect(
                self._apply_menubar_palette)
        except Exception:
            pass


# --- Resize filter for TopLeftCorner full-width trick -----------------------

class _CornerResizeFilter(QObject):
    """Force the TopLeftCorner widget to match the menubar width on resize."""

    def __init__(self, menubar: QMenuBar, titlebar: QWidget):
        super().__init__()
        self._menubar = menubar
        self._titlebar = titlebar

    def eventFilter(self, obj: QObject, event: QEvent):
        if obj is self._menubar and event.type() == QEvent.Resize:
            print('!!!', self._menubar.width())
            self._titlebar.setFixedWidth(self._menubar.width())
        return False


# --- Window-state event filter ------------------------------------------------

class _WindowStateFilter(QObject):
    """Installed on QMainWindow to detect WindowStateChange events."""

    def __init__(self, callback):
        super().__init__()
        self._cb = callback

    def eventFilter(self, obj: QObject, event: QEvent):
        if event.type() == QEvent.WindowStateChange:
            self._cb()
        return super().eventFilter(obj, event)


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
        win_obj_name = window.objectName()

        # Deferred via QTimer.singleShot — synchronous menubar mutation
        # can cause a segfault in Krita's event loop.
        @partial(QTimer.singleShot, 0)
        def _():
            # Re-acquire the window wrapper (Krita wrappers are ephemeral)
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

            # ---- Save menus, then clear the original menubar ------------
            saved_menus: List[QMenu] = []
            for action in original_menubar.actions():
                m = action.menu()
                if m is not None:
                    saved_menus.append(m)
            original_menubar.clear()

            # ---- Build the custom titlebar -------------------------------
            titlebar = _TitleBar(qwin, obj_name, original_menubar,
                                 saved_menus, TITLE_LAYOUT)

            # ---- Set as TopLeftCorner + Resize filter → full width -------
            if old_one := original_menubar.cornerWidget(Qt.TopLeftCorner):
                old_one.hide()
            original_menubar.setCornerWidget(titlebar, Qt.TopLeftCorner)
            titlebar.show()
            
            resize_filter = _CornerResizeFilter(original_menubar, titlebar)
            original_menubar.installEventFilter(resize_filter)

            # ---- Polling guard: Krita MDI subwindow may steal corner ----
            poll_timer = QTimer(qwin)
            def _poll_corner():
                cur = original_menubar.cornerWidget(Qt.TopLeftCorner)
                if cur is not titlebar:
                    if cur is not None:
                        cur.hide()
                    original_menubar.setCornerWidget(titlebar, Qt.TopLeftCorner)
                    titlebar.show()
                
                # then, hide top right corner
                if tr := original_menubar.cornerWidget(Qt.TopRightCorner):
                    tr.hide()
            poll_timer.timeout.connect(_poll_corner)
            poll_timer.start(CORNER_POLL_MS)

            # ---- Frameless window ----------------------------------------
            native_filter = _make_frameless(qwin)

            # ---- Bookkeeping ---------------------------------------------
            self._managed[obj_name] = {
                'titlebar':        titlebar,
                'native_filter':   native_filter,
                'qwin':            qwin,
                'menubar':         original_menubar,
                'resize_filter':   resize_filter,
                'poll_timer':      poll_timer,
            }
            window_ref.windowClosed.connect(
                lambda on=obj_name: self._teardown_window(on))

    def _teardown_window(self, obj_name: str):
        if obj_name not in self._managed:
            return
        d = self._managed.pop(obj_name)

        # Remove Resize filter and clear corner widget
        d['menubar'].removeEventFilter(d['resize_filter'])
        d['menubar'].setCornerWidget(None, Qt.TopLeftCorner)
        d['poll_timer'].stop()

        d['titlebar'].teardown()
        if d['native_filter'] is not None:
            QCoreApplication.instance().removeNativeEventFilter(
                d['native_filter'])


_EXT = CompactTitlebarExtension(Krita.instance())
Krita.instance().addExtension(_EXT)
