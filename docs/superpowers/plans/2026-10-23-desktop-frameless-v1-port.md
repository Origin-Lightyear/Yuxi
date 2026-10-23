# 桌面壳 v1 移植无边框窗口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `origin/main` 的桌面壳 v1（托盘 / 窗口状态 / 原生菜单 / 右键菜单）上，去掉菜单栏并把窗口改成无边框、内容铺满、顶部 36px 留给系统按钮。

**Architecture:** 只改 `desktop/main.js`（外加 README / changelog / 决策记录）。菜单由 `buildMenu()` 的全量菜单收敛为「macOS 应用菜单 + 编辑菜单，其他平台无菜单」；窗口加 `titleBarStyle` 平台分支；页面在 `dom-ready` 注入顶部留白 CSS 与透明拖动条，全屏时收起。

**Tech Stack:** Electron 37，`node --test`（`desktop/test`）

**Spec:** 本地 `main` 上已验证并合并的实现（提交 `12749d43`、`7a354835`、`3467f82b`、`3028deb0`），本次仅把行为移植到 v1 代码结构，验收标准不变；v1 既有能力（托盘、窗口状态、右键菜单、openExternal 白名单、下载落盘、通知）不得回归。

## Global Constraints

- 改动范围：`desktop/main.js`、`desktop/README.md`、`docs/develop-guides/changelog.md`、新增决策记录；不动 `desktop/url_policy.js`、`desktop/window_state.js`、`desktop/test/*`、`electron-builder.yml`、`package.json`
- 保留 v1 全部行为：托盘常驻与首次提示、窗口状态记忆、可编辑区右键菜单、openExternal 白名单、`will-download` 落盘、`yuxi:show-main-window` IPC、单实例锁
- 菜单：macOS 仅「应用 / 编辑」两组；其他平台 `Menu.setApplicationMenu(null)`；`editSubmenuTemplate()` 必须保留（右键菜单共用）
- 留白高度唯一常量 `TITLEBAR_HEIGHT = 36`；CSS 与拖动条均插值，不出现第二个 36
- 代码风格跟随 v1：2 空格、单引号、分号、中文注释、`// ---` 段落分隔
- 决策记录遵循 `docs/develop-guides/decisions/README.md` 格式，并交叉链接 `implemented/2026-10-10-desktop-shell-v1.md`（原菜单决策被本记录取代）

## Review Focus

- 去掉原生菜单后，macOS 的 Cmd+Q / Cmd+C/V、Windows 的编辑快捷键是否仍可用（右键菜单不覆盖快捷键场景）
- 托盘「关闭到隐藏」与无边框窗口右上角关闭按钮是否仍走同一 close 路径
- `dom-ready` 在每个导航触发，注入是否幂等，切换服务地址后是否仍生效
- 深色主题下顶部留白条是否跟随 `body` 背景，不出现白条
- 全屏 / 最大化下红绿灯与 overlay 按钮位置，以及全屏退出后样式是否恢复
- 拖动条是否挡住页面顶部 36px 内的可点击控件；窗口是否真的可拖动

---

### Task 1: 菜单收敛 + 无边框窗口 + 页面留白与拖动条

**Files:**
- Modify: `desktop/main.js`（顶部常量、`buildMenu` 段落、`createWindow`、`app.whenReady`）

**Interfaces:**
- Produces: `TITLEBAR_HEIGHT = 36`、`applyAppMenu()`、`installPageChrome(webContents)`、`FULLSCREEN_OVERRIDE_CSS`
- Removes: `buildMenu()`

- [ ] **Step 1: 菜单收敛**

删除 `buildMenu()`，在「应用菜单与可编辑区右键菜单」段落新增 `applyAppMenu()`：macOS `Menu.setApplicationMenu(Menu.buildFromTemplate([{ role: 'appMenu' }, { label: '编辑', submenu: editSubmenuTemplate() }]))`；其他平台 `Menu.setApplicationMenu(null)`。`app.whenReady` 中 `Menu.setApplicationMenu(buildMenu())` 改为 `applyAppMenu()`。`editSubmenuTemplate()` 保留不动。

