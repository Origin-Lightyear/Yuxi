import pytest

from yuxi_im_gateway.models import BotConfig, InboundMessage


class _FakeAdapter:
    """记录生命周期与审批回调的适配器替身。"""

    instances: list["_FakeAdapter"] = []

    def __init__(self, bot, *, on_inbound):
        """保存机器人、入站回调并登记到实例列表。"""
        self.bot = bot
        self.on_inbound = on_inbound
        self.started = False
        self.stopped = False
        _FakeAdapter.instances.append(self)

    async def start(self):
        """标记适配器已启动。"""
        self.started = True

    async def stop(self):
        """标记适配器已停止。"""
        self.stopped = True


def _bot(bot_id: int, app_id: str) -> BotConfig:
    """构造一个用于测试的飞书机器人配置。"""
    return BotConfig(bot_id, "feishu", "b", {"app_id": app_id, "app_secret": "s"}, "k", "a")


def _msg() -> InboundMessage:
    """构造一条最小的入站消息。"""
    return InboundMessage(message_id="m1", chat_id="c1", sender_id="u1", is_group=False, mentions_bot=False, text="hi")


@pytest.fixture(autouse=True)
def _reset():
    """每个测试前后清空适配器替身实例。"""
    _FakeAdapter.instances.clear()
    yield
    _FakeAdapter.instances.clear()


@pytest.mark.asyncio
async def test_reconcile_starts_new_bots(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = mod.GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    assert _FakeAdapter.instances[0].started is True


@pytest.mark.asyncio
async def test_reconcile_rebuilds_on_credential_change(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = mod.GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([_bot(1, "b")])
    assert _FakeAdapter.instances[0].stopped is True
    assert _FakeAdapter.instances[1].started is True


@pytest.mark.asyncio
async def test_reconcile_keeps_unchanged_bot(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = mod.GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([_bot(1, "a")])
    assert len(_FakeAdapter.instances) == 1


@pytest.mark.asyncio
async def test_reconcile_stops_removed_bot(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = mod.GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([])
    assert _FakeAdapter.instances[0].stopped is True


@pytest.mark.asyncio
async def test_attach_approval_forwards_all_keywords(monkeypatch):
    import yuxi_im_gateway.main as mod

    calls: list[dict] = []

    async def fake_resolve_approval(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(mod, "resolve_approval", fake_resolve_approval)
    bot = _bot(1, "a")
    adapter = _FakeAdapter(bot, on_inbound=lambda msg: None)
    runner = mod.GatewayRunner(client=None, settings=None)

    runner._attach_approval(adapter, bot)
    await adapter.approval_handler(
        decision="approve",
        thread_id="t1",
        run_id="r1",
        operator_id="u1",
        requester_id="u2",
        chat_id="c1",
    )

    assert calls == [
        {
            "client": None,
            "adapter": adapter,
            "bot": bot,
            "thread_id": "t1",
            "run_id": "r1",
            "decision": "approve",
            "operator_id": "u1",
            "requester_id": "u2",
            "chat_id": "c1",
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_applies_live_field_change_to_inbound(monkeypatch):
    """配置热更新后，入站消息应使用最新的机器人配置。"""
    import yuxi_im_gateway.main as mod

    seen: list[dict] = []

    async def fake_handle_inbound(**kwargs):
        """记录入站处理参数供断言使用。"""
        seen.append(kwargs)

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    monkeypatch.setattr(mod, "handle_inbound", fake_handle_inbound)
    runner = mod.GatewayRunner(client=None, settings=None)

    await runner.reconcile([_bot(1, "a")])
    adapter = _FakeAdapter.instances[0]
    await adapter.on_inbound(_msg())

    updated = BotConfig(1, "feishu", "b", {"app_id": "a", "app_secret": "s"}, "k2", "a")
    await runner.reconcile([updated])
    await adapter.on_inbound(_msg())

    assert len(_FakeAdapter.instances) == 1
    assert seen[0]["bot"].api_key == "k"
    assert seen[1]["bot"].api_key == "k2"
