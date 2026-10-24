import pytest

from yuxi_im_gateway.models import BotConfig, InboundMessage


class _FakeAdapter:
    """记录生命周期与审批回调的适配器替身。"""

    RESTART_ON_STOP = False
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


@pytest.mark.asyncio
async def test_reconcile_isolates_bot_start_failure(monkeypatch):
    """一个机器人启动失败不应阻止其它机器人启动，也不应留下半初始化的适配器。"""
    import yuxi_im_gateway.main as mod

    def build(bot, on_inbound):
        if bot.id == 1:
            raise RuntimeError("bad bot")
        return _FakeAdapter(bot, on_inbound=on_inbound)

    monkeypatch.setattr(mod, "build_adapter", build)
    runner = mod.GatewayRunner(client=None, settings=None)

    await runner.reconcile([_bot(1, "a"), _bot(2, "b")])

    assert 1 not in runner._adapters
    assert 2 in runner._adapters
    assert [(adapter.bot.id, adapter.started) for adapter in _FakeAdapter.instances] == [(2, True)]


@pytest.mark.asyncio
async def test_reconcile_restarts_process_when_leaky_bot_replaced(monkeypatch):
    """长连接无法原地停止时，配置变更应请求重启进程而不是重建适配器。"""

    class _LeakyAdapter(_FakeAdapter):
        """模拟 SDK 无法停止后台线程的适配器。"""

        RESTART_ON_STOP = True

    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _LeakyAdapter(bot, on_inbound=on_inbound))
    runner = mod.GatewayRunner(client=None, settings=None)

    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([_bot(1, "b")])

    assert runner.restart_requested is True
    assert runner._stopping.is_set() is True
    assert _FakeAdapter.instances[0].stopped is True
    assert len(_FakeAdapter.instances) == 1


@pytest.mark.asyncio
async def test_reconcile_restarts_process_when_leaky_bot_removed(monkeypatch):
    """删除机器人同样需要重启进程，避免旧连接继续占用消息。"""

    class _LeakyAdapter(_FakeAdapter):
        """模拟 SDK 无法停止后台线程的适配器。"""

        RESTART_ON_STOP = True

    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _LeakyAdapter(bot, on_inbound=on_inbound))
    runner = mod.GatewayRunner(client=None, settings=None)

    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([])

    assert runner.restart_requested is True
    assert _FakeAdapter.instances[0].stopped is True


@pytest.mark.asyncio
async def test_reconcile_keeps_running_on_in_place_field_change(monkeypatch):
    """白名单等非连接字段变化不应触发重启。"""
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = mod.GatewayRunner(client=None, settings=None)

    await runner.reconcile([_bot(1, "a")])
    updated = BotConfig(1, "feishu", "b", {"app_id": "a", "app_secret": "s"}, "k2", "a")
    await runner.reconcile([updated])

    assert runner.restart_requested is False
    assert runner._stopping.is_set() is False
