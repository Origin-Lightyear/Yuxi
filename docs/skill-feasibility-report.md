# Yuxi 对标 WorkBuddy 常用 Skill 集成可行性报告

> 日期：2026-08-17 · 范围：Yuxi v0.7.1 当前架构（服务端执行 + 桌面薄壳共用服务模式）

## 1. 背景与评估方法

WorkBuddy（腾讯桌面级 AI Agent 工作台）的核心公式是「AI 大脑 + **用户本机工具执行** + 技能市场」——它的技能可以直接读写用户电脑上的文件、执行本机命令、操作本机浏览器。Yuxi 是**服务端架构**（worker 容器 + Docker 沙盒 + 桌面薄壳），执行位置天然不同。

本报告按 **skill 的资源依赖类型**分类评估，判定标准：

- ✅ **可行**：Yuxi 原生支持或仅需轻度适配（提示词/配置层面）
- ⚠️ **部分受限**：功能可实现但体验/能力打折扣，需改造
- ❌ **受限**：当前架构下不可行，需架构级方案（本地桥/本地沙盒）

## 2. Yuxi 当前能力基线

| 能力 | 现状 |
|---|---|
| Skill 机制 | SKILL.md + scripts，来源 builtin / upload / remote(git)，脚本可在沙盒执行（如 mysql-reporter 的 query.py） |
| 沙盒执行 | 服务端 Docker 容器（sandbox-provisioner），运行时仅 Python 3.13-slim，三目录：workspace / uploads / outputs |
| MCP | stdio / SSE / streamable-http，由服务端 worker 发起连接 |
| 内置工具 | web_search（豆包/Tavily）、ocr_parse_file（PaddleX/MinerU）、present_artifacts、install_skill、动态工具生成 |
| 浏览器自动化 | **无**（无 playwright/selenium/puppeteer 依赖） |
| 记忆/人设系统 | **无独立实现**（仅有 personal skill 作用域概念） |
| 文档解析 | MinerU（PDF）+ PaddleX（OCR），`--profile all` 启动 |

## 3. 逐类评估

### 3.1 文档生成/办公类 — ✅ 可行

| Skill | 评估 | 说明 |
|---|---|---|
| docx / xlsx / pptx / office-document-suite | ✅ | 纯服务端执行：模板+API 生成文档，产物存入沙盒 outputs 或 MinIO，用户下载。WorkBuddy 的同类技能本身就是云端 API 驱动 |
| prd-generator / weekly-report / ppt-maker | ✅ | 提示词方法类，SKILL.md 即可移植 |
| Enhanced PDF Parsing / pdf 处理 | ✅ | Yuxi 原生 MinerU 解析能力更强（扫描件/图片型 PDF） |

### 3.2 代码类 — ⚠️ 部分受限

| Skill | 评估 | 说明 |
|---|---|---|
| code-doc / code-review-expert / unit-test-generator | ✅ | 输入代码内容即可，不依赖本机仓库；配合文件上传可用 |
| **本机项目执行**（npm/pip/git/终端命令、项目构建部署） | ⚠️ | 沙盒能跑 Python 脚本，但**目标资源在用户本机时不可达**（服务端容器看不到用户电脑的代码仓库/环境）。能跑的场景：代码作为附件上传后在服务端沙盒执行 |
| skill-creator | ✅ | 生成 SKILL.md 包，Yuxi 的 upload 安装通道可承接 |

### 3.3 浏览器/联网类 — ⚠️～❌ 受限

| Skill | 评估 | 说明 |
|---|---|---|
| web-search | ✅ | Yuxi 原生内置（豆包/Tavily） |
| agent-browser / playwright（打开页面、滚动、点击、截图、填表、抓取） | ❌ | **Yuxi 无浏览器自动化基础设施**。服务端即使加 Playwright，也是无头浏览器运行在服务器：无法复用用户本机登录态/Cookie、访问不了内网页面、IP 属地不同 |
| 批量抓取公开数据 | ⚠️ | 服务端加无头浏览器可做公开页面抓取，但体验远弱于 WorkBuddy 本机浏览器 |

### 3.4 知识库/记忆类 — ✅ 可行（部分需适配）

| Skill | 评估 | 说明 |
|---|---|---|
| 知识库检索（等价 ima-skills） | ✅ | Yuxi 原生 knowledge-base skill（Milvus+Neo4j），能力对等甚至更强（图谱+思维导图） |
| agent-memory / 跨会话记忆、人设 | ⚠️ | Yuxi 无独立记忆系统。可通过 SKILL.md 内嵌人设/规则实现单智能体级等价物；跨会话结构化记忆需开发（有 middleware 扩展点 `agents/middlewares/` 可挂） |
| obsidian-skills（读写本机 Obsidian 库） | ❌ | 依赖**用户本机文件系统**，服务端不可达 |
| weread-skills（微信读书云端 API） | ✅ | 云端 API 类，服务端可达，可封装为 skill/MCP |

