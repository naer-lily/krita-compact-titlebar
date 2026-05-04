"""Spacer — an expanding empty widget."""
from PyQt5.QtWidgets import QWidget, QSizePolicy


class _SpacerSection(QWidget):
    """Horizontally-expanding empty space between sections."""

    def __init__(self, bar_h: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(bar_h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


def create(window, bar_h: int, config: dict, ctx):
    """Factory: Spacer component.

    config keys (all optional):
        (currently none)
    """
    return _SpacerSection(bar_h)
