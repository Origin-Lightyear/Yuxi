/**
 * Yuxi 桌面壳 (Electron) — 前后端分离，服务与网页版共用
 *
 * 桌面应用只做窗口外壳，连接网页版正在使用的同一套服务：
 *   - 本地 docker compose 起的 5173 端口，或
 *   - 部署在服务器上的实例地址
 *
 * 服务地址优先级：环境变量 YUXI_BACKEND_URL > 用户上次保存的地址 > 默认 http://10.69.188.181:5173
 * 连接失败时弹窗提供地址输入，用户可修改并重试（保存到 userData/config.json）。
 */
const { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, Notification, screen, session, shell, Tray } = require('electron');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { isAllowedExternalUrl } = require('./url_policy.js');
const { sanitizeWindowState } = require('./window_state.js');

// 关闭到托盘后窗口隐藏/被遮挡，禁用 Chromium 后台节流，保证隐藏窗口内 30s 轮询不失速
app.commandLine.appendSwitch('disable-renderer-backgrounding');
app.commandLine.appendSwitch('disable-backgrounding-occluded-windows');

const DEFAULT_URL = 'http://10.69.188.181:5173';
const CONFIG_FILE = 'config.json';
// 顶部给系统按钮（macOS 红绿灯 / Windows 窗口控件）留出的高度，窗口与页面留白共用同一常量
const TITLEBAR_HEIGHT = 36;
// 拖动条元素 id：注入脚本按它判重，全屏规则按它隐藏同一条带，改名单点即可
const DRAG_STRIP_ID = 'yuxi-titlebar-drag';

let mainWindow = null;
let backendUrl = null;
// 退出标志：before-quit 与托盘「退出」共享；close 据此区分「关闭到托盘」与真正退出
let isQuitting = false;
let tray = null;

// ---------------------------------------------------------------------------
// 服务地址配置（userData/config.json 持久化）
// ---------------------------------------------------------------------------

function configPath() {
  return path.join(app.getPath('userData'), CONFIG_FILE);
}

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(configPath(), 'utf-8'));
  } catch (_) {
    return {};
  }
}

function saveConfig(partial) {
  const cfg = { ...loadConfig(), ...partial };
  fs.mkdirSync(path.dirname(configPath()), { recursive: true });
  fs.writeFileSync(configPath(), JSON.stringify(cfg, null, 2));
}

function resolveBackendUrl() {
  return (process.env.YUXI_BACKEND_URL || loadConfig().backendUrl || DEFAULT_URL).replace(/\/+$/, '');
}

// 应用图标（Dock/任务栏）：build/icon.png 为方形品牌 Logo（打包时同源）
function resolveAppIcon() {
  const icon = path.join(__dirname, 'build', 'icon.png');
  return fs.existsSync(icon) ? icon : null;
}

// ---------------------------------------------------------------------------
// 健康检查
// ---------------------------------------------------------------------------

function waitForBackend(url, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const health = new URL(`${url}/api/system/health`);
    const deadline = Date.now() + timeoutMs;
    const tick = () => {
      const req = http.get(
        { host: health.hostname, port: health.port, path: health.pathname, timeout: 3000 },
        (res) => {
          res.resume();
          if (res.statusCode === 200) resolve();
          else retry();
        }
      );
      req.on('error', retry);
      req.on('timeout', () => {
        req.destroy();
        retry();
      });
    };
    const retry = () => {
      if (Date.now() > deadline) reject(new Error(`连接超时 (${url})`));
      else setTimeout(tick, 500);
    };
    tick();
  });
}

// ---------------------------------------------------------------------------
// 服务地址输入窗口（preload + IPC）
// ---------------------------------------------------------------------------

