"""CustomToolBar — hosts customToolBar2 in a zero-margin layout.

Qt limitation: `layout.addWidget()` unavoidably reparents the toolbar to this
widget.  The original parent is saved and restored on teardown.
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget, QToolBar, QVBoxLayout, QSizePolicy


class _ToolBarSection(QWidget):
    """Zero-margin widget that hosts a named QToolBar in its layout."""

    def __init__(self, window, bar_h: int, config: dict, parent=None):
        super().__init__(parent)
        qwin = window.qwindow()
        toolbar_name = config.get('toolbar_name', 'customToolBar2')
        self._toolbar: QToolBar | None = qwin.findChild(QToolBar, toolbar_name)
        self._original_parent = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if self._toolbar is not None:
            self._original_parent = self._toolbar.parent()
            # ⚠ layout.addWidget() will reparent the toolbar to self.
            # This is unavoidable in Qt.
            layout.addWidget(self._toolbar)
            self._toolbar.show()

        # Let the toolbar dictate the size — no fixed height
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def teardown(self):
        """Remove toolbar from layout and restore it to its original parent."""
        if self._toolbar is None:
            return
        try:
            self.layout().removeWidget(self._toolbar)
            if self._original_parent is not None:
                self._toolbar.setParent(self._original_parent)
        except: pass 
        self._toolbar = None


def create(window, bar_h: int, config: dict, ctx):
    """Factory: CustomToolBar.

    Finds the named QToolBar via findChild, then hosts it in a zero-margin
    QVBoxLayout.  The toolbar is reparented to the returned widget (Qt
    requirement); original parent is restored on teardown.

    config keys (all optional):
        toolbar_name: str -- objectName of the QToolBar to find
                      (default 'customToolBar2')
    """
    widget = _ToolBarSection(window, bar_h, config)
    ctx.teardown.connect(widget.teardown)
    return widget
