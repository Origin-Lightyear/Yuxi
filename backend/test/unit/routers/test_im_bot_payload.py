"""IM 机器人请求体 schema：require_binding 字段的创建 / 更新序列化契约。"""

from server.routers.im_bot_router import IMBotPayload


def test_im_bot_payload_exposes_require_binding_on_create():
    """创建路径用 exclude_none=True：显式传入 require_binding 时保留，缺省时交由服务层默认 False。"""
    explicit = IMBotPayload(platform="feishu", require_binding=True)
    assert explicit.model_dump(exclude_none=True)["require_binding"] is True

    omitted = IMBotPayload(platform="feishu")
    assert "require_binding" not in omitted.model_dump(exclude_none=True)


def test_im_bot_payload_exposes_require_binding_on_update():
    """更新路径用 exclude_unset=True：显式传 false 时保留，未提供时忽略不覆盖。"""
    updated = IMBotPayload(require_binding=False)
    assert updated.model_dump(exclude_unset=True)["require_binding"] is False

    untouched = IMBotPayload()
    assert "require_binding" not in untouched.model_dump(exclude_unset=True)
