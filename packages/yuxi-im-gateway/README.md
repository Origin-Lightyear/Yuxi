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

### 启动检查

`docker logs gateway-dev` 中出现 `机器人已启动 id=... platform=...` 表示对应机器人已连接；
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
- [ ] 触发工具审批时收到交互卡片，批准与拒绝均生效；重复点击不会产生第二个恢复 run。
- [ ] `allowed_users` 留空时所有用户可用，非空时白名单外用户被忽略。
