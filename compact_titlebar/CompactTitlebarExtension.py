"""
Compact Titlebar — Krita Plugin  (Windows 10+ only)
=====================================================

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
#  Part 1 — Windows-specific helpers (ctypes / Win32)
# ═══════════════════════════════════════════════════════════════════════════════

# --- DWM (Desktop Window Manager) ---------------------------------------------
#
# DwmExtendFrameIntoClientArea tells DWM "I'm painting custom window chrome".
# In return DWM provides the standard window drop-shadow (on Windows 10/11).
# The MARGINS structure defines how far into the client area the frame is
# extended on each side.  We use (0, 1, 0, 0): 1 px at the top is enough
# to signal "custom chrome" without adding any visible frame.

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
        margins = _MARGINS(0, 1, 0, 0)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            hwnd, ctypes.byref(margins))
        return True
    except Exception:
        return False


# --- Win32 window style constants ---------------------------------------------
#
# Every Windows HWND has a set of style bits (GetWindowLongW / SetWindowLongW).
# We manipulate these bits directly to:
#   - Remove the titlebar   (clear WS_CAPTION)
#   - Keep resize borders   (set WS_THICKFRAME)
#
# WS_CAPTION  = 0x00C00000    includes the titlebar and system-button area
# WS_SYSMENU  = 0x00080000    the system menu (Alt+Space) — we keep this
# WS_THICKFRAME = 0x00040000  the resizeable border frame — KEPT for hit-testing
# WS_EX_NOREDIRECTIONBITMAP = 0x00200000 — remove to avoid rendering artifacts

GWL_STYLE      = -16         # parameter for GetWindowLongW: standard style
GWL_EXSTYLE    = -20         # parameter for GetWindowLongW: extended style
WS_CAPTION     = 0x00C00000
WS_SYSMENU     = 0x00080000
WS_THICKFRAME  = 0x00040000
WS_EX_NOREDIRECTIONBITMAP = 0x00200000

# SetWindowPos flags: tell Windows to recalculate the non-client area
# after we've changed the window styles, without moving or resizing.
SWP_FLAGS = 0x0020 | 0x0002 | 0x0001 | 0x0004
#             FRAMECHANGED | NOMOVE  | NOSIZE  | NOZORDER


# --- Windows messages we intercept -------------------------------------------
#
# These are raw Win32 messages sent to the HWND's window procedure.  We
# intercept them via Qt's QAbstractNativeEventFilter, which runs before
# Qt's own event processing.
#
# WM_GETMINMAXINFO  — Windows asking "what's the max size/position when maximised?"
# WM_NCCALCSIZE     — Windows asking "what's the boundary between non-client (NC)
#                      area and client area?"
# WM_NCHITTEST      — Windows asking "which part of the NC area is the mouse on?"
#                      We use this to provide wider resize edges.

WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE    = 0x0083
WM_NCHITTEST     = 0x0084

# Return values for WM_NCHITTEST.  Each tells Windows "the mouse is on a
# resize edge / corner" so Windows draws the correct cursor and handles
# the drag-to-resize operation natively.

HTLEFT         = 10      # left edge
HTRIGHT        = 11      # right edge
HTTOP          = 12      # top edge
HTTOPLEFT      = 13      # top-left corner
HTTOPRIGHT     = 14      # top-right corner
HTBOTTOM       = 15      # bottom edge
HTBOTTOMLEFT   = 16      # bottom-left corner
HTBOTTOMRIGHT  = 17      # bottom-right corner


# --- ctypes structures for Win32 API calls ------------------------------------

class _MONITORINFO(ctypes.Structure):
    """Used with GetMonitorInfoW to get monitor dimensions.
    rcMonitor = full monitor rect; rcWork = work area (excludes taskbar)."""
    _fields_ = [
        ("cbSize",    wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork",    wintypes.RECT),
        ("dwFlags",   wintypes.DWORD),
    ]


class _MINMAXINFO(ctypes.Structure):
    """Used with WM_GETMINMAXINFO.  ptMaxSize / ptMaxPosition define the
    maximum size and position the window can take when maximised."""
    _fields_ = [
        ("ptReserved",     wintypes.POINT),
        ("ptMaxSize",      wintypes.POINT),
        ("ptMaxPosition",  wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]


# --- Style manipulation -------------------------------------------------------

def _remove_caption(hwnd: int):
    """Remove WS_CAPTION from the window style, keeping WS_THICKFRAME.

    Why this specific combination:
    - WS_CAPTION gone      → no visible titlebar ✓
    - WS_THICKFRAME stays  → Windows still sends WM_NCHITTEST ✓
    - WS_SYSMENU stays     → Alt+Space menu still works (icon hidden by no caption)

    We also remove WS_EX_NOREDIRECTIONBITMAP from the extended style
    to prevent rendering glitches when the DWM frame is extended.
    SetWindowPos(SWP_FRAMECHANGED) triggers Windows to re-layout the
    non-client area based on the new styles.
    """
    user32 = ctypes.windll.user32

    # --- standard style ---
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    style &= ~WS_CAPTION          # "unset" the caption bit
    style |= WS_THICKFRAME         # ensure the resize frame is on
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)

    # --- extended style ---
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style &= ~WS_EX_NOREDIRECTIONBITMAP
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)

    # --- force re-layout ---
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)


# ═══════════════════════════════════════════════════════════════════════════════
#  Part 2 — Native event filter  (WM_GETMINMAXINFO, WM_NCCALCSIZE, WM_NCHITTEST)
# ═══════════════════════════════════════════════════════════════════════════════

class _WinFrameFilter(QAbstractNativeEventFilter):
    """Per-window filter for native Windows messages.

    Installed via QCoreApplication.installNativeEventFilter().
    Qt calls nativeEventFilter() *before* it dispatches the message
    to the widget's nativeEvent() handler, so we get first dibs.

    Three messages handled:

    WM_GETMINMAXINFO
        Windows asks "what are the maximum bounds for this window?"
        We set ptMaxSize / ptMaxPosition to the monitor's *work area*
        (rectangle excluding the taskbar).  This ensures a maximised
        window does NOT cover the taskbar or overflow the screen edge.

    WM_NCCALCSIZE
        Windows asks "how big is the non-client area?"
        Returning (True, 0) means "client rect == window rect" — i.e.,
        the entire window is client area, no visible borders.  Without
        this the WS_THICKFRAME borders would be visible as a thin frame.

    WM_NCHITTEST
        Windows asks "which part of the window is the mouse over?"
        For the outer BORDER (6) pixels on each edge we return the
        appropriate HT* code so Windows shows a resize cursor and
        handles edge-dragging natively.  For everything else we return
        (False, 0) to let Qt / DefWindowProc decide.
    """

    BORDER = 6               # width of the invisible resize zone in pixels

    def __init__(self, qwin: QMainWindow):
        super().__init__()
        self._hwnd = int(qwin.winId())   # native Windows HWND
        self._qwin = qwin

    # -- nativeEventFilter -------------------------------------------------

    def nativeEventFilter(self, event_type: bytes, message):
        # Qt sends us ALL native events (not just window messages).
        # We only care about "windows_generic_MSG".
        if event_type != b"windows_generic_MSG":
            return False, 0

        # Convert the opaque Qt message pointer into a readable Win32 MSG.
        msg = wintypes.MSG.from_address(int(message))
        if msg.hWnd != self._hwnd:
            return False, 0          # not our window → pass through

        # ---- WM_GETMINMAXINFO -------------------------------------------
        # Windows sends this *once* (or on display change) to learn the
        # maximum dimensions for maximised state.  We use the monitor's
        # work area so the maximised window respects the taskbar.
        if msg.message == WM_GETMINMAXINFO:
            # Get the monitor that contains the window (MONITOR_DEFAULTTONEAREST = 2)
            monitor = ctypes.windll.user32.MonitorFromWindow(
                self._hwnd, 2)
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            ctypes.windll.user32.GetMonitorInfoW(
                monitor, ctypes.byref(mi))

            # lParam points to a MINMAXINFO struct
            mmi = ctypes.cast(
                msg.lParam, ctypes.POINTER(_MINMAXINFO)).contents
            mmi.ptMaxPosition.x = mi.rcWork.left
            mmi.ptMaxPosition.y = mi.rcWork.top
            mmi.ptMaxSize.x     = mi.rcWork.right  - mi.rcWork.left
            mmi.ptMaxSize.y     = mi.rcWork.bottom - mi.rcWork.top
            return True, 0          # handled

        # ---- WM_NCCALCSIZE ----------------------------------------------
        # Returning (True, 0) tells Windows "client rect = window rect".
        # Result: no visible border gap around the window content.
        if msg.message == WM_NCCALCSIZE:
            if msg.wParam:          # wParam == TRUE means "recalculate"
                return True, 0
            return False, 0

        # ---- WM_NCHITTEST -----------------------------------------------
        # Translate the mouse position into resize-edge codes so Windows
        # shows the correct cursor and handles edge-dragging.
        if msg.message == WM_NCHITTEST:
            # Maximised windows should NOT be resizable from edges.
            if self._qwin.isMaximized():
                return False, 0

            # lParam encodes x (LOWORD) and y (HIWORD) in screen coords.
            # ctypes.c_short handles sign-extension for multi-monitor setups
            # where coords can be negative (monitors to the left / above).
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value

            # Convert screen coords → window-local coords
            g = self._qwin.geometry()
            rx = x - g.x()
            ry = y - g.y()
            rw = g.width()
            rh = g.height()

            # Determine which edge / corner the cursor is near
            left   = rx < self.BORDER
            right  = rx > rw - self.BORDER
            top    = ry < self.BORDER
            bottom = ry > rh - self.BORDER

            # Return the appropriate hit-test code (order matters:
            # corners checked before edges)
            if top and left:     return True, HTTOPLEFT
            if top and right:    return True, HTTOPRIGHT
            if bottom and left:  return True, HTBOTTOMLEFT
            if bottom and right: return True, HTBOTTOMRIGHT
            if left:             return True, HTLEFT
            if right:            return True, HTRIGHT
            if top:              return True, HTTOP
            if bottom:           return True, HTBOTTOM

        # Not our message → let Qt / DefWindowProc handle it
        return False, 0


# ═══════════════════════════════════════════════════════════════════════════════
#  Part 3 — Menubar event filter  (drag + double-click)
# ═══════════════════════════════════════════════════════════════════════════════

class _MenubarEventFilter(QObject):
    """Installed on QMenuBar.  Intercepts mouse events on *empty* menubar
    space (where no QAction sits, e.g. to the right of the "Help" menu).

    - Drag:   starts on MouseMove after a 5px threshold (not on press).
              This preserves double-click detection.
    - Double-click: toggles maximise / restore.
    - Menu-item clicks pass through normally (we check actionAt()).
    """

    DRAG_THRESHOLD = 5       # pixels of movement before drag starts

    def __init__(self, qwin: QMainWindow):
        super().__init__()
        self._qwin      = qwin
        self._press_pos = None   # global position of the initial mouse press

    # -- eventFilter -------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent):
        """Qt calls this for every event delivered to the menubar.
        Returning True means "I handled it, don't pass it on".
        Returning False means "I didn't handle it, keep going"."""
        etype = event.type()

        # -- Mouse press on empty space -----------------------------------
        # Record the press position so MouseMove can detect a drag.
        # We do NOT consume the event here — otherwise the menubar would
        # never see the press and Qt couldn't detect a double-click.
        if etype == QEvent.MouseButtonPress:
            if obj.actionAt(event.pos()) is None:
                self._press_pos = event.globalPos()
                return False
            # Press is on a menu action (e.g. "File") → pass through
            return super().eventFilter(obj, event)

        # -- Mouse move: check for drag start ------------------------------
        if etype == QEvent.MouseMove:
            if self._press_pos is not None:
                delta = event.globalPos() - self._press_pos
                if delta.manhattanLength() >= self.DRAG_THRESHOLD:
                    self._press_pos = None

                    # Before starting the native window drag we must cancel
                    # the menubar's internal "pending press" state.  If we
                    # don't, the real mouse-up after the drag will be
                    # interpreted as a click on whatever is under the cursor
                    # (which after an un-maximise drag may be a menu action).
                    QCoreApplication.sendEvent(obj, QMouseEvent(
                        QEvent.MouseButtonRelease,
                        event.localPos(), event.screenPos(),
                        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))

                    # Tell the OS to start a window move.
                    # Qt's startSystemMove() uses the Win32
                    # DefWindowProc(WM_SYSCOMMAND, SC_MOVE | HTCAPTION)
                    # mechanism, which triggers Aero Snap as a bonus.
                    handle = self._qwin.windowHandle()
                    if handle is not None and hasattr(handle, 'startSystemMove'):
                        handle.startSystemMove()
                    return True
            return super().eventFilter(obj, event)

        # -- Mouse release: clear press tracking ---------------------------
        if etype == QEvent.MouseButtonRelease:
            self._press_pos = None
            return super().eventFilter(obj, event)

        # -- Double-click: toggle maximise / restore -----------------------
        if etype == QEvent.MouseButtonDblClick:
            self._press_pos = None
            if obj.actionAt(event.pos()) is None:
                if self._qwin.isMaximized():
                    self._qwin.showNormal()
                else:
                    self._qwin.showMaximized()
                return True

        return super().eventFilter(obj, event)


# ═══════════════════════════════════════════════════════════════════════════════
#  Part 4 — Window-state filter  (keep maximise button icon in sync)
# ═══════════════════════════════════════════════════════════════════════════════

class _WindowStateFilter(QObject):
    """Detects when the window is maximised or restored (e.g. via Win+Up,
    double-click, or Aero Snap) and calls the callback to update the
    maximise button icon.

    We cannot simply subscribe to a "windowStateChanged" signal because
    QMainWindow doesn't emit one.  Instead we intercept QEvent.WindowStateChange
    at the widget level.
    """

    def __init__(self, on_state_changed):
        super().__init__()
        self._cb = on_state_changed

    def eventFilter(self, obj: QObject, event: QEvent):
        if event.type() == QEvent.WindowStateChange:
            self._cb()
        return super().eventFilter(obj, event)


# ═══════════════════════════════════════════════════════════════════════════════
#  Part 5 — Krita Extension  (the plugin entry point)
# ═══════════════════════════════════════════════════════════════════════════════

class CompactTitlebarExtension(Extension):
    """Krita plugin that replaces the native titlebar with a compact one.

    Lifecycle per Krita's plugin API:
      1. __init__()          — once per plugin load
      2. setup()             — once on first load
      3. createActions(win)  — once per window (including windows created later)
    """

    def __init__(self, parent):
        super().__init__(parent)
        # We track our injected objects per window.
        # Key = QMainWindow.objectName() (a stable string) rather than the
        # Krita Window wrapper (which is unhashable and can be GC'd).
        self._managed: Dict[str, dict] = {}

    # -- lifecycle ------------------------------------------------------------

    def setup(self):
        """Called once when the plugin is loaded.  Nothing to do yet."""
        pass

    def createActions(self, window: Window):
        """Called for each Krita main window.  This is where we inject all
        our customizations onto the window's QMainWindow."""
        qwin    = window.qwindow()           # the actual QMainWindow Qt widget
        menubar = qwin.menuBar()             # its menu bar
        if menubar is None:
            return

        # Use the QMainWindow's objectName() as a stable identifier.
        # Krita's Window wrapper is ephemeral (can be GC'd at any time).  --  see MEMORY.md
        obj_name = qwin.objectName()

        # Guard against double-setup (e.g. the plugin being reloaded).
        if obj_name in self._managed:
            self._teardown_window(obj_name)

        # ---- 1. Corner widget: minimise / maximise / close buttons --------
        corner, btn_max = _make_window_controls(qwin, obj_name, menubar)
        menubar.setCornerWidget(corner, Qt.TopRightCorner)

        # ---- 2. Menubar event filter: drag + double-click -----------------
        menubar_filter = _MenubarEventFilter(qwin)
        menubar.installEventFilter(menubar_filter)

        # ---- 3. Window-state filter: keep maximise icon in sync -----------
        def _update_max():
            if qwin.isMaximized():
                btn_max.setText("\u2750")      # ❐  (restore icon)
                btn_max.setToolTip("Restore")
            else:
                btn_max.setText("\u25A1")      # □  (maximise icon)
                btn_max.setToolTip("Maximize")

        state_filter = _WindowStateFilter(_update_max)
        qwin.installEventFilter(state_filter)

        # ---- 4. Frameless window -----------------------------------------
        native_filter = _make_frameless(qwin)

        # Store everything for cleanup
        self._managed[obj_name] = {
            'corner':         corner,
            'menubar_filter': menubar_filter,
            'state_filter':   state_filter,
            'native_filter':  native_filter,
            'menubar':        menubar,
            'qwin':           qwin,
        }

        # Clean up when the Krita window closes.
        # The lambda uses a default-argument capture (on=obj_name) to avoid
        # the classic late-binding closure bug where all lambdas reference
        # the last value of obj_name.
        window.windowClosed.connect(
            lambda on=obj_name: self._teardown_window(on))

    def _teardown_window(self, obj_name: str):
        """Remove all injected filters and widgets for a closing window."""
        if obj_name not in self._managed:
            return
        d = self._managed.pop(obj_name)
        d['menubar'].removeEventFilter(d['menubar_filter'])
        d['qwin'].removeEventFilter(d['state_filter'])
        if d['native_filter'] is not None:
            QCoreApplication.instance().removeNativeEventFilter(
                d['native_filter'])


