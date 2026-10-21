# yuxi-im-gateway

Linko AI 的 IM 网关服务，负责将飞书、钉钉的机器人事件归一化后转发到 Yuxi HTTP API。

## 运行

网关通过 WebSocket 长连接接收机器人事件，并周期性从
`/api/internal/im-gateway/config` 拉取启用中的机器人配置。

启用 profile 前必须在 `.env`（生产为 `.env.prod`）中设置 `IM_GATEWAY_TOKEN`，
取值与 api 服务保持一致；留空时内部接口返回 404，网关会立即以
`IM_GATEWAY_TOKEN 未配置` 退出，并因 `restart: unless-stopped` 被 Docker 反复重启。

compose 中的 `gateway` 服务受 profile 保护，默认不会启动。启用命令：

```bash
docker compose --profile gateway up -d gateway
```

生产环境使用 `docker compose -f docker-compose.prod.yml --profile gateway up -d gateway`。

### 配置热更新与自动重启

白名单、@ 策略、API Key 等字段的变更会在下一轮轮询（默认 30s）内生效，无需重启。
但**凭证或目标 Agent 变更**，以及**删除 / 停用机器人**，会触发网关进程自动重启：
飞书、钉钉的 SDK 都没有提供停止长连接的接口，旧守护线程只能在进程退出时释放；
若不重启，旧连接可能继续占用消息，钉钉侧还会把消息分流到旧连接导致丢失。
重启期间进行中的回复会中断，等待中的 run 不会自动补发。

### 工具审批

IM 会话不提供审批交互：网关提交 run 时固定 `tool_approval_mode=always_trust`，写文件、
改文件、执行代码等敏感工具直接执行，不会弹卡片等待批准。若某个 run 仍进入审批中断
（例如 Agent 侧另有配置），网关只回一条提示，不会让会话静默挂起。

### 启动检查

`docker logs gateway-dev` 中出现 `机器人正在建立长连接 id=... platform=...` 表示适配器已启动，
连接结果以紧随其后的 SDK 日志为准（钉钉为 `endpoint is {...}`，飞书为 lark 的连接日志）；
尚未配置机器人时只输出配置轮询日志也属正常。若反复出现 `IM_GATEWAY_TOKEN 未配置`，
说明 token 尚未配置，请补全后再启用 profile。

开发镜像只安装运行时依赖，不包含 `watchfiles`。修改网关源码后执行
`docker compose --profile gateway restart gateway`；修改依赖或 `pyproject.toml` 后需重新构建
`docker compose --profile gateway build gateway`。生产环境重启命令为
`docker compose -f docker-compose.prod.yml --profile gateway restart gateway`。

## 手工联调验收清单

以下项目依赖真实的飞书 / 钉钉应用凭证，需要在配置好机器人后逐项验证：

- [ ] 飞书单聊文本问答，Markdown 正文正确渲染。
- [ ] 钉钉单聊文本问答正常。
- [ ] 群聊仅在被 @ 时响应，且群内不同用户的会话相互隔离。
- [ ] Agent 交付 Excel 时，文件作为附件发送。
- [ ] Agent 执行代码 / 写文件时无需审批直接完成，回复不中断。
- [ ] `allowed_users` 留空时所有用户可用，非空时白名单外用户被忽略。
