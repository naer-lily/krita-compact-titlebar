"""OriginalMenuBar — hosts the original Krita QMenu objects.

Two modes:
  - Full    (compact=False, default): real QMenuBar with all menus inline
  - Compact (compact=True):           single button → dropdown with submenus
"""
from typing import List, Dict

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPalette, QKeySequence, QIcon
from PyQt5.QtWidgets import (
    QMenuBar, QMenu, QToolButton, QSizePolicy, QShortcut, QWidget,
    QVBoxLayout, QHBoxLayout,
)
from krita import Krita


# ---------------------------------------------------------------------------
# Full mode — real QMenuBar (existing behaviour)
# ---------------------------------------------------------------------------

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
            # WARNING: DON'T DO IT! IT WILL RUIN ITS FUNCTION
            # menu.setParent(self)
            self.addMenu(menu)

    def apply_palette(self, pal: QPalette):
        self.setPalette(pal)
        for menu in self.findChildren(QMenu):
            menu.setPalette(pal)


# ---------------------------------------------------------------------------
# Compact mode — single button, dropdown with submenus
# ---------------------------------------------------------------------------

class _CompactMenuSection(QWidget):
    """A single QToolButton that shows all original menus as submenus in a
    dropdown, with Alt+letter shortcuts preserved via QShortcut."""

    def __init__(self, menus: List[QMenu], bar_h: int, config: dict,
                 window, parent=None):
        super().__init__(parent)
        qwin = window.qwindow()
        self._menus = menus
        self._qwin = qwin
        self._shortcuts: List[QShortcut] = []

        label = config.get('menu_label', None)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._btn = QToolButton(self)
        if label is not None:
            self._btn.setText(label)
        else:
            self._btn.setIcon(Krita.instance().icon('properties'))
        self._btn.setPopupMode(QToolButton.InstantPopup)
        self._btn.setAutoRaise(True)
        self._btn.setFixedHeight(bar_h)
        self.setFixedHeight(bar_h)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        # Build the root dropdown menu (all original menus as submenus)
        self._root_menu = QMenu(self)
        for menu in menus:
            self._root_menu.addMenu(menu)
        self._btn.setMenu(self._root_menu)

        layout.addWidget(self._btn)

        # ---- Register Alt+letter shortcuts -------------------------------
        # Extract mnemonic from each menu's title (e.g. "&File" → 'F')
        used_chars: Dict[str, QMenu] = {}
        for menu in menus:
            title = menu.title()
            # Qt mnemonic is the character after '&'
            idx = title.find('&')
            if idx >= 0 and idx + 1 < len(title):
                ch = title[idx + 1].lower()
                if ch not in used_chars:
                    used_chars[ch] = menu

        for ch, menu in used_chars.items():
            sc = QShortcut(QKeySequence(f'Alt+{ch.upper()}'), qwin)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            # Capture the menu in a closure
            sc.activated.connect(
                lambda m=menu, b=self._btn: self._activate_submenu(m, b))
            self._shortcuts.append(sc)

    def _activate_submenu(self, target_menu: QMenu, anchor: QToolButton):
        """Popup the root menu and programmatically activate a submenu."""
        # Show the root menu below the button
        pos = anchor.mapToGlobal(
            anchor.rect().bottomLeft())
        self._root_menu.popup(pos)
        # Activate the target submenu inside the root menu
        for action in self._root_menu.actions():
            if action.menu() is target_menu:
                self._root_menu.setActiveAction(action)
                break

    def apply_palette(self, pal: QPalette):
        self.setPalette(pal)
        self._root_menu.setPalette(pal)
        self._btn.setPalette(pal)

    def teardown(self):
        for sc in self._shortcuts:
            sc.setEnabled(False)
        self._shortcuts.clear()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create(window, bar_h: int, config: dict, ctx):
    """Factory: OriginalMenuBar component.

    Extracts all QMenu objects from the current QMainWindow.menuBar()
    (must be called BEFORE the original menubar is cleared).

    config keys (all optional):
        compact:     bool -- single-button dropdown (default False)
        menu_label:  str  -- button text in compact mode; omit to use
                      Krita's 'properties' icon (default: icon)
    """
    qwin = window.qwindow()
    original_menubar = qwin.menuBar()

    menus: List[QMenu] = []
    for action in original_menubar.actions():
        m = action.menu()
        if m is not None:
            menus.append(m)

    compact = config.get('compact', False)

    if compact:
        widget = _CompactMenuSection(menus, bar_h, config, window)
    else:
        widget = _MenuBarSection(menus, bar_h)

    ctx.palette_changed.connect(widget.apply_palette)
    if compact:
        ctx.teardown.connect(widget.teardown)
    return widget
