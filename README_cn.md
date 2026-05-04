Vibe-coded with deepseek-v4-pro.

# Frameless — Krita Plugin

> [English version](README.md)

**不止是无边框——标题栏结构完全可自定义。** 去掉 Windows 原生标题栏后，你可以自由组合任意组件来构建自己的紧凑标题栏：文档名、菜单、弹性空白、画笔大小滑块、窗口控制按钮，也可以自己写组件。标题栏空白区域可拖动窗口，双击切换最大化。

布局通过 `config.json` 驱动，组件模块化——在 `components/` 下放一个新的 `.py` 文件并注册即可添加自定义组件。

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
- 布局可通过 `config.json` 配置——自由排序、增删组件

### 配置

编辑 `frameless/config.json` 自定义标题栏布局：

```json
{
    "layout": [
        {"name": "CurrentFileName", "config": {"poll_ms": 500}},
        {"name": "OriginalMenuBar", "config": {}},
        {"name": "Spacer",           "config": {}},
        {"name": "WindowControl",   "config": {"button_width": 60}}
    ]
}
```

组件位于 `frameless/components/`——每个组件暴露 `create(window, bar_h, config)` 工厂函数。添加自定义组件只需在该目录下放置 `.py` 文件并在 `__init__.py` 注册即可。

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

在自定义 `_TitleBar` 控件上通过 `mousePressEvent`/`mouseMoveEvent` 处理拖拽。鼠标在非按钮区域（窗口控制按钮）按下后**移动超过 5px** 时才启动拖动。拖动本身通过 Qt 的 `QWindow.startSystemMove()` 实现，自动触发 Aero Snap。

### 标题栏架构

自定义标题栏作为原 QMenuBar 的 **TopLeftCorner 控件**，通过 Resize 事件过滤器强制撑满整个菜单栏宽度。在清空原菜单栏前，所有 QMenu 对象被提取并迁移到标题栏内的真正 `QMenuBar` 中——Alt+字母快捷键、hover 切换菜单、键盘导航全部保留。

组件通过共享的 `SignalBus` 与标题栏通信（palette 变更、窗口状态、teardown）。

### Krita 特定注意事项

Krita 的 Python API 中，`Window`、`View`、`Document` 等对象是**即用即弃的薄封装**——引用它们的 Python 对象随时可能被垃圾回收。菜单栏修改通过 `QTimer.singleShot(0)` 延迟执行以避免段错误。

### 文件结构

```
frameless/
├── frameless.desktop                     # Krita 插件描述文件
├── frameless/
│   ├── __init__.py                       # Python 包入口
│   ├── FramelessExtension.py             # 主逻辑（Win32/DWM + 标题栏 + 入口）
│   ├── config.json                       # 布局配置
│   ├── components/
│   │   ├── __init__.py                   # 组件注册表 + 配置加载器
│   │   ├── filename.py                   # CurrentFileName
│   │   ├── menubar.py                    # OriginalMenuBar
│   │   ├── spacer.py                     # Spacer
│   │   └── window_control.py             # WindowControl
│   ├── krita.pyi                         # Krita Python API 类型桩
│   └── Manual.html                       # Krita 插件帮助页
├── README.md                              # English version
└── README_cn.md                           # 本文件（中文）
```
