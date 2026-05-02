# Compact Titlebar — Krita Plugin

> [中文版](README_cn.md)

A Krita plugin that replaces the native Windows titlebar with a compact header — window control buttons (minimise / maximise / close) are embedded on the right side of the menu bar, and empty menubar space can be dragged to move the window or double-clicked to toggle maximise.

**Windows 10 / 11 only.** Other operating systems are unaffected (the plugin silently skips non-Windows messages at load time).

## 安装

refer <https://github.com/naer-lily/krita-shortcut-fix>。

## Visual comparison

```
Native Krita:
┌─────────────────────────────────────────────┐
│  untitled.kra  —  Krita         ─  □  ✕  │  ← native titlebar
├─────────────────────────────────────────────┤
│  File  Edit  View  ...                      │  ← menu bar
├─────────────────────────────────────────────┤
│  canvas                                      │
└─────────────────────────────────────────────┘

Compact Titlebar:
┌─────────────────────────────────────────────┐
│  File  Edit  View  ...          ─  □  ✕  │  ← menu bar == titlebar
├─────────────────────────────────────────────┤
│  canvas                                      │
└─────────────────────────────────────────────┘
```

- Three window control buttons appear on the right of the menu bar
- Empty menubar space (where no menu action sits) can be dragged to move the window; Aero Snap (half-screen / quarter-screen) works when dragged to a screen edge
- Double-click empty menubar space to toggle maximise / restore

## Installation

1. Copy the `compact_titlebar/` folder into Krita's plugin directory:
   ```
   %APPDATA%\krita\pykrita\
   ```
2. Restart Krita
3. Enable **Compact Titlebar** in **Settings → Configure Krita → Python Plugin Manager**

## Technical overview

### Why not `Qt.FramelessWindowHint`

The obvious approach is to call `QMainWindow.setWindowFlags(Qt.FramelessWindowHint)`, but on Windows this changes the underlying HWND to `WS_POPUP` style — which means Windows **stops sending `WM_NCHITTEST` entirely**. Without `WM_NCHITTEST` there is no way to provide resize cursors at window edges or implement edge-drag resizing.

### The correct approach: manual Win32 style manipulation

Our implementation is equivalent to how VS Code / Chrome / Electron handle custom titlebars at the lowest level:

| Step | What | Why |
|------|------|-----|
| 1. Win32 style | Remove `WS_CAPTION` (titlebar), keep `WS_THICKFRAME` (borders) | `WS_THICKFRAME` present → Windows still sends `WM_NCHITTEST` → resize + Aero Snap work |
| 2. DWM frame extension | `DwmExtendFrameIntoClientArea(0, 1, 0, 0)` | Tells the Desktop Window Manager "custom chrome in use" → DWM renders drop shadows |
| 3. `WM_NCCALCSIZE` | Return 0 → client rect = window rect | `WS_THICKFRAME` borders become invisible (0 px wide); window appears borderless |
| 4. `WM_NCHITTEST` | Return `HTLEFT` / `HTRIGHT` etc. for the outer 6 px | Windows shows the correct resize cursor and handles edge-dragging natively |
| 5. `WM_GETMINMAXINFO` | Set max bounds to monitor work area (excluding taskbar) | Maximised window does not cover the taskbar |

### Drag handling

A Qt event filter is installed on the menu bar. Window drag starts on **MouseMove after a 5 px threshold** (not on MousePress — this preserves double-click detection). The drag itself uses Qt's `QWindow.startSystemMove()`, which internally calls `DefWindowProc(WM_SYSCOMMAND, SC_MOVE | HTCAPTION)` — Aero Snap is triggered automatically.

### Krita-specific caveats

Krita's Python API objects (`Window`, `View`, `Document`, etc.) are **ephemeral thin wrappers** — they can be garbage-collected at any time. Never capture their references in signal callbacks or asynchronous code. Instead, cache stable underlying Qt object identifiers (like `qwin.objectName()`) and look up fresh wrappers at call time via `Krita.instance().windows()`.

### File structure

```
compact_titlebar/
├── compact_titlebar.desktop              # Krita plugin descriptor
├── compact_titlebar/
│   ├── __init__.py                       # Python package entry
│   ├── CompactTitlebarExtension.py       # main logic (heavily commented)
│   ├── krita.pyi                         # Krita Python API type stubs
│   └── Manual.html                       # Krita plugin help page
├── README.md                              # this file (English)
└── README_cn.md                           # Chinese version
```
