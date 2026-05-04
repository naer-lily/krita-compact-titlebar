"""WindowControl — minimise / maximise / close buttons."""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPalette
from PyQt5.QtWidgets import (
    QToolButton, QWidget, QHBoxLayout, QStyle, QStyleOption,
)
from krita import Krita


DEFAULT_BTN_WIDTH   = 60
BTN_TEXT_MINIMISE   = "\u2500"   # ─
BTN_TEXT_MAXIMISE   = "\u25A1"   # □
BTN_TEXT_RESTORE    = "\u2750"   # ❐
BTN_TEXT_CLOSE      = "\u2715"   # ✕
DEFAULT_CLOSE_HOVER = "#E81123"


def _build_stylesheet(close_hover_bg: str) -> str:
    return f"""
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
        background: {close_hover_bg};
        color: white;
    }}
"""


class _WindowControlSection(QWidget):
    """Minimise / Maximise / Close buttons."""

    def __init__(self, qwin, obj_name: str, bar_h: int,
                 btn_w: int, stylesheet: str,
                 parent=None):
        super().__init__(parent)
        self._qwin = qwin
        self.setFixedHeight(bar_h)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._b_min = QToolButton(self)
        self._b_min.setText(BTN_TEXT_MINIMISE)
        self._b_min.setObjectName("titlebar-minimize")
        self._b_min.setFixedSize(btn_w, bar_h)
        self._b_min.setToolTip("Minimise")
        self._b_min.setStyleSheet(stylesheet)
        self._b_min.clicked.connect(qwin.showMinimized)

        self._b_max = QToolButton(self)
        self._b_max.setObjectName("titlebar-maximize")
        self._b_max.setFixedSize(btn_w, bar_h)
        self._b_max.setStyleSheet(stylesheet)

        def _toggle():
            if qwin.isMaximized():
                qwin.showNormal()
            else:
                qwin.showMaximized()
        self._b_max.clicked.connect(_toggle)

        def _on_close():
            for kw in Krita.instance().windows():
                if kw.qwindow().objectName() == obj_name:
                    kw.qwindow().close()
                    return
            qwin.close()

        self._b_close = QToolButton(self)
        self._b_close.setText(BTN_TEXT_CLOSE)
        self._b_close.setObjectName("titlebar-close")
        self._b_close.setFixedSize(btn_w, bar_h)
        self._b_close.setToolTip("Close")
        self._b_close.setStyleSheet(stylesheet)
        self._b_close.clicked.connect(_on_close)

        lay.addWidget(self._b_min)
        lay.addWidget(self._b_max)
        lay.addWidget(self._b_close)

        self.update_maximize_icon()

    def update_maximize_icon(self):
        if self._qwin.isMaximized():
            self._b_max.setText(BTN_TEXT_RESTORE)
            self._b_max.setToolTip("Restore")
        else:
            self._b_max.setText(BTN_TEXT_MAXIMISE)
            self._b_max.setToolTip("Maximize")

    def apply_palette(self, pal: QPalette):
        self.setPalette(pal)
        for btn in self.findChildren(QToolButton):
            btn.setPalette(pal)


def create(window, bar_h: int, config: dict):
    """Factory: WindowControl component.

    config keys (all optional):
        button_width: int      — width per button in px (default 60)
        close_hover_bg: str    — CSS color for close button hover (default "#E81123")
    """
    qwin = window.qwindow()
    obj_name = qwin.objectName()

    btn_w = config.get('button_width', DEFAULT_BTN_WIDTH)
    if not isinstance(btn_w, (int, float)) or btn_w <= 0:
        btn_w = DEFAULT_BTN_WIDTH
    btn_w = int(btn_w)

    close_hover = config.get('close_hover_bg', DEFAULT_CLOSE_HOVER)
    if not isinstance(close_hover, str):
        close_hover = DEFAULT_CLOSE_HOVER

    stylesheet = _build_stylesheet(close_hover)
    return _WindowControlSection(qwin, obj_name, bar_h, btn_w, stylesheet)