function askUrl(current) {
  return new Promise((resolve) => {
    const win = new BrowserWindow({
      width: 480,
      height: 200,
      resizable: false,
      minimizable: false,
      maximizable: false,
      title: '大力水手 服务地址',
      show: false,
      webPreferences: {
        nodeIntegration: false,
        contextIsolation: true,
        preload: path.join(__dirname, 'preload.js'),
      },
    });

    ipcMain.once('ask-url:submit', (_event, value) => {
      resolve(value ? String(value).replace(/\/+$/, '') : null);
      win.destroy();
    });
    win.on('closed', () => resolve(null));

    const html = `<!doctype html>
<meta charset="utf-8">
<style>
  body { font-family: -apple-system, sans-serif; padding: 20px; margin: 0; font-size: 14px; }
  h3 { margin: 0 0 8px; font-size: 15px; }
  input { width: 100%; padding: 8px; box-sizing: border-box; }
  .row { margin-top: 14px; text-align: right; }
  button { padding: 6px 14px; }
  button:first-of-type { margin-right: 8px; }
</style>
<body>
  <h3>大力水手 服务地址</h3>
  <p style="margin:0 0 8px;color:#666;font-size:12px">网页版用什么地址访问，这里就填什么地址</p>
  <input id="u" value="${current}" />
  <div class="row">
    <button id="cancel">取消</button>
    <button id="ok">连接</button>
  </div>
  <script>
    const u = document.getElementById('u');
    u.onkeydown = (e) => { if (e.key === 'Enter') submit(u.value); };
    document.getElementById('ok').onclick = () => submit(u.value);
    document.getElementById('cancel').onclick = () => submit(null);
    function submit(v) { window.yuxiDesktop.submit(v); }
  </script>
</body>`;
    win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    win.once('ready-to-show', () => win.show());
  });
}

// ---------------------------------------------------------------------------
// 连接确认（启动连接与托盘切换共用）
// ---------------------------------------------------------------------------

// 等待健康检查通过；失败弹窗三选（修改地址/重试/退出），直到成功或用户退出。
// 成功返回可用 url；用户选退出返回 null，由调用方负责 app.quit()。
// 每轮只等 5 秒，服务不可达时弹窗快速出现。
async function ensureBackend(currentUrl) {
  let url = currentUrl;
  for (;;) {
    try {
      await waitForBackend(url, 5000);
      return url;
    } catch (err) {
      const choice = await dialog.showMessageBox({
        type: 'error',
        title: '无法连接 大力水手 服务',
        message: '无法连接 大力水手 服务',
        detail:
          `${String(err?.message || err)}\n\n` +
          `当前服务地址：${url}\n` +
          '请确认服务地址可达（网页版用什么地址访问，这里就填什么地址）。',
        buttons: ['修改地址', '重试', '退出'],
        defaultId: 0,
        cancelId: 2,
      });
      if (choice.response === 2) return null;
      if (choice.response === 0) {
        const next = await askUrl(url);
        if (!next) continue; // 用户取消输入，回到选择弹窗
        url = next;
        saveConfig({ backendUrl: url });
      }
      // choice.response === 1：直接重试
    }
  }
}

// ---------------------------------------------------------------------------
// 窗口
// ---------------------------------------------------------------------------

let windowSaveTimer = null;

// 目标是否仍属当前服务地址同一 origin。用模块级 backendUrl 的运行时值比对：
// createWindow(url) 的闭包 url 在托盘切换服务地址后已过期，按创建时前缀比对
// 会把新地址的合法导航误判为外开。解析失败或非 http/https（origin 为 "null"）
// 一律返回 false，交给 isAllowedExternalUrl 白名单门控。
function isSameBackendOrigin(target) {
  try {
    return backendUrl !== null && new URL(target).origin === new URL(backendUrl).origin;
  } catch (_) {
    return false;
  }
}

function currentWindowState() {
  const { x, y, width, height } = mainWindow.getNormalBounds();
  return { x, y, width, height, maximized: mainWindow.isMaximized() };
}

// move/resize 高频触发，防抖合并写盘；close 时同步落盘并取消挂起的 timer
function scheduleWindowStateSave() {
  clearTimeout(windowSaveTimer);
  windowSaveTimer = setTimeout(() => {
    windowSaveTimer = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      saveConfig({ windowState: currentWindowState() });
    }
  }, 500);
}

function saveWindowStateNow() {
  clearTimeout(windowSaveTimer);
  windowSaveTimer = null;
  if (mainWindow && !mainWindow.isDestroyed()) {
    saveConfig({ windowState: currentWindowState() });
  }
}

