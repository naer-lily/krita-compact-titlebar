Vibe-coded with deepseek-v4-pro.

# Frameless — Krita Plugin

> [English version](README.md)

**不止是无边框——标题栏完全可自定义，还能把 Krita 原生工具栏直接塞进标题栏。** 去掉 Windows 原生标题栏后，你可以自由组合任意组件来构建自己的紧凑标题栏：文档名、菜单、弹性空白、窗口控制按钮，甚至 Krita 的 QToolBar——全部挤在同一行。标题栏空白区域可拖动窗口，双击切换最大化。

布局通过 `config.json` 驱动。首次运行时（或文件缺失/损坏时）会自动写盘一份模板 `config.json`，编辑后重启 Krita 即可。组件模块化——在 `components/` 下放一个新的 `.py` 文件并注册即可添加自定义组件。

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
┌──────────────────────────────────────────────────┐
│  文档.kra  ☰  [画笔大小 ████]  [工具按钮]  ─  □  ✕  │  ← 自定义标题栏
├──────────────────────────────────────────────────┤
│  画布内容                                         │
└──────────────────────────────────────────────────┘
```

- 左侧显示当前文档名
- 菜单可完整展示或缩为单个图标按钮（下拉展开），两种模式均保留 Alt+字母快捷键
- **Krita 原生工具栏可直接嵌入标题栏**——通过 `CustomToolBar` 组件，回收纵向屏幕空间
- 标题栏空白区域可拖动窗口，贴边时触发 Aero Snap（半屏/四分之一屏）
- 双击标题栏空白区域切换最大化/还原
- 标题栏高度随内容动态调整（如工具栏比菜单高时自动撑开）
- 布局可通过 `config.json` 配置——自由排序、增删组件

### 配置

编辑 `frameless/config.json` 自定义标题栏布局。
文件被删除或损坏后，下次启动会自动写回模板。

```json
{
    "layout": [
        {"name": "CurrentFileName", "config": {"poll_ms": 500}},
        {"name": "OriginalMenuBar", "config": {}},
        {"name": "Separator",       "config": {"width": 8}},
        {"name": "Spacer",          "config": {"scale": 1}},
        {"name": "CustomToolBar",   "config": {}},
        {"name": "Separator",       "config": {"width": 8}},
        {"name": "WindowControl",   "config": {"button_width": 60, "close_hover_bg": "#E81123"}}
    ]
}
```

#### 内置组件

| 组件 | 用途 | 配置项 |
|------|------|--------|
| `CurrentFileName` | 显示当前文档名 | `poll_ms`（int，默认 500） |
| `OriginalMenuBar` | 迁移的原生 QMenuBar | `compact`（bool，默认 false）——缩为图标按钮下拉；`menu_label`（str）——按钮文字覆盖 |
| `Separator` | 固定宽度视觉分割 | `width`（int，像素，默认 8） |
| `Spacer` | 弹性空白，填满剩余空间 | `scale`（int，≥1，默认 1）——相对其他 Spacer 的比例 |
| `CustomToolBar` | **将 Krita 原生 QToolBar 嵌入标题栏** | `toolbar_name`（str，默认 `"customToolBar2"`）——任意 Krita 工具栏的 objectName |
| `WindowControl` | 最小化/最大化/关闭按钮 | `button_width`（int，像素，默认 60），`close_hover_bg`（CSS 颜色，默认 `"#E81123"`） |

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
│   ├── config.json                       # 布局配置（首次运行时自动生成）
│   ├── components/
│   │   ├── __init__.py                   # 组件注册表 + 配置加载器
│   │   ├── filename.py                   # CurrentFileName
│   │   ├── menubar.py                    # OriginalMenuBar
│   │   ├── separator.py                  # Separator（固定宽度分割）
│   │   ├── spacer.py                     # Spacer（比例弹性空白）
│   │   ├── toolbar.py                    # CustomToolBar
│   │   └── window_control.py             # WindowControl
│   ├── krita.pyi                         # Krita Python API 类型桩
│   └── Manual.html                       # Krita 插件帮助页
├── README.md                              # English version
└── README_cn.md                           # 本文件（中文）
```
