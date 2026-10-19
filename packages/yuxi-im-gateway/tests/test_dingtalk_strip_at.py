from types import SimpleNamespace

from yuxi_im_gateway.adapters.dingtalk import _strip_at


def _user(dingtalk_id=None, staff_id=None):
    return SimpleNamespace(dingtalk_id=dingtalk_id, staff_id=staff_id)


def test_strip_at_removes_leading_mention_by_dingtalk_id():
    assert _strip_at("@bot123 你好", [_user(dingtalk_id="bot123")], True) == "你好"


def test_strip_at_removes_leading_mention_by_staff_id():
    assert _strip_at("@staff9 你好", [_user(staff_id="staff9")], True) == "你好"


def test_strip_at_strips_unknown_leading_token_when_bot_mentioned():
    assert _strip_at("@whatever 你好", [_user(dingtalk_id="bot123")], True) == "你好"


def test_strip_at_keeps_text_unchanged_when_bot_not_mentioned():
    assert _strip_at("@alice 你好", [_user(dingtalk_id="alice")], False) == "@alice 你好"


def test_strip_at_keeps_text_without_leading_mention():
    assert _strip_at("你好 bot123", [_user(dingtalk_id="bot123")], True) == "你好 bot123"