- [ ] **Step 2: 无边框窗口**

`createWindow` 中按平台构造 `titleBarOptions`：macOS `titleBarStyle: 'hiddenInset'` + `trafficLightPosition: { x: 14, y: (TITLEBAR_HEIGHT - 14) / 2 }`；其他平台 `titleBarStyle: 'hidden'` + `titleBarOverlay: { color: '#00000000', symbolColor: '#5f6a6a', height: TITLEBAR_HEIGHT }`。BrowserWindow 增加 `backgroundColor: '#fcfefe'` 并展开 `titleBarOptions`。窗口状态恢复逻辑不改。

- [ ] **Step 3: 页面留白与拖动条**

在「窗口」段落新增 `installPageChrome(webContents)`：

```css
body { height: 100vh; padding-top: 36px; box-sizing: border-box; overflow-y: auto; }
.app-layout { height: calc(100vh - 36px) !important; }
.login-view, .oidc-callback-view, .cli-auth-view { min-height: calc(100vh - 36px) !important; }
```

（实际写入时 36 由 `TITLEBAR_HEIGHT` 插值）另注入幂等拖动条 `#yuxi-titlebar-drag`：`position:fixed; top:0; left:0; right:0; height:36px; z-index:2147483647; background:transparent; -webkit-app-region:drag`，脚本整体用 IIFE 包裹，靠 id 判重。

在 `createWindow` 中于 `loadURL` 前注册 `webContents.on('dom-ready', ...)` 调用它。

- [ ] **Step 4: 全屏收起**

新增 `FULLSCREEN_OVERRIDE_CSS`（`body{padding-top:0!important}`、`.app-layout{height:100vh!important}`、`#yuxi-titlebar-drag{display:none}`）。`createWindow` 里监听 `enter-full-screen` 插入并记录 key，`leave-full-screen` 在 key 非空时 `removeInsertedCSS(key)`。与托盘/窗口状态逻辑互不影响。

- [ ] **Step 5: 静态与单测验证**

Run: `cd desktop && node --check main.js && node --test`
Expected: 语法通过；`url_policy`、`window_state` 两个测试文件全部通过（本次未触及它们）。

- [ ] **Step 6: Commit**

```bash
git add desktop/main.js
git commit -m "feat(desktop): 去掉菜单栏并使用无边框窗口"
```

---

### Task 2: 决策记录与文档

**Files:**
- Create: `docs/develop-guides/decisions/implemented/2026-10-23-desktop-frameless-window.md`
- Modify: `desktop/README.md`、`docs/develop-guides/changelog.md`

- [ ] **Step 1: 写决策记录**

按仓库格式写 `状态：implemented`、`类型：feature`、`Owner：desktop/main.js`，包含「问题 / 决策（含 `### 实现方案`）/ 替代方案 / 后果 / 验证」。必须写明：原 v1 原生菜单决策被取代；顶部 36px 成为网页组件不可放可点击控件的约束；保留原生窗口边框的替代方案及放弃原因。

- [ ] **Step 2: 更新 README**

目录结构补充 `url_policy.js` / `window_state.js` / `test/`（v1 已有但 README 未列），并补两句：顶部 36px 为拖动区、Windows 透明 `titleBarOverlay` 需实机确认。

- [ ] **Step 3: 更新 changelog**

在 `docs/develop-guides/changelog.md` 当前版本列表补一条桌面客户端窗口优化，≤200 字。

- [ ] **Step 4: Commit**

```bash
git add desktop/README.md docs/develop-guides/changelog.md docs/develop-guides/decisions/implemented/2026-10-23-desktop-frameless-window.md docs/superpowers/plans/2026-10-23-desktop-frameless-v1-port.md
git commit -m "docs(desktop): 记录无边框窗口决策与使用约束"
```
