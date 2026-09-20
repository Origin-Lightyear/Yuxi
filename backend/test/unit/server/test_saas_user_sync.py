from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.routers import auth_router
from server.routers.auth_router import _find_or_create_saas_user, _select_saas_mcp_server, _upsert_saas_mcp_server
from yuxi.services.saas_client import EmployeeAuthResult, TenantEmployee
from yuxi.storage.postgres.models_business import Base, Department, MCPServer, User

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db

    await engine.dispose()


async def test_saas_user_sync_reuses_employee_code_when_mobile_changes(db_session):
    old_department = Department(name="旧部门")
    user = User(
        username="旧姓名",
        uid="E001",
        phone_number="13800000000",
        password_hash="placeholder",
        role="admin",
        department=old_department,
    )
    db_session.add_all([old_department, user])
    await db_session.commit()
    await db_session.refresh(user)

    synced_user = await _find_or_create_saas_user(
        db_session,
        mobile="13900000000",
        employee_code="E001",
        employee_name="新姓名",
        department_name="新部门",
    )

    assert synced_user.id == user.id
    assert synced_user.phone_number == "13900000000"
    assert synced_user.username == "新姓名"
    assert synced_user.role == "admin"
    assert synced_user.department.name == "新部门"


async def test_saas_user_sync_rejects_conflicting_uid_and_mobile(db_session):
    db_session.add_all(
        [
            User(
                username="员工甲",
                uid="E001",
                phone_number="13800000000",
                password_hash="placeholder",
                role="user",
            ),
            User(
                username="员工乙",
                uid="E002",
                phone_number="13900000000",
                password_hash="placeholder",
                role="user",
            ),
        ]
    )
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await _find_or_create_saas_user(
            db_session,
            mobile="13900000000",
            employee_code="E001",
            employee_name="员工甲",
            department_name="研发部",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "员工编码与手机号已绑定不同账户，请联系管理员"


async def test_saas_mcp_sync_uses_streamable_http_endpoint(db_session):
    server = MCPServer(
        slug="saas-mcp",
        name="SaaS MCP",
        transport="sse",
        url="https://mcp.example.com",
        enabled=1,
        created_by="saas",
        updated_by="saas",
    )
    db_session.add(server)
    await db_session.commit()

    await _upsert_saas_mcp_server(db_session, "https://mcp.example.com/", "runtime-1")
    synced_server = await db_session.scalar(select(MCPServer).where(MCPServer.slug == "saas-mcp"))

    assert synced_server.transport == "streamable_http"
    assert synced_server.url == "https://mcp.example.com/mcp"
    assert synced_server.instance_id == "runtime-1"


async def test_select_saas_mcp_server_requires_enabled_instance_and_url():
    assert _select_saas_mcp_server(
        [
            {"instanceId": "disabled", "mcpUrl": "https://disabled/mcp", "enabled": 0},
            {"instanceId": "runtime-1", "mcpUrl": "https://mcp.example.com"},
        ]
    ) == ("https://mcp.example.com", "runtime-1")


async def test_select_saas_mcp_server_uses_platform_url_when_item_only_has_instance_id():
    assert _select_saas_mcp_server(
        [{"instanceId": "runtime-1"}], fallback_url="https://mcp.example.com"
    ) == ("https://mcp.example.com", "runtime-1")


async def test_saas_login_limits_jwt_to_agent_session_lifetime(monkeypatch):
    expires_at = str(time.time() + 3600)
    employee = TenantEmployee(
        tenant_id=1,
        tenant_name="测试租户",
        employee_id=2,
        employee_code="E001",
        employee_name="测试员工",
        department_name="研发部",
        enabled=1,
        llm_key="",
        llm_url="",
        agent_session_token="session-token",
        agent_session_expires_at=expires_at,
    )

    class SaasClient:
        async def authenticate_employee(self, mobile, password):
            del mobile, password
            return EmployeeAuthResult(mobile="13800000000", tenants=[employee])

        async def get_tenant_config(self, tenant_id):
            del tenant_id
            raise RuntimeError("未配置 Platform")

        async def list_mcp_servers(self, tenant_id):
            del tenant_id
            return [{"instanceId": "runtime-1", "url": "https://mcp.example.com"}]

    user = SimpleNamespace(
        id=10,
        uid="E001",
        username="测试员工",
        phone_number="13800000000",
        avatar=None,
        role="user",
        department_id=None,
    )
    captured: dict = {}

    async def no_op(*_args, **_kwargs):
        return None

    def create_access_token(data, expires_delta=None):
        captured["data"] = data
        captured["expires_delta"] = expires_delta
        return "yuxi-token"

    monkeypatch.setattr(auth_router, "get_saas_client", lambda: SaasClient())
    monkeypatch.setattr(auth_router, "_upsert_saas_mcp_server", no_op)
    monkeypatch.setattr(auth_router, "_find_or_create_saas_user", lambda *_args, **_kwargs: _async_value(user))
    monkeypatch.setattr(auth_router, "log_operation", no_op)
    monkeypatch.setattr(auth_router.AuthUtils, "create_access_token", create_access_token)
    monkeypatch.setattr("yuxi.services.saas_identity.save_saas_employee_context", no_op)
    monkeypatch.setattr("yuxi.services.saas_session.save_saas_agent_session", no_op)

    db = SimpleNamespace(commit=no_op)
    result = await auth_router._saas_login(db, "13800000000", "password")

    assert result["access_token"] == "yuxi-token"
    assert captured["data"] == {"sub": "10"}
    assert 3500 <= captured["expires_delta"].total_seconds() <= 3600


async def _async_value(value):
    return value
