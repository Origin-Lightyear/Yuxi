# 桌面窗口无边框：去掉菜单栏与页面顶部拖动区

状态：implemented
类型：feature
Owner：desktop/main.js

## 问题

桌面壳 v1 保留系统标题栏与完整原生菜单栏，窗口观感与网页版割裂：内容无法铺满窗口，托盘常驻的应用长期显示一条与页面重复的菜单。本记录取代 [桌面壳 v1](2026-10-10-desktop-shell-v1.md) 中的原生菜单决策，其余能力（托盘、窗口状态、右键菜单、openExternal 白名单、下载落盘、通知）不变。

## 决策

窗口改为无边框，应用菜单收敛为 macOS 的「应用 / 编辑」两组，Windows 与 Linux 不设应用菜单。页面顶部 36px 由壳注入留白与透明拖动条，该高度是唯一常量 `TITLEBAR_HEIGHT`；这条带属于窗口拖动区，不是页面内容区，网页组件不得在其中放置可点击控件。

### 实现方案

- **窗口**：`createWindow` 按平台传 `titleBarStyle`。macOS 用 `hiddenInset`，红绿灯位置由 `TITLEBAR_HEIGHT` 计算后内嵌到顶部留白；其他平台用 `hidden` 加透明 `titleBarOverlay`，符号色 `#5f6a6a`、高度 `TITLEBAR_HEIGHT`。窗口底色 `#fcfefe` 与页面侧栏一致，减少无边框启动白闪。
- **菜单**：`applyAppMenu()` 取代 `buildMenu()`。macOS 构建 `appMenu` 与「编辑」菜单，保留 Cmd+Q、Cmd+C/V/A；其他平台 `Menu.setApplicationMenu(null)`。`editSubmenuTemplate()` 保留，可编辑区右键菜单仍由它构建。
- **页面**：`webContents` 的 `dom-ready` 每次触发 `installPageChrome()`；`insertCSS` 写入 `body` 顶部 padding 与布局高度，`executeJavaScript` 注入固定定位的透明条 `#yuxi-titlebar-drag`（`-webkit-app-region: drag`，最高 z-index）。脚本按 id 判重，重复导航不叠加元素。
- **全屏**：`enter-full-screen` 插入 `FULLSCREEN_OVERRIDE_CSS`（留白归零、隐藏拖动条、布局占满），`leave-full-screen` 按返回的 key 移除。

顶部 36px 是可点击控件的禁区：拖动条覆盖整条并吃掉鼠标事件。网页需要新的顶部操作时，把它放在留白之下。

## 替代方案

保留原生窗口边框与完整菜单，只调整菜单文案和页面样式。该方案改动最小，但标题栏始终占一条，无法得到内容铺满的观感，菜单栏也与网页重复。采用无边框并把系统按钮交给 `titleBarStyle` / `titleBarOverlay`，代价是留白由壳注入、菜单快捷键减少，接受。

## 后果

- Windows 与 Linux 没有应用菜单，菜单加速键随之消失，`Cmd/Ctrl+R` 重载不再可用，编辑快捷键依赖页面自身与右键菜单；macOS 保留「编辑」菜单，重载项同样移除。
- Windows 透明 `titleBarOverlay` 的系统按钮外观、位置与深色主题适配需要 Windows 实机确认。
- 顶部 36px 成为壳与网页之间的隐式约束：网页新增顶部控件必须避开该带，否则不可点击。
- 全屏覆盖样式的 key 由异步 `insertCSS` 返回。极快地「进入 → 退出」全屏时，退出可能早于 key 赋值，覆盖样式会保留到下一次全屏循环；正常全屏操作不受影响。

## 验证

`cd desktop && node --check main.js` 通过；`cd desktop && node --test` 7/7 通过（`url_policy.test.js`、`window_state.test.js`，本次未改动）。窗口拖动、红绿灯位置、全屏留白收起与恢复的 GUI 行为未在当前环境执行；Windows 安装版的透明 overlay 与深色主题为 Not run，需实机补验。
