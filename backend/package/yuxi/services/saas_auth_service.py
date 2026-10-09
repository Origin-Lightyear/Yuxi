"""SaaS 登录时的本地持久化同步用例：模型供应方、MCP 服务器与员工用户。"""

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.storage.postgres.models_business import Department, User
from yuxi.utils import logger
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.datetime_utils import utc_now_naive


async def find_or_create_saas_user(
    db: AsyncSession, mobile: str, employee_code: str, employee_name: str, department_name: str
) -> User:
    """按员工编码或手机号同步 SaaS 用户，不存在则创建。"""
    result = await db.execute(select(User).filter(User.uid == employee_code))
    user_by_uid = result.scalar_one_or_none()

    result = await db.execute(select(User).filter(User.phone_number == mobile))
    user_by_phone = result.scalar_one_or_none()

    if user_by_uid is not None and user_by_phone is not None and user_by_uid.id != user_by_phone.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="员工编码与手机号已绑定不同账户，请联系管理员",
        )

    user = user_by_uid or user_by_phone

    if user is not None:
        if user.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该账户已注销",
                headers={"WWW-Authenticate": "Bearer"},
            )

        dept = await _resolve_department(db, department_name) if department_name else None
        user.username = employee_name
        user.uid = employee_code
        user.phone_number = mobile
        if dept is not None:
            user.department = dept
        user.reset_failed_login()
        user.last_login = utc_now_naive()
        await db.flush()
        return user

    hashed_pw = AuthUtils.hash_password(AuthUtils.generate_api_key()[0])
    dept = await _resolve_department(db, department_name) if department_name else None

    new_user = User(
        username=employee_name,
        uid=employee_code,
        phone_number=mobile,
        password_hash=hashed_pw,
        role="user",
        department_id=dept.id if dept else None,
        last_login=utc_now_naive(),
    )
    db.add(new_user)
    await db.flush()
    await db.refresh(new_user)
    logger.info(f"SaaS local user created: mobile={mobile}, name={employee_name}")
    return new_user


async def _resolve_department(db: AsyncSession, name: str) -> Department | None:
    """按名称查找部门，不存在则在当前会话中创建。"""
    result = await db.execute(select(Department).filter(Department.name == name))
    dept = result.scalar_one_or_none()
    if dept is not None:
        return dept

    dept = Department(name=name, description=f"SaaS 自动创建: {name}")
    db.add(dept)
    await db.flush()
    await db.refresh(dept)
    return dept


async def upsert_saas_model_provider(db, llm_key, llm_url):
    """同步 SaaS LLM 配置，含自动拉取模型列表。保留已有的 request_body_overrides。"""
    from yuxi.models.providers.repository import create_model_provider
    from yuxi.models.providers.service import _normalize_payload
    from yuxi.storage.postgres.models_business import ModelProvider

    provider_id = "saas-newapi"
    result = await db.execute(select(ModelProvider).filter(ModelProvider.provider_id == provider_id))
    provider = result.scalar_one_or_none()

    # 保留已有的 overrides
    existing_overrides = {}
    if provider and provider.enabled_models:
        for m in provider.enabled_models:
            if m.get("request_body_overrides"):
                existing_overrides[m["id"]] = m["request_body_overrides"]

    # 自动拉取 NewAPI 模型列表
    enabled_models = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{llm_url.rstrip('/')}/v1/models",
                headers={"Authorization": f"Bearer {llm_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
            raw_models = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(raw_models, list):
                raise ValueError("NewAPI model response must contain a model list")
            for m_data in raw_models:
                if not isinstance(m_data, dict):
                    raise ValueError("NewAPI model list contains an invalid item")
                model_id = m_data.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    raise ValueError("NewAPI model item must contain a valid id")

                model_cfg = {
                    "id": model_id,
                    "type": "chat",
                    "source": "remote",
                    "display_name": m_data.get("name", model_id),
                }
                # 保留已有 overrides 并添加默认 thinking disable
                if model_id in existing_overrides:
                    model_cfg["request_body_overrides"] = existing_overrides[model_id]
                else:
                    model_cfg["request_body_overrides"] = {"thinking": {"type": "disabled"}}
                enabled_models.append(model_cfg)
            logger.info(f"SaaS NewAPI models fetched: {len(enabled_models)}")
    except Exception as exc:
        logger.warning(f"Failed to fetch SaaS NewAPI models: {exc}")

    else:
        # 成功返回空清单也要覆盖旧值；请求或解析失败时保留上一版配置。
        if provider is None:
            payload = _normalize_payload(
                {
                    "provider_id": provider_id,
                    "display_name": "SaaS NewAPI",
                    "provider_type": "openai",
                    "base_url": llm_url,
                    "api_key": llm_key,
                    "capabilities": ["chat"],
                    "enabled_models": enabled_models,
                    "is_enabled": True,
                    "is_builtin": False,
                }
            )
            payload["created_by"] = "saas"
            payload["updated_by"] = "saas"
            await create_model_provider(db, payload)
            logger.info(f"SaaS model provider created: {provider_id}")
        else:
            provider.base_url = llm_url
            provider.api_key = llm_key
            provider.is_enabled = True
            provider.enabled_models = enabled_models
            provider.updated_by = "saas"
            logger.info(f"SaaS model provider updated: {provider_id}")

    await db.flush()


async def upsert_saas_mcp_server(db, mcp_url: str, instance_id: str) -> None:
    """根据 SaaS 下发的 MCP URL 创建或更新 MCP 服务器配置。"""
    if not mcp_url:
        return
    from yuxi.storage.postgres.models_business import MCPServer

    slug = "saas-mcp"
    endpoint_url = mcp_url.rstrip("/")
    if not endpoint_url.endswith("/mcp"):
        endpoint_url = f"{endpoint_url}/mcp"

    result = await db.execute(select(MCPServer).filter(MCPServer.slug == slug))
    server = result.scalar_one_or_none()

    if server is None:
        server = MCPServer(
            slug=slug,
            name="SaaS MCP",
            transport="streamable_http",
            url=endpoint_url,
            instance_id=instance_id,
            description="SaaS 平台 MCP 服务",
            icon="🏭",
            enabled=True,
            created_by="saas",
            updated_by="saas",
        )
        db.add(server)
        logger.info(f"SaaS MCP server created: {slug} -> {endpoint_url}")
    else:
        server.transport = "streamable_http"
        server.url = endpoint_url
        server.instance_id = instance_id
        server.enabled = True
        logger.info(f"SaaS MCP server updated: {slug} -> {endpoint_url}")

    await db.flush()
