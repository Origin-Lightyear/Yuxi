# 桌面壳 v1：运行通知与客户端体验

状态：implemented
类型：feature
Owner：desktop/main.js

跨 Owner 分工：`desktop/main.js` 拥有窗口、托盘、菜单、自启、地址切换与两个协议边界（openExternal、will-download）；`web/src/composables/useAgentRunStream.js` 拥有当前会话流的终态挂点；`web/src/utils/desktopNotify.js` 拥有通知门控、文案与点击行为；`web/src/utils/scheduledRunPoller.js` 拥有定时任务轮询与快照差异。

## 问题

桌面薄壳（Electron）目前只是把网页版装进窗口：运行终态只以聊天气泡呈现，用户切走窗口后对完成/失败无感知；定时任务在服务端后台触发，用户通常不在对应会话，更没有任何通知出口（后端没有用户级事件通道，SSE 只有 per-request/per-run 两个端点）。

客户端基础体验存在缺口：关闭即退出（后台任务期间无法常驻收通知）、窗口状态不记忆、无原生菜单与右键编辑、开机自启无入口、正常使用中无法切换服务地址。安全与系统集成上有三个小洞：`shell.openExternal` 对任意 URL 不校验协议、下载无 `will-download` 显式处理、后端断连时页面静默变死（健康检查只在启动时跑）。

平台口径：Windows 是目标运行平台（NSIS 安装版，electron-builder appId `com.yuxi.desktop`）；macOS 只作为开发与测试平台。拖拽文件到附件区在 Electron 中预期已可用（antd 拖拽框走 File 字节上传），需实测确认而非默认。

## 决策

桌面壳 v1 一次交付四组能力：**运行通知**（当前会话流 + 定时任务轮询）、**常驻与通知配套**（关闭到托盘、开机自启、窗口状态、原生菜单与快捷键、拖拽实测）、**客户端便利**（通知直达会话、切换服务地址、断连检测）、**安全与系统集成**（openExternal 白名单、will-download 处理）。浏览器端打开 web 的行为保持零变化。

### 实现方案

web 侧（渲染层，`yuxiDesktop` 存在性门控）：

- `web/src/utils/desktopNotify.js`：`notifyRunTerminal({ title, threadId, status })`。顺序判定：非桌面壳直接返回；`document.hasFocus()` 为 true 直接返回；否则构造 `new Notification`（中文文案：已完成 / 执行失败 / 已取消），`onclick` 经 preload 的 show 通道恢复窗口、回焦并导航到 `/agent/:thread_id`（通知直达会话）。
- `web/src/composables/useAgentRunStream.js`：既有终态判定处（`RUN_TERMINAL_STATUSES`）调用上述通知函数。
- `web/src/utils/scheduledRunPoller.js`：仅桌面壳且窗口失焦时，每 30s 轮询 `GET /api/scheduled-tasks`，内存维护 `run_id → status` 快照，发现新进终态的 run 调 `notifyRunTerminal`（标题为任务名）。聚焦时停止轮询；浏览器环境不启动。
- 断连检测（仅桌面壳，同门控）：渲染层每 20s 探测 `/api/system/health`，失败显示全局横幅（断开状态与重试）；断连 <30s 恢复仅收起横幅，≥30s 恢复后自动刷新页面以重建数据一致性（代价见风险）。

desktop 侧（`main.js` 及纯函数模块；`preload.js` 仅新增恢复主窗口的 show 通道，通知仍由渲染层发出）：

