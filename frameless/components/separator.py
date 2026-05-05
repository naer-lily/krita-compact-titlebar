"""Separator — a fixed-width visual divider between titlebar sections."""
from PyQt5.QtWidgets import QWidget, QSizePolicy


class _SeparatorSection(QWidget):
    """Fixed-width empty widget for explicit spacing between sections.
    Unlike Spacer (which expands to fill remaining space), Separator has a
    fixed pixel width, making spacing deterministic.
    """

    def __init__(self, bar_h: int, width: int = 8, parent=None):
        super().__init__(parent)
        self.setFixedSize(width, bar_h)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


def create(window, bar_h: int, config: dict, ctx):
    """Factory: Separator component.

    config keys (all optional):
        width: int -- fixed width in pixels (default 8)
    """
    width = config.get('width', 8)
    if not isinstance(width, int) or width < 0:
        width = 8
    return _SeparatorSection(bar_h, width)
