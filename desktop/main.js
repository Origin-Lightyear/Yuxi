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
const { app, BrowserWindow, dialog, ipcMain, Menu, Notification, screen, session, shell, Tray } = require('electron');
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

  mainWindow = new BrowserWindow({
    ...initialBounds,
    minWidth: 900,
    minHeight: 600,
    title: '大力水手',
    icon: resolveAppIcon(),
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      spellcheck: false,
    },
  });
  if (saved && saved.maximized) mainWindow.maximize();

  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (!isAllowedExternalUrl(target)) return { action: 'deny' };
    shell.openExternal(target);
    return { action: 'deny' };
  });

  mainWindow.webContents.on('will-navigate', (event, target) => {
    if (!target.startsWith(url)) {
      event.preventDefault();
      if (!isAllowedExternalUrl(target)) return;
      shell.openExternal(target);
    }
  });

  // 可编辑区右键弹出编辑菜单；独立构建 popup 菜单，不依赖全局应用菜单的内部结构
  mainWindow.webContents.on('context-menu', (_event, params) => {
    if (!params.isEditable) return;
    Menu.buildFromTemplate(editSubmenuTemplate()).popup({ window: mainWindow });
  });

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

function buildMenu() {
  const template = [];
  if (process.platform === 'darwin') {
    template.push({
      label: '大力水手',
      submenu: [
        { role: 'about', label: '关于' },
        { type: 'separator' },
        { role: 'hide', label: '隐藏' },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    });
  }
  template.push(
    { label: '编辑', submenu: editSubmenuTemplate() },
    { label: '查看', submenu: [{ role: 'reload', label: '重新加载' }] },
    // role quit 走 app.quit() -> before-quit 置 isQuitting，close 处理器不会拦截，真正退出
    { label: '应用', submenu: [{ role: 'quit', label: '退出' }] }
  );
  return Menu.buildFromTemplate(template);
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

    Menu.setApplicationMenu(buildMenu());

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

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow(backendUrl);
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