- **窗口状态**：`config.json` 增加 `windowState`，创建窗口时经纯函数校验 bounds 落在当前显示器内（超界回默认 1280×820），`move/resize` 防抖 500ms 与 `close` 时保存。
- **关闭到托盘**：`close` → `preventDefault + hide`（托盘缺席——图标缺失或创建失败——时放行真实关闭，不假装隐藏）；`before-quit` 置 `isQuitting` 才真退出；托盘菜单 = 显示主窗口 / 切换服务地址 / 开机自启（checkbox）/ 退出。首次最小化到托盘弹一次告知性 toast（config 记一次性标志）。
- **后台保活**：启动时追加 `disable-renderer-backgrounding`、`disable-backgrounding-occluded-windows`，保证隐藏窗口内定时任务轮询仍按 30s 执行——这是「托盘常驻 + 定时任务通知」成立的前提。
- **开机自启**：托盘 checkbox 驱动 `app.setLoginItemSettings`，系统登录项为事实源；`app.isPackaged === false`（开发直跑）时禁用。
- **原生菜单**：`Menu.setApplicationMenu`（中文文案）含编辑 roles（撤销/重做/剪切/复制/粘贴/全选及快捷键）、查看（重载）、应用（退出），mac 补最小 app 菜单；`webContents.on('context-menu')` 在可编辑区弹编辑菜单。
- **切换服务地址**：托盘菜单复用既有 `askUrl` 与连接弹窗逻辑，成功后保存配置并重新加载页面。
- **openExternal 白名单**：抽纯函数模块（`http/https/mailto` 放行，其余拒绝并记日志），两个既有调用点（`will-navigate`、`setWindowOpenHandler`）统一走它；拖到窗口非附件区的文件不再外开。
- **will-download**：显式 `setSavePath` 到系统下载目录，重名自动编号，不弹保存对话框。
- **拖拽**：以 E2E 实测为准；附件区内可用即为通过，区外行为改为白名单口径（不外开）。
- 纯函数（URL 白名单、窗口 bounds 校验）用 Node 内置 `node --test` 增加负向测试，为桌面侧首个最小测试面。

Non-goal（另行立项，本提案零代码）：自动更新与 CI 打包签名、全局热键、`yuxi://` 深链接、托盘红点/通知中心、通知勿扰时段、全站 i18n、多窗口、Agent 发起式本机选文件工具（依赖服务端到桌面的反向通道）。

## 替代方案

