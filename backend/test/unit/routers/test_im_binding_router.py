"""IM 绑定公开回调结果页的转义单元测试。"""

from __future__ import annotations

import pytest

from server.routers.im_binding_router import _result_page

pytestmark = [pytest.mark.unit]


def test_result_page_escapes_message():
    """回调错误文案可能来自平台响应，必须转义后再拼进 HTML。"""
    body = _result_page("<script>alert('x')</script>").body.decode()

    assert "<script>" not in body
    assert "&lt;script&gt;" in body
