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
const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const fs = require('fs');
const http = require('http');
const path = require('path');

const DEFAULT_URL = 'http://10.69.188.181:5173';
const CONFIG_FILE = 'config.json';

let mainWindow = null;
let backendUrl = null;

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
// 窗口
// ---------------------------------------------------------------------------

function createWindow(url) {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
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

  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    shell.openExternal(target);
    return { action: 'deny' };
  });

  mainWindow.webContents.on('will-navigate', (event, target) => {
    if (!target.startsWith(url)) {
      event.preventDefault();
      shell.openExternal(target);
    }
  });

  // 默认打开 /agent 聊天页；未登录由前端路由守卫自动跳转登录页，登录后跳回 /agent
  mainWindow.loadURL(`${url}/agent`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ---------------------------------------------------------------------------
// 应用生命周期
// ---------------------------------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
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

    // 先开窗：用户双击后立刻看到界面（服务不可达时显示浏览器的连接错误页），
    // 而不是健康检查通过前长时间无窗口、像没启动一样
    createWindow(backendUrl);

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow(backendUrl);
    });

    // 连接循环：失败 -> 弹窗（修改地址/重试/退出），直到成功或用户退出
    // 每轮只等 5 秒，服务不可达时弹窗快速出现
    for (;;) {
      try {
        await waitForBackend(backendUrl, 5000);
        break;
      } catch (err) {
        const choice = await dialog.showMessageBox({
          type: 'error',
          title: '无法连接 大力水手 服务',
          message: '无法连接 大力水手 服务',
          detail:
            `${String(err?.message || err)}\n\n` +
            `当前服务地址：${backendUrl}\n` +
            '请确认服务地址可达（网页版用什么地址访问，这里就填什么地址）。',
          buttons: ['修改地址', '重试', '退出'],
          defaultId: 0,
          cancelId: 2,
        });
        if (choice.response === 2) {
          app.quit();
          return;
        }
        if (choice.response === 0) {
          const url = await askUrl(backendUrl);
          if (!url) continue; // 用户取消输入，回到选择弹窗
          backendUrl = url;
          saveConfig({ backendUrl });
        }
        // choice.response === 1：直接重试
      }
    }

    // 连接成功后加载页面（用户可能在弹窗里改过地址）
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadURL(`${backendUrl}/agent`);
    }
  });
}
