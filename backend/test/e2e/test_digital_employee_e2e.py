"""数字员工真实 HTTP + worker + deterministic replay 对话链路。"""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from e2e_helpers import postgres_dsn, wait_for_run

from test.live_api_cleanup import make_test_conversation_title
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow, pytest.mark.timeout(360)]

EXPECTED_OUTPUT = "DETERMINISTIC_AGENT_E2E_OK"
PROVIDER_ID = "ci-replay-digital"
MODEL_SPEC = f"{PROVIDER_ID}:deterministic-chat"
ADMIN_UID = "pytest-digital-admin"


@pytest.fixture(scope="session", autouse=True)
def cleanup_e2e_test_resources():
    """本文件使用本地测试管理员 JWT，不启动依赖 SaaS 登录的会话清理钩子。"""
    yield


@pytest.fixture(scope="module", autouse=True)
def require_replay_gateway():
    """replay 网关不可用时明确跳过，避免把环境缺失写成产品通过。"""
    try:
        response = httpx.get("http://localhost:8765/health", timeout=3)
        if response.status_code != 200:
            pytest.skip("Not run: deterministic replay gateway unavailable")
    except Exception:
        pytest.skip("Not run: deterministic replay gateway unavailable")


@pytest_asyncio.fixture()
async def e2e_headers(e2e_client) -> dict[str, str]:
    """为本地测试管理员签发 JWT，保留真实 HTTP 鉴权路径。"""
    response = await e2e_client.get("/api/auth/check-first-run")
    if response.status_code != 200:
        pytest.skip("local API is not ready for E2E")
    async with asyncpg.connect(postgres_dsn()) as conn:
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE uid = $1 AND is_deleted = 0",
            ADMIN_UID,
        )
    if user_id is None:
        pytest.skip(f"local E2E admin {ADMIN_UID} is not provisioned")
    token = AuthUtils.create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


async def _create_provider(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post(
        "/api/system/model-providers",
        json={
            "provider_id": PROVIDER_ID,
            "display_name": "CI digital employee replay",
            "provider_type": "openai",
            "base_url": "http://api:8765/v1",
            "api_key": "ci-replay-key",
            "capabilities": ["chat"],
            "enabled_models": [
                {
                    "id": "deterministic-chat",
                    "display_name": "Deterministic chat",
                    "type": "chat",
                    "source": "manual",
                }
            ],
            "is_enabled": True,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


async def _delete_provider(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    response = await client.delete(f"/api/system/model-providers/{PROVIDER_ID}", headers=headers)
    assert response.status_code in {200, 404}, response.text


def _employee_payload(slug: str) -> dict:
    return {
        "name": "Deterministic 数字员工",
        "slug": slug,
        "backend_id": "ChatbotAgent",
        "description": "P1 数字员工真实运行链路",
        "profile": {
            "position": "经营分析官",
            "department": "经营管理",
            "responsibilities": "输出经营分析",
            "capabilities": ["数据查询", "报告生成"],
            "boundaries": "不代替管理层决策",
            "acceptance": "结论包含数据口径",
        },
        "config_json": {
            "context": {
                "model": MODEL_SPEC,
                "system_prompt": f"不要调用工具，只输出 {EXPECTED_OUTPUT}。",
                "tools": [],
                "knowledges": [],
                "mcps": [],
                "skills": ["image-gen"],
                "preload_skills": ["image-gen"],
            }
        },
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [ADMIN_UID]},
            "manage_scope": None,
        },
    }


async def test_digital_employee_runs_through_real_worker(e2e_client, e2e_headers):
    """发布员工后，真实 worker 使用员工配置完成一次 Run 并返回确定性输出。"""
    await _create_provider(e2e_client, e2e_headers)
    slug = f"e2e-digital-employee-{uuid.uuid4().hex[:8]}"
    thread_id = None
    try:
        created = await e2e_client.post(
            "/api/digital-employees",
            headers=e2e_headers,
            json=_employee_payload(slug),
        )
        assert created.status_code == 200, created.text
        published = await e2e_client.post(f"/api/digital-employees/{slug}/publish", headers=e2e_headers)
        assert published.status_code == 200, published.text

        thread = await e2e_client.post(
            "/api/chat/thread",
            headers=e2e_headers,
            json={"agent_id": slug, "title": make_test_conversation_title("数字员工 deterministic E2E")},
        )
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]

        run_response = await e2e_client.post(
            "/api/agent/runs",
            headers=e2e_headers,
            json={
                "agent_slug": slug,
                "thread_id": thread_id,
                "query": f"只输出 {EXPECTED_OUTPUT}",
                "meta": {"request_id": str(uuid.uuid4())},
            },
        )
        assert run_response.status_code == 200, run_response.text
        run = await wait_for_run(e2e_client, e2e_headers, str(run_response.json()["run_id"]))
        assert run["status"] == "completed", run

        result = await e2e_client.get(
            f"/api/agent/runs/{run_response.json()['run_id']}/result",
            headers=e2e_headers,
        )
        assert result.status_code == 200, result.text
        assert result.json()["output"] == EXPECTED_OUTPUT, result.text
    finally:
        if thread_id:
            await e2e_client.delete(f"/api/chat/thread/{thread_id}", headers=e2e_headers)
        await _delete_provider(e2e_client, e2e_headers)
        async with asyncpg.connect(postgres_dsn()) as conn:
            await conn.execute("DELETE FROM agents WHERE slug = $1", slug)
