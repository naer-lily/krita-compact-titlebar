"""OriginalMenuBar — a real QMenuBar hosting the original Krita QMenu objects."""
from typing import List

from PyQt5.QtWidgets import QMenuBar, QMenu, QSizePolicy


class _MenuBarSection(QMenuBar):
    """A real QMenuBar that hosts the original QMenu objects from Krita's
    native menubar.

    SetSizePolicy(Maximum, Fixed) ensures it only takes as much width as
    its menus need, leaving the rest for the Spacer.
    """

    def __init__(self, menus: List[QMenu], bar_h: int, parent=None):
        super().__init__(parent)
        self.setNativeMenuBar(False)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.setFixedHeight(bar_h)

        for menu in menus:
            self.addMenu(menu)


def create(window, bar_h: int, config: dict):
    """Factory: OriginalMenuBar component.

    Extracts all QMenu objects from the current QMainWindow.menuBar()
    (must be called BEFORE the original menubar is cleared).

    config keys (all optional):
        (currently none — future: e.g. hidden_menus list)
    """
    qwin = window.qwindow()
    original_menubar = qwin.menuBar()

    menus: List[QMenu] = []
    for action in original_menubar.actions():
        m = action.menu()
        if m is not None:
            menus.append(m)

    return _MenuBarSection(menus, bar_h)
