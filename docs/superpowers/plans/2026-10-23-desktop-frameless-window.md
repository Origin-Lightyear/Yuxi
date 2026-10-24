# 桌面客户端去菜单栏 + 无边框窗体 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 去掉 Electron 桌面客户端默认菜单栏，窗口改为无标题栏、内容铺满，红绿灯 / 右上角窗口按钮浮在顶部留白区。

**Architecture:** 只改 `desktop/main.js` 一个文件。窗口用 `titleBarStyle: 'hiddenInset'`（macOS）/ `titleBarStyle: 'hidden'` + `titleBarOverlay`（Windows）隐藏原生标题栏；页面加载完成后向远程页面注入一段 CSS（顶部留 36px、`.app-layout` 高度改 100%）和一个透明拖动条，让窗口可拖动且内容不被系统按钮压住。

**Tech Stack:** Electron 37（无测试框架、无构建步骤，验证靠实机启动）

**Spec:** 本次会话确认的设计（小改动，未单独建 spec 文件）：

- Windows/Linux 无菜单；macOS 仅保留应用 / 编辑 / 窗口的极简菜单，保证 Cmd+Q/W/C/V 可用
- macOS 用原生红绿灯并垂直居中于 36px 留白区；Windows 用透明 `titleBarOverlay`
- 留白由注入 CSS 实现，背景取 `body` 背景，自动跟随明暗主题
- 顶部留白区叠一个透明拖动条（`-webkit-app-region: drag`）
- 登录页等非 `.app-layout` 页面整体下移 36px 后仍需居中

## Global Constraints

- 改动范围仅 `desktop/main.js`，另可在 `desktop/README.md` 补一条 Windows 待实机验证的说明
- 不引入新依赖，不改 `package.json` / `electron-builder.yml`
- 代码风格跟随现有 `main.js`：2 空格缩进、单引号、分号、中文注释、函数上方用 `// ---` 分隔段落
- 保留现有行为：单实例锁、健康检查弹窗、`will-navigate` 外链交给系统浏览器、`/agent` 默认页
- 留白高度统一取常量 `TITLEBAR_HEIGHT = 36`，`main.js` 与注入 CSS 共用同一数值

## Review Focus

- 页面加载完成前（`did-finish-load` 之前）留白尚未注入：首屏会不会出现内容被系统按钮压住或跳一下
- 登录页 `.login-view` 的 `min-height: 100vh` 与 body 高度覆盖叠加后是否仍视觉居中
- 深色主题（`html.dark`）下，顶部留白条颜色是否跟随，不能出现白条
- 页面内部滚动容器（`#app-router-view`）是否因整体缩高 36px 出现多余滚动条
- 窗口最大化 / 全屏时红绿灯与 overlay 按钮位置是否正常
- 无边框后窗口是否真的能拖动（拖动条是否被页面元素遮住）

---

### Task 1: 去掉菜单栏 + 无边框窗口外观

**Files:**
- Modify: `desktop/main.js:11`（引入 `Menu`）、`desktop/main.js:147-180`（`createWindow`）、`desktop/main.js:201-212`（`app.whenReady`）

**Interfaces:**
- Produces: 常量 `TITLEBAR_HEIGHT = 36`；函数 `applyAppMenu()`（无参数、无返回）；`createWindow(url)` 内部新增的无边框窗口配置

- [ ] **Step 1: 顶部引入 `Menu` 并定义留白高度常量**

```js
const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron');
```

在 `DEFAULT_URL` 常量旁增加：

```js
const TITLEBAR_HEIGHT = 36;
```

- [ ] **Step 2: 增加 `applyAppMenu()` 并实现菜单策略**

放在「窗口」段落之前的 `// ---` 分隔块内，函数带中文 docstring 注释：macOS 保留应用 / 编辑 / 窗口三组 role 菜单（否则 Cmd+Q、Cmd+W、Cmd+C/V 失效），其余平台直接清空菜单。

- [ ] **Step 3: `createWindow` 改为无边框外观**

