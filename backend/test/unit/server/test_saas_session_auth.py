from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.utils import auth_middleware
from yuxi.services import saas_identity, saas_session

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _UserSession:
    def __init__(self, user):
        self._user = user

    async def execute(self, _statement):
        return _ScalarResult(self._user)


async def test_saas_jwt_rejects_request_when_agent_session_is_missing(monkeypatch):
    user = SimpleNamespace(id=1, uid="E001", is_login_locked=lambda: False)
    context = saas_identity.SaasEmployeeContext(uid="E001", tenant_id=1, employee_id=2)

    monkeypatch.setattr(auth_middleware.AuthUtils, "verify_access_token", lambda _token: {"sub": "1"})
    monkeypatch.setattr(saas_identity, "get_saas_employee_context", lambda _db, _uid: _async_value(context))
    monkeypatch.setattr(saas_session, "get_saas_agent_session", lambda _uid: _async_value(None))

    with pytest.raises(HTTPException) as exc_info:
        await auth_middleware.get_current_user("Bearer token", _UserSession(user))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Agent登录状态已失效，请重新登录"


async def test_non_saas_jwt_does_not_require_agent_session(monkeypatch):
    user = SimpleNamespace(id=1, uid="local-user", is_login_locked=lambda: False)

    monkeypatch.setattr(auth_middleware.AuthUtils, "verify_access_token", lambda _token: {"sub": "1"})
    monkeypatch.setattr(saas_identity, "get_saas_employee_context", lambda _db, _uid: _async_value(None))

    async def fail_if_called(_uid):
        raise AssertionError("非 SaaS JWT 不应读取 AgentSession")

    monkeypatch.setattr(saas_session, "get_saas_agent_session", fail_if_called)

    result = await auth_middleware.get_current_user("Bearer token", _UserSession(user))

    assert result is user


async def _async_value(value):
    return value
