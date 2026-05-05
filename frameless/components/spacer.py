"""Spacer — an expanding empty widget with proportional scaling."""
from PyQt5.QtWidgets import QWidget, QSizePolicy


class _SpacerSection(QWidget):
    """Horizontally-expanding empty space between sections.

    Uses ``QSizePolicy.setHorizontalStretch()`` to control proportional
    space allocation; ``QHBoxLayout.addWidget(widget)`` (no explicit
    stretch) automatically picks this up from the widget's size policy.
    """

    def __init__(self, bar_h: int, scale: int = 1, parent=None):
        super().__init__(parent)
        self.setFixedHeight(bar_h)
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sp.setHorizontalStretch(scale)
        self.setSizePolicy(sp)


def create(window, bar_h: int, config: dict, ctx):
    """Factory: Spacer component.

    config keys (all optional):
        scale: int -- stretch factor relative to other spacers (default 1).
               e.g. two spacers with scale 2 and 1 split remaining space 2:1.
    """
    scale = config.get('scale', 1)
    if not isinstance(scale, int) or scale < 1:
        scale = 1
    return _SpacerSection(bar_h, scale)