- 新增 `backgroundColor: '#fcfefe'`（与侧栏底色一致，减少启动白闪）
- macOS：`titleBarStyle: 'hiddenInset'`，`trafficLightPosition: { x: 14, y: (TITLEBAR_HEIGHT - 14) / 2 }`
- Windows/Linux：`titleBarStyle: 'hidden'`，`titleBarOverlay: { color: '#00000000', symbolColor: '#5f6a6a', height: TITLEBAR_HEIGHT }`
- 平台分支集中在 `createWindow` 顶部的一个对象展开里，不新增 helper

- [ ] **Step 4: 在 `app.whenReady` 里调用 `applyAppMenu()`**

放在 `backendUrl = resolveBackendUrl()` 之后、`createWindow()` 之前。

- [ ] **Step 5: 实机验证（macOS）**

Run: `cd desktop && npm start`
Expected: 屏幕顶部系统菜单只剩「大力水手 / 编辑 / 窗口」，窗口内无标题栏，红绿灯浮在窗口左上角并与侧栏顶部留白垂直居中；Cmd+Q 能退出。

- [ ] **Step 6: Commit**

```bash
git add desktop/main.js
git commit -m "feat(desktop): 去掉默认菜单栏并使用无边框窗口"
```

---

### Task 2: 页面顶部留白与拖动条注入

**Files:**
- Modify: `desktop/main.js`（新增注入函数 + `createWindow` 内注册 `did-finish-load`）

**Interfaces:**
- Consumes: Task 1 的 `TITLEBAR_HEIGHT`
- Produces: `installPageChrome(webContents)`（无返回），由 `createWindow` 在加载页面前调用

- [ ] **Step 1: 实现 `installPageChrome(webContents)`**

`// ---` 分隔块，中文 docstring 说明「远程页面自己不知道窗口无边框，这里给它留出标题栏高度并补一个可拖动区」。在 `did-finish-load` 事件里做两件事：

1. `webContents.insertCSS(...)` 注入以下样式（`TITLEBAR_HEIGHT` 插值）：

```css
body {
  height: 100vh;
  padding-top: 36px;        /* TITLEBAR_HEIGHT */
  box-sizing: border-box;
  overflow: hidden;
}
.app-layout { height: 100% !important; }
.login-view { min-height: calc(100vh - 36px) !important; }
```

2. `webContents.executeJavaScript(...)` 在 `document.body` 末尾追加拖动条，若已存在则跳过：

```js
const id = 'yuxi-titlebar-drag';
if (!document.getElementById(id)) {
  const bar = document.createElement('div');
  bar.id = id;
  bar.style.cssText = [
    'position:fixed', 'top:0', 'left:0', 'right:0', 'height:36px',
    'z-index:2147483647', 'background:transparent',
    '-webkit-app-region:drag', 'pointer-events:auto',
  ].join(';');
  document.body.appendChild(bar);
}
```

- [ ] **Step 2: 在 `createWindow` 里注册**

`mainWindow.loadURL(...)` 之前调用 `installPageChrome(mainWindow.webContents)`。注意 `did-finish-load` 每次导航都会触发，两条注入都要幂等（CSS 重复插入无害，拖动条靠 id 判重）。

- [ ] **Step 3: 实机验证（macOS，浅色 + 深色）**

Run: `cd desktop && npm start`
Expected: 页面整体下移 36px；侧栏与内容区顶部留白无白条、随主题变色；窗口顶部空白处按住可拖动窗口；无多余滚动条；登录页（退出登录后）卡片仍视觉居中。

- [ ] **Step 4: README 补一条 Windows 验证说明**

在 `desktop/README.md` 合适位置补一句：无边框窗口在 Windows 上使用透明 `titleBarOverlay`，需在 Windows 实机确认按钮区域无黑条。仅此一行，不扩写。

- [ ] **Step 5: Commit**

```bash
git add desktop/main.js desktop/README.md
git commit -m "feat(desktop): 内容区顶部留白并支持拖动窗口"
```
