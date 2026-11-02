# 桌面客户端自动更新

状态：implemented
类型：feature
Owner：desktop/main.js

跨 Owner 分工：`desktop/main.js` 拥有更新检查、下载完成提示、托盘入口、启动连接与切换服务地址后的更新源重解析；`desktop/update_feed.js` 拥有更新源地址解析与平台判定；`desktop/scripts/publish-update.js` 拥有产物校验与发布；`docker/nginx/default.conf` 与 `docker-compose.prod.yml` 拥有生产环境的静态分发。

## 问题

桌面客户端是 Electron 外壳，页面与功能随服务端更新，但外壳自身的修复、Electron 升级和窗口行为变化只能靠用户重新下载安装包。发布流程手动执行 electron-builder，产物留在 `desktop/dist/`，没有更新元数据、更新源或发布步骤；[桌面壳 v1](2026-10-10-desktop-shell-v1.md) 把自动更新与签名列为 non-goal。已安装的 Windows 客户端需要自行发现并安装新版本，更新包复用内网已有的网页服务分发，不引入公网 CDN 或签名流水线。

## 决策

### 实现方案

- `electron-updater` 是运行时依赖，只在 `app.isPackaged && process.platform === 'win32' && process.arch === 'x64'` 时启用。macOS 构建未签名，Squirrel 安装无法完成；ARM 安装版没有对应的更新源，也不启用。
- 更新源是当前服务地址所在站点的根路径 `/desktop-updates/`，可用 `YUXI_UPDATE_URL` 覆盖；非 http/https 覆盖地址被忽略并回落。更新源不使用服务地址的 pathname，避免客户端请求落到未配置的子路径并回退到 SPA 首页。
- 启动 15 秒后首次检查，之后每 6 小时复查；`autoDownload=true`。下载完成弹窗提供「立即重启 / 稍后」，选「稍后」由 `autoInstallOnAppQuit` 在退出时安装。同一版本只提示一次；已下载后再点「检查更新」会重新给出安装入口。更新弹窗先恢复主窗口，避免托盘常驻时对话框藏在隐藏窗口后面。
- 检查定时器在 feed 可用后启动一次；启动连接弹窗改过地址或托盘切换服务地址后重新 `setFeedURL`，不会重复叠加定时器，也不会因初始地址非法永久关闭能力。
- 托盘菜单只在支持自动更新的构建中出现「检查更新」。手动检查弹无更新或错误结果（含手动触发的下载失败）；后台检查失败只写日志。检查失败由 `error` 事件统一收敛，并发检查被门闩挡住；手动标记保留到下载完成或失败，托盘点击先重新同步 feed，未就绪时提示而不是回退到打包内默认地址。
- `electron-builder` 的 `win.publish`（generic）生成 `latest.yml` 与 `app-update.yml`。`desktop/scripts/publish-update.js` 只接受 x64 安装包：校验 `latest.yml` 版本与 `package.json` 一致、`path` 在 `files` 列表中、所有文件名不带目录、`files` 每项都带 `sha512`，并拒绝覆盖目标目录中同名但内容不同的安装包。产物先复制到目标目录的临时目录并按 `files[].sha512` 校验，再原子改名；`latest.yml` 最后替换，失败时保留旧版本。缺少 `.blockmap` 时告警并允许整包下载。
- 开发版 Vite 直接提供 `web/public/`；生产 web 容器把同一目录只读挂载到 nginx，发布新包不需要重建网页镜像。生产 nginx 的 `/desktop-updates/` 缺失时返回 404，不能回落到 SPA 的 `index.html`；开发版 Vite 仍会回落到首页，此时客户端解析失败并只记日志。
- 更新包与网页同源、走内网 HTTP，构建未签名；更新链路不做发布者签名校验，完整性由 `latest.yml` 的 sha512 保证。

## 替代方案

- GitHub Releases 或公网 CDN：内网实例不可达，并给内网客户端引入外网依赖，拒绝。
- 自建独立更新服务：只分发静态文件却新增一个服务与部署面，现有网页服务已经可达，拒绝。
- 不用 electron-updater 自研下载与替换：NSIS 安装、退出时安装、差分下载都要自维护，拒绝。
- 静默下载并强制重启：打断用户工作，拒绝。
- 只在启动时检查：常驻托盘用户长期不退出，会错过版本，拒绝。
- 打包时写死更新地址：切换服务地址或换部署端口后失效，改为运行时从服务地址解析，拒绝写死。
- x64 与 arm64 共用一份 `latest.yml`：Windows updater 只读一份元数据，先后发布会把错误架构的安装包发给另一端，拒绝；自动更新只发布 x64。

## 后果

- 每次发布必须提升 `desktop/package.json` 版本并运行 `npm run publish:win`；版本不一致、产物不是 x64、缺少 `sha512` 或目标目录已有同名但内容不同的安装包时发布脚本拒绝执行。
- 发布脚本不清理历史产物；长期发版需人工删除 `web/public/desktop-updates/` 中旧版本的安装包和 `.blockmap`，否则占用网页服务所在磁盘。
- ARM 安装版不启用自动更新；需要自动更新时先为 arm64 建立独立更新源与发布流程。
- 首次带自动更新的版本仍需用户手工安装一次，之前安装的 0.1.0 不含这段代码。
- 更新整包约 80MB，`.blockmap` 保留差分下载能力；内网带宽可接受。
- 构建未签名且走明文 HTTP，更新链路可被内网中间人篡改；签名与 HTTPS 需要另行立项。
- 更新源跟随服务地址：用户切到不提供 `/desktop-updates/` 的实例时检查静默失败，不影响应用使用。
- 生产镜像不包含更新包，靠宿主机目录挂载提供；开发版由 Vite 的 `public/` 直接提供。