function createWindow(url) {
  // 恢复上次布局；状态超界（显示器拔除等）时回默认，避免窗口不可见
  const saved = sanitizeWindowState(
    loadConfig().windowState,
    screen.getAllDisplays().map((d) => d.workArea)
  );
  const initialBounds = saved
    ? { x: saved.x, y: saved.y, width: saved.width, height: saved.height }
    : { width: 1280, height: 820 };

  // 无边框外观：macOS 隐藏标题栏但把红绿灯内嵌到页面左上角留白内；
  // 其他平台隐藏标题栏，用透明 titleBarOverlay 把系统窗口控件浮在页面右上角
  const titleBarOptions =
    process.platform === 'darwin'
      ? {
          titleBarStyle: 'hiddenInset',
          trafficLightPosition: { x: 14, y: (TITLEBAR_HEIGHT - 14) / 2 },
        }
      : {
          titleBarStyle: 'hidden',
          titleBarOverlay: {
            color: '#00000000',
            symbolColor: '#5f6a6a',
            height: TITLEBAR_HEIGHT,
          },
        };

  mainWindow = new BrowserWindow({
    ...initialBounds,
    minWidth: 900,
    minHeight: 600,
    title: '大力水手',
    icon: resolveAppIcon(),
    // 与页面侧栏底色一致，减少无边框窗口启动时的白闪
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#030303' : '#fcfefe',
    ...titleBarOptions,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      spellcheck: false,
    },
  });
  if (saved && saved.maximized) mainWindow.maximize();

  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    // 同源弹窗不外开（v1 单窗口，直接拒绝），跨源仍走白名单
    if (isSameBackendOrigin(target)) return { action: 'deny' };
    if (!isAllowedExternalUrl(target)) {
      console.warn('[yuxi] 拒绝外开非白名单 URL:', target);
      return { action: 'deny' };
    }
    shell.openExternal(target);
    return { action: 'deny' };
  });

  // 深链/切地址后的导航必须留在应用内；比对失败（跨源或非 http/https）才交给白名单决定是否外开
  mainWindow.webContents.on('will-navigate', (event, target) => {
    if (isSameBackendOrigin(target)) return;
    event.preventDefault();
    if (!isAllowedExternalUrl(target)) {
      console.warn('[yuxi] 拒绝外开非白名单 URL:', target);
      return;
    }
    shell.openExternal(target);
  });

  // 可编辑区右键弹出编辑菜单；独立构建 popup 菜单，不依赖全局应用菜单的内部结构
  mainWindow.webContents.on('context-menu', (_event, params) => {
    if (!params.isEditable) return;
    Menu.buildFromTemplate(editSubmenuTemplate()).popup({ window: mainWindow });
  });

  const { webContents } = mainWindow;

  // 每次导航（含托盘切换服务地址）在 DOMContentLoaded 重新注入留白、拖动条与全屏状态；
  // 注入幂等，且不阻塞窗口显示
  webContents.on('dom-ready', () => installPageChrome(webContents));

  // 全屏切换只切 body 类：样式恒在，快速进出全屏或全屏中导航都不会残留失效覆盖
  mainWindow.on('enter-full-screen', () => syncFullscreenClass(webContents));
  mainWindow.on('leave-full-screen', () => syncFullscreenClass(webContents));

  // 默认打开 /agent 聊天页；未登录由前端路由守卫自动跳转登录页，登录后跳回 /agent
  mainWindow.loadURL(`${url}/agent`);

  mainWindow.on('move', scheduleWindowStateSave);
  mainWindow.on('resize', scheduleWindowStateSave);
  // 关闭默认隐藏到托盘（先落盘窗口状态），只有退出路径才真正关闭；
  // 托盘缺席（图标缺失/创建失败）时无法靠托盘恢复，放行走真实关闭，不假装隐藏
  mainWindow.on('close', (e) => {
    if (isQuitting) return;
    if (!tray) return;
    e.preventDefault();
    saveWindowStateNow();
    mainWindow.hide();
    firstTrayHint();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

/**
 * 给远程页面补上无边框窗口所需的顶部留白、拖动条与全屏样式。
 *
 * 远程页面不知道窗口无边框，这里替它留出标题栏高度，并叠一个透明拖动区。
 * 基础规则与全屏规则同在一张样式表内，全屏规则由 `body.yuxi-fullscreen` 限定，
 * 因此每次 DOMContentLoaded 重新注入后，全屏状态由 syncFullscreenClass 一并恢复。
 */
function installPageChrome(webContents) {
  // 留白用 padding 而非 margin，避免 body 背景色与页面顶部之间露出窗口底色
  const css = `
body {
  height: 100vh;
  padding-top: ${TITLEBAR_HEIGHT}px;
  box-sizing: border-box;
  overflow-y: auto;
}
.app-layout { height: calc(100vh - ${TITLEBAR_HEIGHT}px) !important; }
.login-view, .oidc-callback-view, .cli-auth-view {
  min-height: calc(100vh - ${TITLEBAR_HEIGHT}px) !important;
}

/* 全屏下系统按钮消失，留白、拖动区与独立视图高度一并按整屏计算 */
body.yuxi-fullscreen { padding-top: 0 !important; }
body.yuxi-fullscreen .app-layout { height: 100vh !important; }
body.yuxi-fullscreen .login-view,
body.yuxi-fullscreen .oidc-callback-view,
body.yuxi-fullscreen .cli-auth-view {
  min-height: 100vh !important;
}
body.yuxi-fullscreen #${DRAG_STRIP_ID} { display: none !important; }
`;

  const script = `
(() => {
  const id = '${DRAG_STRIP_ID}';
  if (document.getElementById(id)) return;

  const bar = document.createElement('div');
  bar.id = id;
  bar.style.cssText = [
    'position:fixed', 'top:0', 'left:0', 'right:0', 'height:${TITLEBAR_HEIGHT}px',
    'z-index:2147483647', 'background:transparent', '-webkit-app-region:drag',
  ].join(';');
  document.body.appendChild(bar);
})();
`;

  webContents.insertCSS(css).catch(warnPageChromeFailure);
  webContents.executeJavaScript(script).catch(warnPageChromeFailure);
  syncFullscreenClass(webContents);
}

/**
 * 按窗口当前全屏状态切换页面上的 `yuxi-fullscreen` 类。
 *
 * 全屏样式常驻样式表，这里只改类名：快速进出全屏、全屏中切换服务地址都不会留下失效覆盖。
 */
function syncFullscreenClass(webContents) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const script = `document.body.classList.toggle('yuxi-fullscreen', ${mainWindow.isFullScreen()});`;
  webContents.executeJavaScript(script).catch(warnPageChromeFailure);
}

// 导航过程中注入可能失败（页面被替换、窗口关闭），失败要可见而不是变成未处理的 rejection
function warnPageChromeFailure(err) {
  console.warn('[yuxi] 页面样式注入失败:', err);
}

// ---------------------------------------------------------------------------
// 托盘
// ---------------------------------------------------------------------------

// 关闭到托盘的首次提示只发一次，之后不再打扰
function firstTrayHint() {
  if (loadConfig().trayHintShown) return;
  new Notification({ title: '大力水手', body: '已最小化到托盘，点击托盘图标可恢复' }).show();
  saveConfig({ trayHintShown: true });
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  if (!mainWindow.isVisible()) mainWindow.show();
  mainWindow.focus();
}

// Electron 对隐藏窗口 focus() 是 no-op，通知点击仅靠渲染层 window.focus() 无法恢复窗口；
// 渲染层经此最小 show 通道请求主进程 restore/show/focus（非通知类 IPC）
ipcMain.on('yuxi:show-main-window', () => showMainWindow());

// ---------------------------------------------------------------------------
// 应用菜单与可编辑区右键菜单
// ---------------------------------------------------------------------------

// 编辑菜单模板：主菜单与右键菜单共用同一份 role 清单，避免两处漂移
function editSubmenuTemplate() {
  return [
    { role: 'undo', label: '撤销' },
    { role: 'redo', label: '重做' },
    { type: 'separator' },
    { role: 'cut', label: '剪切' },
    { role: 'copy', label: '复制' },
    { role: 'paste', label: '粘贴' },
    { role: 'selectAll', label: '全选' },
  ];
}

function applyAppMenu() {
  // 窗口无边框后不需要完整菜单栏：macOS 保留应用菜单与编辑菜单，保证 Cmd+Q / Cmd+C/V
  // 等系统快捷键可用；其他平台清空默认菜单，编辑能力由页面与右键菜单承担
  if (process.platform !== 'darwin') {
    Menu.setApplicationMenu(null);
    return;
  }
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([{ role: 'appMenu' }, { label: '编辑', submenu: editSubmenuTemplate() }])
  );
}

