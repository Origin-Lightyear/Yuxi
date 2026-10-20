# yuxi-im-gateway

Linko AI 的 IM 网关服务，负责将飞书、钉钉的机器人事件归一化后转发到 Yuxi HTTP API。

## 运行

网关通过 WebSocket 长连接接收机器人事件，并周期性从
`/api/internal/im-gateway/config` 拉取启用中的机器人配置。运行前需要在 `.env`
中配置 `IM_GATEWAY_TOKEN`（与 api 服务共享；留空时内部接口返回 404）。

compose 中的 `gateway` 服务受 profile 保护，默认不会启动。启用命令：

```bash
docker compose --profile gateway up -d gateway
```

生产环境使用 `docker compose -f docker-compose.prod.yml --profile gateway up -d gateway`。

开发镜像只安装运行时依赖，不包含 `watchfiles`；修改网关代码后需要执行
`docker compose restart gateway` 让新代码生效。

## 手工联调验收清单

以下项目依赖真实的飞书 / 钉钉应用凭证，需要在配置好机器人后逐项验证：

- [ ] 飞书单聊文本问答，Markdown 正文正确渲染。
- [ ] 钉钉单聊文本问答正常。
- [ ] 群聊仅在被 @ 时响应，且群内不同用户的会话相互隔离。
- [ ] Agent 交付 Excel 时，文件作为附件发送。
- [ ] 触发工具审批时收到交互卡片，批准与拒绝均生效；重复点击不会产生第二个恢复 run。
- [ ] `allowed_users` 留空时所有用户可用，非空时白名单外用户被忽略。