- 新增用户级 SSE 终态通道替代定时任务轮询：覆盖面等价（都只在应用打开时生效），只省 30s 延迟，但要新建后端事件面（Owner、恢复、鉴权一整套），拒绝进本提案。
- 主进程 IPC 发通知：能力与渲染层 `Notification` 等价，只增加 preload/IPC 改动面，拒绝。
- 仅靠渲染层 `window.focus()` 恢复隐藏窗口：Electron 对隐藏窗口的 `focus()` 是 no-op，隐藏态点击通知无法恢复窗口，拒绝；改为 preload 暴露最小 IPC show 通道（只恢复窗口，不经 IPC 发通知）。
- 浏览器端也开通知：改变 web 既有行为，超出桌面范围，拒绝。
- 终态始终通知（含聚焦时）：用户盯着聊天气泡时属打扰，拒绝；「聚焦不打扰」为既定取舍。
- 开机自启放 web 设置页：需要 preload 新增 IPC 与 web UI 入口，托盘 checkbox 同样可达且改动面更小，拒绝。
- 窗口状态引入 `electron-window-state` 依赖：功能可用但仓库无此依赖，校验逻辑约 50 行可自持，拒绝。
- 轮询常开（不限失焦）：聚焦时无通知需求，白耗请求，拒绝。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 桌面失焦时当前会话终态弹系统通知，点击直达对应会话 | 只回焦不跳转，或气泡内提示无系统通知 | `useAgentRunStream.js`、`desktopNotify.js` | Windows 安装版手工 E2E：发起会话、切走窗口、等待终态、点击通知 | 正向用例 | Not run |
| 浏览器环境零变化，`Notification` 从不被调用 | 门控缺失打扰 web 用户 | `desktopNotify.js` 桌面门控 | web unit：非桌面环境触发终态（已执行，见验证段） | 无 `yuxiDesktop` 时断言 `Notification` 构造未被调用 | Passed |
| 隐藏态点击系统通知恢复主窗口 | 隐藏窗口 `focus()` 是 no-op，点击无可见反应 | `main.js` IPC show 通道、`preload.js`、`desktopNotify.js` onclick | web unit：点击调用 showMainWindow 与缺方法负向；Windows E2E：关闭到托盘后点通知断言窗口恢复前台 | 旧 preload（无 `showMainWindow`）点击不抛错 | Not run |
| 切换服务地址后点通知/长断连刷新留在应用内 | 创建时 url 闭包过期，新地址导航被误判外开到系统浏览器 | `main.js` will-navigate/setWindowOpenHandler origin 比对 | Windows E2E：托盘切地址后点通知；杀后端 ≥30s 恢复刷新 | 切地址后断言未拉起系统浏览器打开应用地址 | Not run |
| 聚焦时不弹通知 | 聚焦仍弹造成打扰 | `desktopNotify.js` 聚焦判定 | web unit：mock `document.hasFocus()` 为 true（已执行，见验证段） | 断言 `Notification` 构造未被调用 | Passed |
| completed / failed / cancelled 三终态均给出对应中文文案 | 只覆盖成功态 | `desktopNotify.js` 状态映射 | web unit 表驱动三种状态（已执行，见验证段） | 正向用例 | Passed |
| 定时任务失焦时完成会弹通知（含任务名） | 只覆盖当前会话流，定时跑完无感知 | `scheduledRunPoller.js` | Windows E2E：run-now 触发定时任务、切走窗口、等待完成 | 正向用例 | Not run |
| 轮询仅在桌面壳且失焦时运行 | 浏览器常轮询或聚焦仍轮询 | `scheduledRunPoller.js` 启停逻辑 | web unit：浏览器环境不启动；`focus` 事件停止轮询（已执行，见验证段） | mock 聚焦后断言轮询器清除定时器 | Passed |
| 托盘隐藏态下定时任务通知仍按期弹出 | renderer 后台被 Chromium 节流导致轮询失速 | `main.js` backgrounding switches | Windows E2E：关闭到托盘后 run-now 定时任务 | 正向用例 | Not run |
| 窗口大小/位置重启后恢复，跨分辨率超界回默认 | 恢复到已拔显示器的坐标导致窗口不可见 | `main.js` + bounds 校验纯函数 | `node --test` 负向（超界 bounds 拒绝）+ E2E（移动后重启） | bounds 落在显示器外时断言返回默认值 | Not run |
| 关闭进托盘常驻，托盘退出真正结束进程 | 关闭即退或退出后进程残留 | `main.js` close/quit 语义 | Windows E2E：关闭→托盘仍在→托盘退出→任务管理器无进程 | 正向用例 | Not run |
| 开机自启 checkbox 与系统登录项一致，开发直跑禁用 | 状态不同步或 dev 模式误改登录项 | `main.js` setLoginItemSettings | Windows E2E：切换后查系统登录项；`npm start` 下 checkbox 禁用 | dev 直跑时断言 `setLoginItemSettings` 未被调用 | Not run |
| 菜单快捷键（复制/粘贴/全选）与可编辑区右键菜单可用 | 壳内无快捷键、右键无菜单 | `main.js` Menu/context-menu | Windows/macOS 手工 E2E | 正向用例 | Not run |
| 拖文件进附件区完成上传，区外拖拽不再外开 | 区内不可用或区外触发系统打开 | 附件区（web）、`url_policy.js` 白名单 | Windows E2E：区内拖拽上传；`node --test`（`javascript:`/`file:`/`data:` 拒绝，`http/https/mailto` 放行） | 非白名单协议构造链接断言拒绝外开 | Not run |
| 下载显式落入系统下载目录且重名不覆盖 | 依赖 Electron 默认行为、重名覆盖旧文件 | `main.js` will-download | Windows E2E：触发导出下载，连续两次同名文件 | 正向用例 | Not run |
| 后端断连出现横幅，短断连恢复不刷新、长断连恢复自动刷新 | 页面静默变死，或短暂抖动触发刷新丢输入 | 断连检测逻辑（web utils + 挂载组件） | E2E：杀后端观察横幅；<30s 与 ≥30s 两种恢复路径 | 短断连恢复断言未执行刷新 | Not run |
| 托盘可切换服务地址，不可达时复用连接弹窗 | 切换后白屏无退路 | `main.js`（复用 `askUrl`、`waitForBackend`） | Windows E2E：切到可达地址与不可达地址各一次 | 不可达地址断言出现修改/重试/退出弹窗 | Not run |
| web lint + unit、desktop `node --test`、contracts、docs build 全部通过 | 回归破坏现有事件流或文档导航 | 各命令 Owner | `web/AGENTS.md` 命令、`node --test desktop/test`、`python3 scripts/verify_engineering_contracts.py`、docs build | 正向命令 | Passed |

