# Yuxi 桌面壳（Electron）

把 Yuxi 网页版包装成桌面应用，支持 macOS / Windows。

## 架构：前后端分离，服务与网页版共用

桌面应用只做窗口外壳，连接网页版正在使用的同一套服务：

```
Electron 窗口 ──HTTP──▶ Yuxi 服务 (默认 http://10.69.188.181:5173)
```

- 服务地址优先级：环境变量 `YUXI_BACKEND_URL` > 上次保存的地址 > 默认值
- 连接失败时弹窗可直接修改服务地址并重试（保存到 `userData/config.json`）
- 不做 Docker 管理、不打包后端——网页版怎么部署，桌面版就怎么连

## 用法

```bash
cd desktop
npm install          # 首次；网络慢时加 ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
npm start            # 启动（默认连 http://10.69.188.181:5173，无需额外配置）

# 连本地开发实例：
YUXI_BACKEND_URL=http://127.0.0.1:5173 npm start
```

## 打包

```bash
npm run dist:mac     # macOS (.dmg/.zip)
npm run dist:win     # Windows (NSIS 安装包)
```

**Windows 打包注意**：直接在 macOS 上跑即可，electron-builder 用纯 JS 提取卸载器，**不需要 wine**
（Docker + wine 在 Apple Silicon 上会因 qemu 模拟崩溃，不要用）。默认跟宿主架构产 arm64，
主流 Windows 需显式指定 x64：

```bash
npx electron-builder --win nsis --x64     # x64（主流 Windows）
npx electron-builder --win nsis --arm64   # ARM Windows
```

**网络慢时**（GitHub 下载超时）用国内镜像：

```bash
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ \
ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ \
npm run dist:mac
```

产物在 `desktop/dist/`：`.dmg`、`.zip`、`大力水手 Setup 0.1.0-x64.exe` / `-arm64.exe` 等
（文件名跟随 `electron-builder.yml` 的 `productName`；mac 的 .app 同样以产品名命名）。
当前未签名（无 Apple 开发者证书），分发时 macOS Gatekeeper 会提示"无法验证开发者"，
需右键→打开 或 系统设置→隐私与安全性 放行；正式分发需配置 Apple 证书签名。

## 目录结构

```
desktop/
├── main.js                # 主进程（健康检查 /api/system/health + 窗口 + 地址弹窗）
├── preload.js             # 地址输入窗口的 IPC 桥
├── package.json           # 依赖与脚本
└── electron-builder.yml   # macOS/Windows 打包配置
```