# ═══════════════════════════════════════════════════════════════════════════════
#  Frameless helper
# ═══════════════════════════════════════════════════════════════════════════════

def _make_frameless(qwin: QMainWindow):
    """Remove the native titlebar and prepare the window for custom chrome.

    Three steps (order matters):
      1. DWM extend  — tell DWM to draw shadows
      2. Win32 style — remove WS_CAPTION (titlebar) but keep WS_THICKFRAME
      3. Native filter — intercept WM_NCCALCSIZE / WM_NCHITTEST / WM_GETMINMAXINFO

    Returns the native event filter instance for later cleanup.
    """
    hwnd = int(qwin.winId())     # native Windows HWND

    _dwm_extend(hwnd)
    _remove_caption(hwnd)

    nc_filter = _WinFrameFilter(qwin)
    QCoreApplication.instance().installNativeEventFilter(nc_filter)

    return nc_filter


# ═══════════════════════════════════════════════════════════════════════════════
#  Window control buttons
# ═══════════════════════════════════════════════════════════════════════════════

# Stylesheet for the three window-control QToolButtons.
# Uses the system palette for text colour so it adapts to Krita's theme.
# The close button gets a red hover background (matching the native behaviour).

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
    """Create the minimise / maximise / close button widget.

    The widget is placed in the menu bar's top-right corner via
    QMenuBar.setCornerWidget().  Button heights match the menu bar height
    so nothing overflows or gets clipped.

    Returns (widget, maximise_button) — the button reference is needed
    so the window-state filter can update its icon.
    """
    bar_h = menubar.height()     # match the menu bar's actual height
    btn_w = 60

    # Container widget
    w = QWidget()
    w.setObjectName("compact-titlebar-controls")
    w.setFixedHeight(bar_h)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    # -- Minimise button ---------------------------------------------------
    b_min = QToolButton()
    b_min.setText("\u2500")            # "─" (horizontal bar, minimal)
    b_min.setObjectName("titlebar-minimize")
    b_min.setFixedSize(btn_w, bar_h)
    b_min.setToolTip("Minimise")
    b_min.setStyleSheet(_BTN_STYLE)
    # Qt's showMinimized() directly minimises the QMainWindow.
    b_min.clicked.connect(qwin.showMinimized)

    # -- Maximise / Restore button -----------------------------------------
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

    # -- Close button ------------------------------------------------------
    # Important: Krita's Window.close() triggers save-prompt dialogs for
    # unsaved documents.  Qt's QMainWindow.close() does not.
    # Because Krita's Window wrapper is ephemeral (cannot be captured in a
    # closure — see MEMORY.md), we look it up at click time by matching
    # the qwindow's objectName().
    def _on_close():
        for w in Krita.instance().windows():
            if w.qwindow().objectName() == obj_name:
                w.close()     # Krita's close — with save prompts
                return
        qwin.close()          # fallback (shouldn't normally be reached)

    b_close = QToolButton()
    b_close.setText("\u2715")          # "✕" (cross)
    b_close.setObjectName("titlebar-close")
    b_close.setFixedSize(btn_w, bar_h)
    b_close.setToolTip("Close")
    b_close.setStyleSheet(_BTN_STYLE)
    b_close.clicked.connect(_on_close)

    # Assemble
    lay.addWidget(b_min)
    lay.addWidget(b_max)
    lay.addWidget(b_close)

    return w, b_max


# ═══════════════════════════════════════════════════════════════════════════════
#  Register the extension with Krita
# ═══════════════════════════════════════════════════════════════════════════════

_EXT = CompactTitlebarExtension(Krita.instance())
Krita.instance().addExtension(_EXT)