## 验证

2026-11-02 实际执行的命令与结果：

```bash
node --test "desktop/test/*.test.js"                  # 24/24 通过（更新源解析、发布校验与原子发布）
npx electron-builder --win nsis --x64 --publish never # 生成 dist/latest.yml、安装包与 .blockmap
npm run publish:win                                   # 校验 sha512 后发布三个文件
vite --port 5199 + curl                               # latest.yml 200；安装包 Range 请求 206
nginx:alpine 挂载仓库配置与 web/public                  # latest.yml 200；缺失文件 404；Range 206
docker compose --env-file <临时env> -f docker-compose.prod.yml config --quiet  # 退出码 0
cd docs && pnpm run build                             # build complete
```

`electron-builder` 产物核对：`dist/latest.yml` 的 `path` 为 `大力水手 Setup 0.1.0-x64.exe`，`dist/win-unpacked/resources/app-update.yml` 写入 generic provider，`app.asar` 包含 `update_feed.js` 与 `electron-updater`。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 构建产物能生成可被客户端读取的更新元数据 | 只生成安装包，客户端找不到 `latest.yml` | `desktop/electron-builder.yml` | Windows 打包后读取 `dist/latest.yml`、`app-update.yml`、`app.asar` 文件清单 | 未配置 `win.publish` 时不生成更新元数据 | Passed |
| 发布脚本只发布当前版本、x64、`latest.yml` 声明的产物 | 发布旧包、错误架构或未声明的文件 | `desktop/scripts/publish-update.js` | `node --test` | 版本不一致、安装包缺失、`path` 不在 `files`、文件名带目录、arm64、缺少 `sha512` 均抛错且不落盘 | Passed |
| 发布失败不破坏已上线的旧版本 | sha512 不匹配或复制中断时 `latest.yml` 已更新；同版本重发覆盖旧安装包 | `desktop/scripts/publish-update.js` | `node --test`：损坏安装包时目标目录旧 `latest.yml` 保持不变，临时目录被清理；同名不同内容时拒绝发布 | 篡改安装包后断言旧 `latest.yml` 仍为原内容；目标目录预置同名旧内容后断言拒绝 | Passed |
| 开发直跑、非 Windows 与 ARM 版不发起更新检查 | 开发模式弹更新错误、macOS 崩溃或 ARM 客户端下载 x64 包 | `desktop/update_feed.js` | `node --test` | `isPackaged=false`、`darwin`、`linux`、`win32/arm64` 均断言 false | Passed |
| 更新源跟随服务地址，环境变量覆盖，非法地址回落 | 切换服务后仍从旧地址取包，或接受 `file:` 等协议 | `desktop/update_feed.js` | `node --test` | `file:`/`ftp:`/非 URL 覆盖被忽略并回落；子路径不参与更新源 | Passed |
| 启动连接改地址与托盘切换后更新源同步，且定时器只启动一次 | 页面换了地址，检查仍请求旧站点 | `desktop/main.js` | 代码路径核对：`syncUpdateFeed` 在初始化、启动连接成功与切换地址三处调用，`updateChecksStarted` 去重 | 初始地址非法时先不启动，修复地址后补启动 | Not run |
| 开发版网页服务提供更新包 | 更新文件 404 | `web/public/` 静态目录 | 本地 Vite 启动后 `curl` | 缺失文件回落首页，客户端解析失败并只记日志 | Passed |
| 生产版网页服务提供更新包且缺失时 404 | SPA fallback 把 `index.html` 当 `latest.yml` 返回 | `docker/nginx/default.conf`、`docker-compose.prod.yml` | nginx 容器挂载仓库配置与 `web/public` 后 `curl` | 请求不存在的 `/desktop-updates/latest.yml` 返回 404 | Passed |
| Windows 安装版能发现、下载并完成升级 | 只提示不安装，或安装后仍是旧版本 | `desktop/main.js` + `electron-updater` | Windows 安装版 E2E | 不发新版时断言不提示 | Not run |
| 现有桌面测试与工程契约不回归 | 新依赖或打包白名单遗漏导致壳启动失败 | `desktop/package.json`、`test/` | `node --test`；干净树上运行 `scripts/verify_engineering_contracts.py` | 打包白名单缺 `update_feed.js` 时 asar 不含该模块 | Passed |

未验证范围：Windows 安装版的真实升级链路（检查、下载、重启、NSIS 静默安装、安装后版本回读）在本机（macOS）无法执行；启动弹窗改地址后的更新源同步没有可运行的 Electron 生命周期测试，只做了代码路径核对。当前工作树另有一份未跟踪提案 `docs/develop-guides/decisions/proposed/2026-11-01-im-account-binding.md`，其格式错误会让仓库内直接运行工程契约检查失败；与本变更无关，上述契约结论来自 HEAD 归档加上本记录的干净树。