### 3.5 本地资源类 — ❌ 受限（核心差异）

| Skill | 评估 | 说明 |
|---|---|---|
| file-organizer（整理本机文件） | ❌ | 用户本机文件系统不可达 |
| local-whisper（本机语音转写） | ❌ | 本机计算；服务端做语音转写需另接云端 STT API（非"本地"） |
| yt-dlp-downloader（下载到用户机器） | ❌ | 下载产物落在服务器，用户需二次下载 |

### 3.6 元技能 — ✅ 可行

| Skill | 评估 | 说明 |
|---|---|---|
| find-skills / skill-scanner（技能发现） | ✅ | Yuxi 已有 remote skill 搜索（`search_remote_skills`）+ 技能列表 API |
| skills-security-check（安装前安全扫描） | ⚠️ | 需开发：上传/remote 安装前扫描 skill 包内容（脚本静态检查），工作量中等 |
| self-improvement（自我改进） | ✅ | 提示词方法类，可移植 |

## 4. 受限根因分析

| # | 根因 | 影响范围 |
|---|---|---|
| R1 | **执行位置差异**：Yuxi 一切跑在服务端容器，WorkBuddy 跑在用户本机 | 本机文件/项目/进程类技能全部受限（3.2、3.5） |
| R2 | **浏览器能力缺失**：Yuxi 无浏览器自动化基础设施 | agent-browser/playwright 类不可行（3.3） |
| R3 | **本地资源不可达**：硬件（麦克风）、本地库（Obsidian）、本机登录态 | 3.3、3.4、3.5 |
| R4 | **MCP stdio 无法拉起用户本机进程** | 连本地数据库/本地应用的 MCP server 需跑在服务端 |

## 5. 建议路线

| 阶段 | 行动 | 解锁 |
|---|---|---|
| **短期（零改造）** | 按 ✅ 清单批量移植 WorkBuddy 技能市场的方法类/文档类技能（SKILL.md 直接兼容）；补齐 upload/remote 安装文档 | 覆盖 WorkBuddy 约 50% 常用技能（文档、报表、搜索、知识库、元技能） |
| **中期（桌面端桥，1-2 周）** | 桌面壳加 preload 桥：① 本地文件对话框→直传沙盒 uploads ② 本地 MCP 桥（把用户本机的 stdio MCP server 代理注册给 Yuxi）③ 系统通知 | 解锁 R4 + 文件交互体验；本机 MCP 集成成为对 WorkBuddy 的差异化能力 |
| **中期（服务端补件，1-2 周）** | ① Playwright 无头浏览器容器 + browser skill（公开页面抓取/截图）② skills-security-check 静态扫描 ③ 跨会话记忆 middleware | 部分解锁 R2；补上元技能缺口 |
| **长期（架构级）** | 可选：LITE 模式本地部署包（用户机器跑 worker+沙盒+SQLite），或本地 sandbox-provisioner 单机版 | 完全对齐 WorkBuddy 的本机执行模型（R1/R3），但牺牲多租户集中管理 |

## 6. 结论

- **结论 1**：约 **50% 的 WorkBuddy 常用技能可直接移植**（方法类 + 云端 API 类），Yuxi 的 SKILL.md 机制与之兼容，无需改代码。
- **结论 2**：受限集中在「**用户本机资源**」这一根因上（文件、进程、浏览器、硬件）——这是服务端架构 vs 本机架构的**本质差异**，不是补丁能完全消除的；桌面薄壳目前只是换窗口，**没有**改变这一点。
- **结论 3**：差异化的正确定位不是复刻 WorkBuddy，而是发挥 Yuxi 的**多租户集中管理 + 知识库/图谱 + 服务端沙盒**优势，用桌面端桥（本地 MCP 桥）弥补最关键的本机集成缺口。

## 参考资料

- [WorkBuddy 必装 Skill 清单 | 腾讯云开发者](https://cloud.tencent.com.cn/developer/article/2719758)
- [WorkBuddy 新手必装 skills 清单 | 腾讯云开发者](https://cloud.tencent.cn/developer/article/2645086)
- [WorkBuddy AI Agent 深度指南：MCP 协议 + Skills 扩展 | 火山引擎](https://developer.volcengine.com/articles/7630198321433051163)
- [awesome-workbuddy 生态资源清单 | GitHub](https://github.com/staruhub/awesome-workbuddy)
- [WorkBuddy 桌面智能体工作台 | 腾讯云开发者](https://cloud.tencent.cn/developer/article/2690305)
