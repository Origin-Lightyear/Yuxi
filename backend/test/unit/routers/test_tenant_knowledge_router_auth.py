"""Tenant 管理 API 鉴权依赖单元测试：401/404/422 矩阵。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.routers.tenant_knowledge_router import require_tenant_admin


@pytest.fixture
def app_config():
    from yuxi.config.app import config

    return config


def _enable(app_config, monkeypatch, *, enabled: bool, key: str | None):
    monkeypatch.setattr(type(app_config), "is_saas_enabled", property(lambda self: enabled))
    monkeypatch.setattr(app_config, "tenant_admin_api_key", key or "")


def test_disabled_saas_returns_404(app_config, monkeypatch):
    _enable(app_config, monkeypatch, enabled=False, key="secret")
    with pytest.raises(HTTPException) as exc_info:
        require_tenant_admin("secret", 100)
    assert exc_info.value.status_code == 404


def test_missing_key_config_returns_404(app_config, monkeypatch):
    _enable(app_config, monkeypatch, enabled=True, key=None)
    with pytest.raises(HTTPException) as exc_info:
        require_tenant_admin("secret", 100)
    assert exc_info.value.status_code == 404


def test_missing_or_mismatched_key_returns_401(app_config, monkeypatch):
    _enable(app_config, monkeypatch, enabled=True, key="secret")
    with pytest.raises(HTTPException) as exc_info:
        require_tenant_admin(None, 100)
    assert exc_info.value.status_code == 401
    with pytest.raises(HTTPException) as exc_info:
        require_tenant_admin("wrong", 100)
    assert exc_info.value.status_code == 401


def test_missing_tenant_id_returns_422(app_config, monkeypatch):
    _enable(app_config, monkeypatch, enabled=True, key="secret")
    with pytest.raises(HTTPException) as exc_info:
        require_tenant_admin("secret", None)
    assert exc_info.value.status_code == 422


def test_valid_credentials_pass(app_config, monkeypatch):
    _enable(app_config, monkeypatch, enabled=True, key="secret")
    ctx = require_tenant_admin("secret", 100)
    assert ctx.tenant_id == 100
