"""Component registry and config loader for the frameless titlebar.

Each component is a module that exposes:
    create(window, bar_h, config, ctx) -> QWidget

where ctx is a _ComponentContext carrying shared signals
(palette_changed, window_state_changed, teardown).

Add new components by:
1. Creating a module in this directory with a `create` function
2. Registering it in COMPONENT_REGISTRY below
"""
import json
import os
from typing import Dict, Callable, List

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QPalette
from PyQt5.QtWidgets import QWidget
from krita import Window

from . import filename
from . import menubar
from . import spacer
from . import window_control

# ---------------------------------------------------------------------------
# Shared signal bus — components subscribe; _TitleBar emits
# ---------------------------------------------------------------------------
class _ComponentContext(QObject):
    palette_changed       = pyqtSignal(QPalette)
    window_state_changed  = pyqtSignal()
    teardown              = pyqtSignal()


# ---------------------------------------------------------------------------
# Component registry: name → factory function
# ---------------------------------------------------------------------------
COMPONENT_REGISTRY: Dict[
    str, Callable[[Window, int, dict, _ComponentContext], QWidget]
] = {
    'CurrentFileName':  filename.create,
    'OriginalMenuBar':  menubar.create,
    'Spacer':           spacer.create,
    'WindowControl':    window_control.create,
}


# ---------------------------------------------------------------------------
# Config file path
# ---------------------------------------------------------------------------
def _config_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config.json',
    )


# ---------------------------------------------------------------------------
# Validation (simple, no pydantic)
# ---------------------------------------------------------------------------
def _validate_layout(layout: list):
    if not isinstance(layout, list):
        raise ValueError(f"'layout' must be a list, got {type(layout).__name__}")

    for i, item in enumerate(layout):
        if not isinstance(item, dict):
            raise ValueError(
                f"layout[{i}] must be a dict ({{name: ..., config: {{...}}}}), "
                f"got {type(item).__name__}"
            )
        if 'name' not in item:
            raise ValueError(f"layout[{i}] is missing the 'name' key")
        name = item['name']
        if name not in COMPONENT_REGISTRY:
            raise ValueError(
                f"Unknown component '{name}' at layout[{i}]. "
                f"Available: {list(COMPONENT_REGISTRY.keys())}"
            )
        if 'config' in item and not isinstance(item['config'], dict):
            raise ValueError(
                f"layout[{i}].config must be a dict, got "
                f"{type(item['config']).__name__}"
            )
        item.setdefault('config', {})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_config() -> List[dict]:
    path = _config_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"config.json not found at {path}")

    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError("config.json root must be a dict")
    if 'layout' not in raw:
        raise ValueError("config.json must have a 'layout' key")

    _validate_layout(raw['layout'])
    return raw['layout']
