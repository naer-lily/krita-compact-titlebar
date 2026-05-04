Vibe-coded with deepseek-v4-pro.

# Frameless — Krita Plugin

> [English version](README.md)

Krita 插件，将 Windows 原生标题栏替换为紧凑的可配置自定义标题栏——Krita 原有菜单迁移到自定义栏内的一个真正 `QMenuBar` 控件中（Alt+字母快捷键、hover 切换菜单、键盘导航全部保留），右侧放置最小化/最大化/关闭按钮。标题栏空白区域可拖动窗口，双击切换最大化。

**仅支持 Windows 10 / 11。** 其他操作系统不受影响（插件在加载时会静默跳过非 Windows 消息）。

![](image.png)

## 安装

参考 <https://github.com/naer-lily/krita-shortcut-fix>。**安装后需要手动重启**。

---

## 效果

```
原生 Krita：
┌─────────────────────────────────────────────┐
│  未命名.kra  —  Krita           ─  □  ✕  │  ← 原生标题栏
├─────────────────────────────────────────────┤
│  File  Edit  View  ...                      │  ← 菜单栏
├─────────────────────────────────────────────┤
│  画布内容                                    │
└─────────────────────────────────────────────┘

Frameless：
┌─────────────────────────────────────────────┐
│  文档.kra  File  Edit  View  ... ─  □  ✕  │  ← 自定义标题栏
├─────────────────────────────────────────────┤
│  画布内容                                    │
└─────────────────────────────────────────────┘
```

- 左侧显示当前文档名
- 菜单（File、Edit 等）放在真正的 `QMenuBar` 中——Alt+字母快捷键、hover 切换菜单、键盘导航全部原生支持
- 标题栏空白区域可拖动窗口，贴边时触发 Aero Snap（半屏/四分之一屏）
- 双击标题栏空白区域切换最大化/还原
- 布局可配置（当前为硬编码，后续支持配置界面）

---

## 技术实现概述

### 为什么不能用 `Qt.FramelessWindowHint`

Krita 使用 PyQt5。最"显然"的去标题栏方式是在 QMainWindow 上设置 `Qt.FramelessWindowHint`。

但在 Windows 上，这个标志会把底层 HWND 的窗口样式改成 `WS_POPUP`——这意味着 Windows **完全不再发送 `WM_NCHITTEST` 消息**。没有 `WM_NCHITTEST`，就无法在窗口边缘提供缩放光标，也无法实现边缘拖拽缩放。

### 正确做法：手动操作 Win32 窗口样式

我们的方案与 VS Code / Chrome / Electron 等应用的底层实现等价：

| 步骤 | 做什么 | 为什么 |
|------|--------|--------|
| 1. Win32 样式修改 | 去掉 `WS_CAPTION`（标题栏），保留 `WS_THICKFRAME`（边框） | `WS_THICKFRAME` 存在时 Windows 仍会发送 `WM_NCHITTEST`，边缘缩放和 Aero Snap 才能工作 |
| 2. DWM 框架扩展 | `DwmExtendFrameIntoClientArea(0, 1, 0, 0)` | 告诉桌面窗口管理器"我们在自定义窗口 chrome"，DWM 自动渲染阴影 |
| 3. `WM_NCCALCSIZE` | 返回 0 → 客户区 = 整个窗口 | `WS_THICKFRAME` 的边框变为不可见（0px 宽），窗口看起来无边框 |
| 4. `WM_NCHITTEST` | 窗口边缘 6px 返回 `HTLEFT`/`HTRIGHT` 等 | Windows 显示正确的缩放光标，并处理边缘拖拽 |
| 5. `WM_GETMINMAXINFO` | 设置最大化边界为显示器工作区（不含任务栏） | 最大化时不会覆盖任务栏 |

### 拖动

在自定义 `_TitleBar` 控件上通过 `mousePressEvent`/`mouseMoveEvent` 处理拖拽。鼠标在非按钮区域（非菜单按钮、非窗口控制按钮）按下后**移动超过 5px** 时才启动拖动（不在按下时立即启动，是为了让双击最大化仍然能正常工作）。拖动本身通过 Qt 的 `QWindow.startSystemMove()` 实现，它内部调用 Windows 的 `DefWindowProc(WM_SYSCOMMAND, SC_MOVE | HTCAPTION)`，自动触发 Aero Snap。

### 标题栏架构

自定义标题栏通过 `QMainWindow.setMenuWidget()` 替代菜单栏区域。在替换前，所有 `QMenu` 对象从原菜单栏中提取并迁移到标题栏内的真正 `QMenuBar` 控件中——这保留了所有原生菜单栏行为（Alt+字母快捷键、hover 切换菜单、键盘导航）。

各区域由 `TITLE_LAYOUT`（当前硬编码）决定渲染顺序：

| 区域 | 控件 | 说明 |
|---------|--------|-------|
| `CurrentFileName` | `QLabel` | 轮询 `Krita.instance().activeDocument().fileName()` |
| `OriginalMenuBar` | `QMenuBar` | 真正的 QMenuBar，承载原 QMenu 对象 |
| `Spacer` | `QWidget` | `QSizePolicy.Expanding`——左对齐左、右对齐右 |
| `WindowControl` | `QWidget` | 最小化 / 最大化 / 关闭按钮 |

### Krita 特定注意事项

Krita 的 Python API 中，`Window`、`View`、`Document` 等对象是**即用即弃的薄封装**——引用它们的 Python 对象随时可能被垃圾回收。因此在信号回调和异步操作中不能捕获这些对象的引用，必须通过底层稳定的 Qt 对象 ID（如 `qwin.objectName()`）反查。

### 文件结构

```
frameless/
├── compact_titlebar.desktop              # Krita 插件描述文件
├── frameless/
│   ├── __init__.py                       # Python 包入口
│   ├── CompactTitlebarExtension.py       # 主逻辑（详见注释）
│   ├── krita.pyi                         # Krita Python API 类型桩
│   └── Manual.html                       # Krita 插件帮助页
├── README.md                              # English version
└── README_cn.md                           # 本文件（中文）
```
