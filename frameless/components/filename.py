"""CurrentFileName — shows the active Krita document name."""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QLabel, QSizePolicy
from krita import Krita


DEFAULT_POLL_MS = 500


class _FileNameSection(QLabel):
    """Polls Krita for the active document name."""

    def __init__(self, bar_h: int, poll_ms: int = DEFAULT_POLL_MS,
                 parent=None):
        super().__init__(parent)
        self.setFixedHeight(bar_h)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(poll_ms)

    def _refresh(self):
        try:
            doc = Krita.instance().activeDocument()
            if doc is not None:
                fname = doc.fileName()
                self.setText(fname if fname else "")
            else:
                self.setText("")
        except Exception:
            self.setText("")

    def teardown(self):
        self._timer.stop()


def create(window, bar_h: int, config: dict):
    """Factory: CurrentFileName component.

    config keys (all optional):
        poll_ms: int — polling interval in ms (default 500)
    """
    poll_ms = config.get('poll_ms', DEFAULT_POLL_MS)
    if not isinstance(poll_ms, int) or poll_ms <= 0:
        poll_ms = DEFAULT_POLL_MS
    return _FileNameSection(bar_h, poll_ms)
