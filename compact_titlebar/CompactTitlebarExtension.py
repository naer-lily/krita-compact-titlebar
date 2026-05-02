"""
Compact Titlebar — Krita Plugin  (Phase 1 — Prototype)
Windows only. Adds window control buttons to the right side of the menu bar,
and makes the menu bar draggable and double-clickable (maximize/restore).

This prototype works alongside the native titlebar — nothing is removed yet.
"""
from __future__ import annotations

from typing import Dict, Optional

from krita import *
from PyQt5.QtCore import Qt, QEvent, QObject, QPoint
from PyQt5.QtWidgets import QToolButton, QWidget, QHBoxLayout, QMainWindow


# ═══════════════════════════════════════════════════════════════════════════════
#  Event filter: menubar drag + double-click
# ═══════════════════════════════════════════════════════════════════════════════

class _MenubarEventFilter(QObject):
    """
    Installed on QMenuBar to intercept mouse events on *empty* menubar space
    (i.e. where no QAction sits).  Menu-item clicks pass through normally.
    """

    DRAG_THRESHOLD = 5          # px of movement before we call it a drag

    def __init__(self, qwin: QMainWindow):
        super().__init__()
        self._qwin = qwin
        self._drag_start: Optional[QPoint] = None
        self._dragging: bool = False

    # -- eventFilter ----------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent):
        etype = event.type()

        # ----- Mouse press on empty space -----
        if etype == QEvent.MouseButtonPress:
            if obj.actionAt(event.pos()) is None:
                # Qt ≥ 5.15  →  hand the drag to the OS window manager (clean & native)
                handle = self._qwin.windowHandle()
                if handle is not None and hasattr(handle, 'startSystemMove'):
                    handle.startSystemMove()
                    return True

                # Fallback for older Qt  →  track a manual drag
                self._drag_start = event.globalPos()
                self._dragging = False
                return True             # consume – don't forward to menubar

            # Click landed on a menu action → pass through
            return super().eventFilter(obj, event)

        # ----- Mouse move (manual drag fallback) -----
        if etype == QEvent.MouseMove:
            if self._drag_start is not None:
                d = event.globalPos() - self._drag_start
                if not self._dragging and d.manhattanLength() >= self.DRAG_THRESHOLD:
                    self._dragging = True
                if self._dragging:
                    self._qwin.move(self._qwin.pos() + d)
                    self._drag_start = event.globalPos()
                return True

        # ----- Mouse release → end drag -----
        if etype == QEvent.MouseButtonRelease:
            self._drag_start = None
            self._dragging = False
            # let menubar handle the release normally (e.g. closing a popup)

        # ----- Double-click on empty space → toggle maximize / restore -----
        if etype == QEvent.MouseButtonDblClick:
            if obj.actionAt(event.pos()) is None:
                if self._qwin.isMaximized():
                    self._qwin.showNormal()
                else:
                    self._qwin.showMaximized()
                return True

        return super().eventFilter(obj, event)


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

        self._managed[obj_name] = {
            'corner':        corner,
            'menubar_filter': menubar_filter,
            'state_filter':  state_filter,
            'menubar':       menubar,
            'qwin':          qwin,
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