## 后果

桌面壳关闭默认常驻托盘（托盘缺席时回退真实关闭）；隐藏窗口内定时器不再节流，后台耗电略增；浏览器环境打开 web 的行为不变，由桌面门控 fail-closed 保证；断连与通知能力仅在 Electron 壳内生效。已知限制与未验证边界记录于验证段。

## 风险

- backgrounding switches 会让所有隐藏窗口内的定时器不再节流（含 web 自身的轮询组件），后台耗电略增，接受。
- 定时任务列表每次仅回每任务最近 3 条 run：30s 轮询间隔内同一任务完成超过 3 条可能挤出漏通知，概率极低，记录为已知限制。
- 长断连（≥30s）恢复自动刷新会丢未提交的输入，短断连不受影响；接受为数据一致性的代价。
- 断连阈值从首个失败探测起算，探测粒度 20s，真实断连时长存在 ±20s 模糊带；阈值附近的断连落在刷新或不刷新哪一侧随探测相位抖动。
- macOS 只是开发/测试平台：通知权限受未签名影响、托盘/菜单行为差异仅影响开发期验证；验收以 Windows NSIS 安装版为准，开发直跑的通知降级形态需实测记录。
- 通知点击深链依赖 run 事件载荷含 thread 标识，实现时若当前订阅流载荷缺该字段，需补取（以最小改动补齐，不扩大事件面）。

## 验证

实测环境：macOS 开发机，worktree 分支 `feat/desktop-shell-v1`，Node v24.19.0，无 docker（web/desktop 命令以 host 直跑等价执行）。

2026-10-10 实际执行并全部通过：

```bash
node --test "desktop/test/*.test.js"        # 7/7
cd web && corepack pnpm run lint:check      # exit 0
cd web && corepack pnpm run test:unit       # 426/426
cd web && corepack pnpm run build           # 成功（既有 chunk>500kB 警告）
python3 scripts/verify_engineering_contracts.py   # 49 decisions OK
python3 -m unittest scripts.test_verify_engineering_contracts  # OK
```

Windows NSIS 安装版 E2E 清单（验收矩阵中标 Not run 的实机行）未执行：本机为 macOS，无法产出 Windows 安装版证据。开发机 GUI 手工检查（关闭到托盘、托盘退出、菜单快捷键、右键菜单、自启勾选、下载落盘编号、拖拽上传、杀后端横幅、切地址两路径、开发直跑通知形态）同属 deferred，需在可运行桌面壳的环境补验。

已知限制（代码审查确认、非阻塞）：自启 checkbox 的 `checked` 启动时读取一次，系统登录项被外部改动后显示陈旧直至重启；poller 拉取失败产生 unhandled rejection 噪音（每 30s 一条 console error，无崩溃、下一 tick 自愈）；托盘切地址与启动连接循环存在并发窗口——启动循环 `waitForBackend` 旧地址成功时可能覆盖运行时地址，runtime/config 短暂分叉、重启回正；下载重名编号存在并发 TOCTOU（brief 指定伪代码的固有属性）。