// ---------------------------------------------------------------------------
// 应用生命周期
// ---------------------------------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('before-quit', () => {
    isQuitting = true;
    // close 处理器在退出时直接 return，退出路径在此落盘，避免丢最后 500ms 防抖期内的几何状态
    saveWindowStateNow();
  });

  app.on('second-instance', () => {
    if (mainWindow) {
      if (!mainWindow.isVisible()) mainWindow.show();
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.whenReady().then(async () => {
    backendUrl = resolveBackendUrl();

    // dev 模式（npm start）下 Electron 自带默认图标，运行时替换为品牌 Logo
    const appIcon = resolveAppIcon();
    if (process.platform === 'darwin' && appIcon) {
      app.dock.setIcon(appIcon);
    }

    // 托盘创建失败（无图标或 Tray 抛错）时 tray 保持 null，close 处理器据此放行真实关闭
    if (appIcon) {
      try {
        tray = new Tray(appIcon);
      } catch (err) {
        tray = null;
        console.warn('托盘创建失败，关闭窗口将真正退出:', err);
      }
    } else {
      console.warn('缺少托盘图标，跳过托盘创建');
    }

    if (tray) {
      tray.setContextMenu(
        Menu.buildFromTemplate([
          { label: '显示主窗口', click: showMainWindow },
          { type: 'separator' },
          {
            label: '切换服务地址',
            click: async () => {
              const url = await askUrl(backendUrl);
              if (!url) return; // 用户取消输入，保持当前地址
              const ok = await ensureBackend(url);
              if (ok === null) {
                isQuitting = true;
                app.quit();
                return;
              }
              saveConfig({ backendUrl: ok });
              backendUrl = ok;
              if (mainWindow && !mainWindow.isDestroyed()) {
                mainWindow.loadURL(`${ok}/agent`);
              }
            },
          },
          // 系统登录项为唯一事实源；开发直跑（isPackaged=false）禁用，避免误设系统登录项
          {
            label: '开机自启动',
            type: 'checkbox',
            checked: app.getLoginItemSettings().openAtLogin,
            enabled: app.isPackaged,
            click: (menuItem) => app.setLoginItemSettings({ openAtLogin: menuItem.checked, path: process.execPath }),
          },
          { type: 'separator' },
          {
            label: '退出',
            click: () => {
              isQuitting = true;
              app.quit();
            },
          },
        ])
      );
      tray.on('click', showMainWindow);
    }

    applyAppMenu();

    // 下载显式落系统下载目录：默认文件名 + 重名自动编号，不依赖 Electron 默认且不覆盖已有文件
    session.defaultSession.on('will-download', (_event, item) => {
      const dir = app.getPath('downloads');
      const suggested = item.getFilename();
      const ext = path.extname(suggested);
      const name = path.basename(suggested, ext);
      let target = path.join(dir, suggested);
      let i = 1;
      while (fs.existsSync(target)) {
        target = path.join(dir, `${name} (${i})${ext}`);
        i += 1;
      }
      item.setSavePath(target);
    });

    // 先开窗：用户双击后立刻看到界面（服务不可达时显示浏览器的连接错误页），
    // 而不是健康检查通过前长时间无窗口、像没启动一样
    createWindow(backendUrl);

    // 托盘隐藏场景下 Dock/任务栏点击也要恢复窗口，而不是只在窗口全关时才重建
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow(backendUrl);
      else showMainWindow();
    });

    // 连接循环：失败 -> 弹窗（修改地址/重试/退出），直到成功或用户退出（ensureBackend 内置）
    const connected = await ensureBackend(backendUrl);
    if (connected === null) {
      app.quit();
      return;
    }
    backendUrl = connected;

    // 连接成功后加载页面（用户可能在弹窗里改过地址）
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadURL(`${backendUrl}/agent`);
    }
  });
}
