from yuxi_im_gateway.filters import is_allowed, session_chat_id, should_handle
from yuxi_im_gateway.models import BotConfig, InboundMessage


def _bot(**overrides) -> BotConfig:
    base = dict(
        id=1, platform="feishu", name="bot", credentials={}, api_key="yxkey_x",
        agent_slug="default-chatbot", allowed_users=[], require_mention=True,
        group_sessions_per_user=True,
    )
    base.update(overrides)
    return BotConfig(**base)


def test_empty_whitelist_allows_everyone():
    assert is_allowed(_bot(allowed_users=[]), "ou_any") is True


def test_non_empty_whitelist_requires_hit():
    bot = _bot(allowed_users=["ou_a", "ou_b"])
    assert is_allowed(bot, "ou_a") is True
    assert is_allowed(bot, "ou_c") is False


def test_private_chat_always_handled_even_without_mention():
    msg = InboundMessage("m1", "c1", "ou_a", is_group=False, mentions_bot=False, text="hi")
    assert should_handle(_bot(require_mention=True), msg) is True


def test_group_requires_mention_when_configured():
    bot = _bot(require_mention=True)
    mentioned = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=True, text="@bot hi")
    ignored = InboundMessage("m2", "c1", "ou_a", is_group=True, mentions_bot=False, text="hi")
    assert should_handle(bot, mentioned) is True
    assert should_handle(bot, ignored) is False


def test_group_without_require_mention_accepts_all():
    bot = _bot(require_mention=False)
    msg = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=False, text="hi")
    assert should_handle(bot, msg) is True


def test_group_session_is_per_user_when_enabled():
    bot = _bot(group_sessions_per_user=True)
    msg = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=True, text="x")
    assert session_chat_id(bot, msg) == "c1:ou_a"


def test_group_session_shared_when_disabled():
    bot = _bot(group_sessions_per_user=False)
    msg = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=True, text="x")
    assert session_chat_id(bot, msg) == "c1"


def test_private_session_uses_chat_id():
    msg = InboundMessage("m1", "c9", "ou_a", is_group=False, mentions_bot=False, text="x")
    assert session_chat_id(_bot(), msg) == "c9"
