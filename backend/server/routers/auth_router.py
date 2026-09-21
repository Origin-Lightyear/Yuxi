import re
from datetime import timedelta
from yuxi.utils import logger

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, status, UploadFile, File
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import APIKey, User, Department
from yuxi.repositories.user_repository import UserRepository
from yuxi.repositories.department_repository import DepartmentRepository
from server.utils.auth_middleware import (
    get_admin_user,
    get_superadmin_user,
    get_db,
    get_required_user,
)
from yuxi.utils.auth_utils import AuthUtils
from yuxi.services.user_identity_service import generate_unique_uid, validate_username, is_valid_phone_number
from yuxi.services.operation_log_service import log_operation
from yuxi.services.auth_service import (
    CLI_AUTH_POLL_INTERVAL_SECONDS,
    CLI_AUTH_SESSION_TTL_SECONDS,
    CLIAuthError,
    approve_cli_auth_session,
    create_cli_auth_session,
    exchange_cli_auth_token,
    get_cli_auth_session_for_user,
)
from yuxi.storage.minio import upload_image_to_minio
from yuxi.storage.minio.client import normalize_public_minio_url
from yuxi.utils.datetime_utils import utc_now_naive

# OIDC 认证相关导入
from yuxi.services.oidc_service import (
    get_oidc_config_handler,
    oidc_callback_handler,
    oidc_exchange_code_handler,
    oidc_login_url_handler,
)

# SaaS 多租户认证相关导入
from yuxi.services.saas_client import SaasAPIError, get_saas_client
from yuxi.config import config as app_config
from server.routers.model_provider_router import _refresh_model_cache

# 创建路由器
auth = APIRouter(prefix="/auth", tags=["authentication"])


# 请求和响应模型
class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: int
    username: str
    uid: str  # 用于登录的user_id
    phone_number: str | None = None
    avatar: str | None = None
    role: str
    department_id: int | None = None
    department_name: str | None = None
    # SaaS 模式下的扩展字段
    saas_mode: bool = False
    employee_code: str | None = None


class UserCreate(BaseModel):
    username: str
    password: str = Field(min_length=8)
    role: str = "user"
    phone_number: str | None = None
    department_id: int | None = None


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = None
    password: str | None = Field(default=None, min_length=8)
    phone_number: str | None = None
    avatar: str | None = None
    department_id: int | None = None


class UserProfileUpdate(BaseModel):
    username: str | None = None
    phone_number: str | None = None


class UserResponse(BaseModel):
    id: int
    username: str
    uid: str
    phone_number: str | None = None
    avatar: str | None = None
    role: str
    department_id: int | None = None
    department_name: str | None = None  # 部门名称
    saas_mode: bool = False
    employee_code: str | None = None
    created_at: str
    last_login: str | None = None


class UserAccessOption(BaseModel):
    uid: str
    username: str
    role: str
    department_id: int | None = None
    department_name: str | None = None


class InitializeAdmin(BaseModel):
    uid: str  # 直接输入用户ID
    password: str = Field(min_length=8)
    phone_number: str | None = None


class UsernameValidation(BaseModel):
    username: str


class UidGeneration(BaseModel):
    username: str
    uid: str
    is_available: bool


class OIDCConfigResponse(BaseModel):
    """OIDC 配置响应"""

    enabled: bool
    login_url: str | None = None
    provider_name: str | None = "OIDC登录"


class OIDCLoginResponse(BaseModel):
    """OIDC 登录响应"""

    access_token: str
    token_type: str
    user_id: int
    username: str
    uid: str
    phone_number: str | None = None
    avatar: str | None = None
    role: str
    department_id: int | None = None
    department_name: str | None = None


class CLIAuthSessionCreate(BaseModel):
    key_name: str | None = Field(default=None, max_length=100)


class CLIAuthTokenRequest(BaseModel):
    device_code: str


class CLIAuthSessionCreateResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class CLIAuthSessionResponse(BaseModel):
    user_code: str
    status: str
    key_name: str
    created_at: str
    expires_at: str
    approved_at: str | None = None


class CLIAuthApproveResponse(BaseModel):
    user_code: str
    status: str
    approved_at: str | None = None


class CLIAuthTokenResponse(BaseModel):
    api_key: dict
    secret: str
    user: dict


# =============================================================================
# === 工具函数 ===
# =============================================================================


def _raise_cli_auth_error(exc: CLIAuthError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "message": exc.message},
    ) from exc


async def _upsert_saas_model_provider(db, llm_key, llm_url):
    """同步 SaaS LLM 配置，含自动拉取模型列表。保留已有的 request_body_overrides。"""
    from yuxi.storage.postgres.models_business import ModelProvider
    from yuxi.models.providers.service import _normalize_payload
    from yuxi.models.providers.repository import create_model_provider
    import httpx

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


async def _commit_saas_changes_and_refresh_model_cache(db) -> None:
    """提交 SaaS 登录同步结果后，刷新跨进程模型缓存。"""
    await db.commit()
    await _refresh_model_cache()


async def _upsert_saas_mcp_server(db, mcp_url: str, instance_id: str) -> None:
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


async def _find_or_create_saas_user(
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


def _select_saas_mcp_server(servers: list[dict], fallback_url: str = "") -> tuple[str, str] | None:
    """从 Tenant MCP 列表中选择当前 Agent 可用的 Runtime。"""
    for server in servers:
        if server.get("enabled") in (False, 0, "0", "DISABLED"):
            continue
        instance_id = str(server.get("instanceId") or server.get("mcpInstanceId") or "").strip()
        mcp_url = str(server.get("url") or server.get("mcpUrl") or server.get("endpoint") or fallback_url).strip()
        if instance_id and mcp_url:
            return mcp_url, instance_id
    return None


@auth.post("/logout")
async def logout(
    current_user: User = Depends(get_required_user),
):
    """注销本地 JWT 对应的 SaaS AgentSession。"""
    from yuxi.services.saas_session import clear_saas_agent_session, get_saas_agent_session

    session = await get_saas_agent_session(current_user.uid)
    if session is not None:
        try:
            await get_saas_client().logout_agent_session(session.token)
        except Exception as exc:
            logger.warning(f"Failed to logout SaaS AgentSession: {exc}")
        await clear_saas_agent_session(current_user.uid)
    return {"success": True}


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


async def _saas_login(db: AsyncSession, mobile: str, password: str, tenant_id: int | None = None) -> dict:
    """SaaS 模式登录：通过 Tenant 服务鉴权。

    单租户：直接同步用户并返回 JWT。
    多租户且未指定 tenant_id：返回租户列表供前端选择。
    多租户且已指定 tenant_id：使用指定租户登录。
    """
    saas = get_saas_client()

    try:
        auth_result = await saas.authenticate_employee(mobile, password)
    except SaasAPIError as exc:
        logger.warning(f"SaaS employee auth failed: code={exc.code}, msg={exc.msg}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="手机号或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as exc:
        logger.error(f"SaaS employee auth error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="认证服务暂时不可用，请稍后重试",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 如果指定了 tenant_id，选择对应租户
    if tenant_id:
        emp = next((t for t in auth_result.tenants if t.tenant_id == tenant_id), None)
        if not emp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="指定的租户不存在",
            )
    elif len(auth_result.tenants) == 1:
        emp = auth_result.primary
    else:
        # 多租户：返回列表供用户选择
        return {
            "need_select_tenant": True,
            "mobile": auth_result.mobile,
            "tenants": [
                {
                    "tenant_id": t.tenant_id,
                    "tenant_name": t.tenant_name,
                    "employee_name": t.employee_name,
                    "department_name": t.department_name,
                }
                for t in auth_result.tenants
            ],
        }

    if not emp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该员工未绑定任何租户",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not emp.agent_session_token or not emp.agent_session_expires_at:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="SaaS 登录响应缺少 AgentSession")
    if emp.enabled != 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该员工账户已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        tenant_cfg = await saas.get_tenant_config(emp.tenant_id)
    except Exception as exc:
        logger.warning(f"Failed to fetch tenant config: {exc}")
        tenant_cfg = None

    # MCP Runtime 实例和 URL 由 Tenant MCP 列表下发，不能由 Agent 自行推断。
    try:
        mcp_servers = await saas.list_mcp_servers(emp.tenant_id)
    except Exception as exc:
        logger.warning(f"Failed to load SaaS MCP servers: {exc}")
        mcp_servers = []
    selected_mcp = _select_saas_mcp_server(mcp_servers, fallback_url=tenant_cfg.mcp_url if tenant_cfg else "")
    if selected_mcp:
        try:
            await _upsert_saas_mcp_server(db, selected_mcp[0], selected_mcp[1])
        except Exception as exc:
            logger.warning(f"Failed to upsert SaaS MCP server: {exc}")

    llm_key = emp.llm_key or (tenant_cfg.llm_key if tenant_cfg else "")
    llm_url = emp.llm_url or (tenant_cfg.llm_url if tenant_cfg else "")
    if llm_key and llm_url:
        try:
            await _upsert_saas_model_provider(db, llm_key, llm_url)
        except Exception as exc:
            logger.warning(f"Failed to upsert SaaS model provider: {exc}")

    user = await _find_or_create_saas_user(
        db,
        mobile=mobile,
        employee_code=emp.employee_code,
        employee_name=emp.employee_name,
        department_name=emp.department_name,
    )

    # 持久化 SaaS 员工身份，供 Agent 数据双写与定时任务使用
    from yuxi.services.saas_identity import save_saas_employee_context
    from yuxi.services.saas_session import (
        SaasAgentSession,
        get_saas_session_ttl_seconds,
        save_saas_agent_session,
    )

    await save_saas_employee_context(db, user.uid, emp.tenant_id, emp.employee_id)
    await _commit_saas_changes_and_refresh_model_cache(db)

    if selected_mcp:
        await save_saas_agent_session(
            user.uid,
            SaasAgentSession(
                token=emp.agent_session_token,
                expires_at=emp.agent_session_expires_at,
                tenant_id=emp.tenant_id,
                employee_id=emp.employee_id,
                mcp_instance_id=selected_mcp[1],
            ),
        )

    token_data = {"sub": str(user.id)}
    access_token = AuthUtils.create_access_token(
        token_data,
        expires_delta=timedelta(seconds=get_saas_session_ttl_seconds(emp.agent_session_expires_at)),
    )

    await log_operation(
        db,
        user.id,
        "SaaS 登录",
        f"tenant_id={emp.tenant_id}, employee_id={emp.employee_id}",
    )

    department_name = None
    if user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == user.department_id))
        department_name = result.scalar_one_or_none()

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "uid": user.uid,
        "phone_number": user.phone_number,
        "avatar": normalize_public_minio_url(user.avatar),
        "role": user.role,
        "department_id": user.department_id,
        "department_name": department_name,
        "saas_mode": True,
        "employee_code": emp.employee_code,
    }


# 路由：登录获取令牌
# =============================================================================
# === 认证分组 ===
# =============================================================================


@auth.post("/token", response_model=Token)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Form(None),
):
    login_identifier = form_data.username

    # --- SaaS 模式：委托 Tenant 服务鉴权 ---
    if app_config.is_saas_enabled:
        return await _saas_login(db, login_identifier, form_data.password, tenant_id)

    # --- 本地认证模式 ---
    # 尝试通过user_id查找
    result = await db.execute(select(User).filter(User.uid == login_identifier))
    user = result.scalar_one_or_none()

    # 如果通过user_id没找到，尝试通过phone_number查找
    if not user:
        result = await db.execute(select(User).filter(User.phone_number == login_identifier))
        user = result.scalar_one_or_none()

    # 如果用户不存在，为防止用户名枚举攻击，返回通用错误信息
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录标识或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 检查用户是否已被删除
    if user.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该账户已注销",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 检查用户是否处于登录锁定状态
    if user.is_login_locked():
        remaining_time = user.get_remaining_lock_time()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"登录被锁定，请等待 {remaining_time} 秒后再试",
            headers={"WWW-Authenticate": "Bearer", "X-Lock-Remaining": str(remaining_time)},
        )

    # 验证密码
    if not AuthUtils.verify_password(user.password_hash, form_data.password):
        # 密码错误，增加失败次数
        user.increment_failed_login()
        await db.commit()

        # 记录失败操作
        await log_operation(db, user.id if user else None, "登录失败", f"密码错误，失败次数: {user.login_failed_count}")

        # 检查是否需要锁定
        if user.is_login_locked():
            remaining_time = user.get_remaining_lock_time()
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=f"由于多次登录失败，账户已被锁定 {remaining_time} 秒",
                headers={"WWW-Authenticate": "Bearer", "X-Lock-Remaining": str(remaining_time)},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # 登录成功，重置失败计数器
    user.reset_failed_login()
    user.last_login = utc_now_naive()
    await db.commit()

    # 生成访问令牌
    token_data = {"sub": str(user.id)}
    access_token = AuthUtils.create_access_token(token_data)

    # 记录登录操作
    await log_operation(db, user.id, "登录")

    # 获取部门名称
    department_name = None
    if user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == user.department_id))
        department_name = result.scalar_one_or_none()

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "uid": user.uid,
        "phone_number": user.phone_number,
        "avatar": normalize_public_minio_url(user.avatar),
        "role": user.role,
        "department_id": user.department_id,
        "department_name": department_name,
        "saas_mode": False,
        "employee_code": None,
    }


# =============================================================================
# === CLI 浏览器登录授权分组 ===
# =============================================================================


@auth.post("/cli/sessions", response_model=CLIAuthSessionCreateResponse)
async def create_cli_session(data: CLIAuthSessionCreate, db: AsyncSession = Depends(get_db)):
    session, device_code = await create_cli_auth_session(db, key_name=data.key_name)
    return CLIAuthSessionCreateResponse(
        device_code=device_code,
        user_code=session.user_code,
        verification_uri="/auth/cli/authorize",
        expires_in=CLI_AUTH_SESSION_TTL_SECONDS,
        interval=CLI_AUTH_POLL_INTERVAL_SECONDS,
    )


@auth.get("/cli/sessions/{user_code}", response_model=CLIAuthSessionResponse)
async def get_cli_session(
    user_code: str,
    _current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        session = await get_cli_auth_session_for_user(db, user_code)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)
    return CLIAuthSessionResponse(**session.to_dict())


@auth.post("/cli/sessions/{user_code}/approve", response_model=CLIAuthApproveResponse)
async def approve_cli_session(
    user_code: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        session = await approve_cli_auth_session(db, user_code, current_user)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)
    return CLIAuthApproveResponse(**session.to_dict())


@auth.post("/cli/sessions/token", response_model=CLIAuthTokenResponse)
async def exchange_cli_session_token(data: CLIAuthTokenRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await exchange_cli_auth_token(db, data.device_code)
    except CLIAuthError as exc:
        _raise_cli_auth_error(exc)


# 路由：校验是否需要初始化管理员
@auth.get("/check-first-run")
async def check_first_run():
    is_first_run = await pg_manager.async_check_first_run()
    return {"first_run": is_first_run}


# 路由：初始化管理员账户
@auth.post("/initialize", response_model=Token)
async def initialize_admin(admin_data: InitializeAdmin, db: AsyncSession = Depends(get_db)):
    # 检查是否是首次运行
    if not await pg_manager.async_check_first_run():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="系统已经初始化，无法再次创建初始管理员",
        )

    # 创建管理员账户
    hashed_password = AuthUtils.hash_password(admin_data.password)

    # 验证用户ID格式（只支持字母数字和下划线）
    if not re.match(r"^[a-zA-Z0-9_]+$", admin_data.uid):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户ID只能包含字母、数字和下划线",
        )

    if len(admin_data.uid) < 3 or len(admin_data.uid) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户ID长度必须在3-20个字符之间",
        )

    # 验证手机号格式（如果提供了）
    if admin_data.phone_number and not is_valid_phone_number(admin_data.phone_number):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号格式不正确")

    # 由于是首次初始化，直接使用输入的user_id
    uid = admin_data.uid

    # 创建默认部门
    dept_repo = DepartmentRepository()
    default_department = await dept_repo.create(
        {
            "name": "默认部门",
            "description": "系统初始化时创建的默认部门",
        }
    )

    # 创建管理员用户
    user_repo = UserRepository()
    new_admin = await user_repo.create(
        {
            "username": admin_data.uid,
            "uid": uid,
            "phone_number": admin_data.phone_number,
            "avatar": None,
            "password_hash": hashed_password,
            "role": "superadmin",
            "department_id": default_department.id,
            "last_login": utc_now_naive(),
        }
    )

    # 生成访问令牌
    token_data = {"sub": str(new_admin.id)}
    access_token = AuthUtils.create_access_token(token_data)

    # 记录操作
    await log_operation(db, new_admin.id, "系统初始化", "创建超级管理员账户")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": new_admin.id,
        "username": new_admin.username,
        "uid": new_admin.uid,
        "phone_number": new_admin.phone_number,
        "avatar": new_admin.avatar,
        "role": new_admin.role,
    }


# 路由：获取当前用户信息
# =============================================================================
# === 用户信息分组 ===
# =============================================================================


@auth.get("/me", response_model=UserResponse)
async def read_users_me(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    """获取当前登录用户的个人信息"""
    user_dict = current_user.to_dict()

    if current_user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == current_user.department_id))
        user_dict["department_name"] = result.scalar_one_or_none()

    from yuxi.services.saas_identity import get_saas_employee_context

    saas_context = await get_saas_employee_context(db, current_user.uid)
    user_dict["saas_mode"] = saas_context is not None
    user_dict["employee_code"] = current_user.uid if saas_context is not None else None

    return user_dict


# 路由：更新个人资料
@auth.put("/profile", response_model=UserResponse)
async def update_profile(
    profile_data: UserProfileUpdate,
    request: Request,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """更新当前用户的个人资料"""
    update_details = []

    # 更新用户名（仅允许修改显示名，不修改 user_id）
    if profile_data.username is not None:
        # 验证用户名格式
        is_valid, error_msg = validate_username(profile_data.username)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg,
            )

        # 检查用户名是否已被其他用户使用
        result = await db.execute(
            select(User).filter(User.username == profile_data.username, User.id != current_user.id)
        )
        existing_user = result.scalar_one_or_none()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已存在",
            )

        current_user.username = profile_data.username
        update_details.append(f"用户名: {profile_data.username}")

    # 更新手机号
    if profile_data.phone_number is not None:
        # 如果手机号不为空，验证格式
        if profile_data.phone_number and not is_valid_phone_number(profile_data.phone_number):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号格式不正确")

        # 检查手机号是否已被其他用户使用
        if profile_data.phone_number:
            result = await db.execute(
                select(User).filter(User.phone_number == profile_data.phone_number, User.id != current_user.id)
            )
            existing_phone = result.scalar_one_or_none()
            if existing_phone:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号已被其他用户使用")

        current_user.phone_number = profile_data.phone_number
        update_details.append(f"手机号: {profile_data.phone_number or '已清空'}")

    await db.commit()

    # 记录操作
    if update_details:
        await log_operation(db, current_user.id, "更新个人资料", f"更新个人资料: {', '.join(update_details)}", request)

    return current_user.to_dict()


# 路由：创建新用户（管理员权限）
# =============================================================================
# === 用户管理分组 ===
# =============================================================================


@auth.post("/users", response_model=UserResponse)
async def create_user(
    user_data: UserCreate,
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建新用户（管理员权限）"""
    user_repo = UserRepository()

    # 验证用户名
    is_valid, error_msg = validate_username(user_data.username)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # 检查用户名是否已存在
    users = await user_repo.list_users()
    if any(u.username == user_data.username for u in users):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在",
        )

    # 检查手机号是否已存在（如果提供了）
    if user_data.phone_number:
        if await user_repo.exists_by_phone(user_data.phone_number):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="手机号已存在",
            )

    # 生成唯一的 uid
    existing_uids = await user_repo.get_all_uids()
    uid = generate_unique_uid(user_data.username, existing_uids)

    # 创建新用户
    hashed_password = AuthUtils.hash_password(user_data.password)

    # 检查角色权限
    # 禁止创建超级管理员账户（系统只能有一个超级管理员）
    if user_data.role == "superadmin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能创建超级管理员账户",
        )

    # 管理员只能创建普通用户
    if current_user.role == "admin" and user_data.role != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员只能创建普通用户账户",
        )

    # 部门分配逻辑
    if current_user.role == "superadmin":
        # 超级管理员创建用户时，使用指定的部门或默认部门
        department_id = user_data.department_id
        if department_id is None:
            # 获取默认部门
            dept_repo = DepartmentRepository()
            departments = await dept_repo.list_departments()
            default_dept = next((d for d in departments if d.name == "默认部门"), None)
            department_id = default_dept.id if default_dept else None
    else:
        # 普通管理员创建用户时，自动继承该管理员的部门
        department_id = current_user.department_id
        if department_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="管理员必须属于部门才能创建用户",
            )
        # 非超级管理员不能指定部门
        if user_data.department_id is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="普通管理员不能指定部门",
            )

    new_user = await user_repo.create(
        {
            "username": user_data.username,
            "uid": uid,
            "phone_number": user_data.phone_number,
            "password_hash": hashed_password,
            "role": user_data.role,
            "department_id": department_id,
        }
    )

    # 记录操作
    await log_operation(
        db, current_user.id, "创建用户", f"创建用户: {user_data.username}, 角色: {user_data.role}", request
    )

    return new_user.to_dict()


# 路由：获取所有用户（管理员权限）
@auth.get("/users", response_model=list[UserResponse])
async def read_users(
    skip: int = 0, limit: int = 100, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)
):
    user_repo = UserRepository()

    # 部门隔离逻辑
    if current_user.role == "superadmin":
        # 超级管理员可以看到所有用户
        users_with_dept = await user_repo.list_with_department(skip=skip, limit=limit)
    else:
        # 普通管理员只能看到本部门用户
        users_with_dept = await user_repo.list_with_department(
            skip=skip, limit=limit, department_id=current_user.department_id
        )

    users = []
    for user, dept_name in users_with_dept:
        user_dict = user.to_dict()
        user_dict["department_name"] = dept_name
        users.append(user_dict)
    return users


def _ensure_user_in_current_department(current_user: User, target_user: User) -> None:
    if current_user.role == "superadmin":
        return
    if target_user.department_id != current_user.department_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只能管理本部门用户",
        )


@auth.get("/users/access-options", response_model=list[UserAccessOption])
async def read_user_access_options(
    skip: int = 0,
    limit: int = 1000,
    current_user: User = Depends(get_admin_user),
):
    user_repo = UserRepository()
    if current_user.role == "superadmin":
        users_with_dept = await user_repo.list_with_department(skip=skip, limit=limit)
    else:
        users_with_dept = await user_repo.list_with_department(
            skip=skip, limit=limit, department_id=current_user.department_id
        )
    return [
        {
            "uid": user.uid,
            "username": user.username,
            "role": user.role,
            "department_id": user.department_id,
            "department_name": dept_name,
        }
        for user, dept_name in users_with_dept
    ]


# 路由：获取特定用户信息（管理员权限）
@auth.get("/users/{user_id}", response_model=UserResponse)
async def read_user(user_id: int, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )
    _ensure_user_in_current_department(current_user, user)
    return user.to_dict()


# 路由：更新用户信息（管理员权限）
@auth.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    _ensure_user_in_current_department(current_user, user)

    # 检查权限
    if user.role == "superadmin" and current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有超级管理员才能修改超级管理员账户",
        )

    if current_user.role == "admin":
        if user.role != "user":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="管理员只能修改普通用户账户",
            )

    # 更新信息
    update_details = []

    if user_data.username is not None:
        # 检查用户名是否已被其他用户使用
        result = await db.execute(select(User).filter(User.username == user_data.username, User.id != user_id))
        existing_user = result.scalar_one_or_none()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已存在",
            )
        user.username = user_data.username
        update_details.append(f"用户名: {user_data.username}")

    if user_data.password is not None:
        user.password_hash = AuthUtils.hash_password(user_data.password)
        update_details.append("密码已更新")

    if user_data.phone_number is not None:
        user.phone_number = user_data.phone_number
        update_details.append(f"手机号: {user_data.phone_number or '已清空'}")

    if user_data.avatar is not None:
        user.avatar = user_data.avatar
        update_details.append(f"头像: {user_data.avatar or '已清空'}")

    # 部门修改权限控制（只有超级管理员可以修改用户部门）
    if user_data.department_id is not None and user_data.department_id != user.department_id:
        if current_user.role != "superadmin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="只有超级管理员才能修改用户部门",
            )

        # 检查该用户是否是当前部门的唯一管理员
        if user.role == "admin" and user.department_id is not None:
            admin_count = await UserRepository().get_admin_count_in_department(
                user.department_id, exclude_user_id=user_id
            )
            if admin_count <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="不能修改该用户的部门，因为该用户是当前部门的唯一管理员",
                )

        user.department_id = user_data.department_id
        update_details.append(f"部门ID: {user_data.department_id}")

    await db.commit()

    # 记录操作
    await log_operation(db, current_user.id, "更新用户", f"更新用户ID {user_id}: {', '.join(update_details)}", request)

    return user.to_dict()


# 路由：删除用户（管理员权限）
@auth.delete("/users/{user_id}", response_model=dict)
async def delete_user(
    user_id: int, request: Request, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    _ensure_user_in_current_department(current_user, user)

    # 不能删除超级管理员账户
    if user.role == "superadmin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除超级管理员账户",
        )

    if current_user.role == "admin" and user.role != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员只能删除普通用户账户",
        )

    # 检查是否是部门的唯一管理员
    if user.role == "admin" and current_user.role != "superadmin":
        result = await db.execute(
            select(func.count(User.id)).filter(
                User.department_id == user.department_id, User.role == "admin", User.is_deleted == 0
            )
        )
        admin_count = result.scalar()
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="不能删除部门唯一的管理员",
            )

    # 不能删除自己的账户
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除自己的账户",
        )

    # 检查是否已经被删除
    if user.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该用户已经被删除",
        )

    deletion_detail = f"删除用户: {user.username}, ID: {user.id}, 角色: {user.role}"

    user.is_deleted = 1
    user.deleted_at = utc_now_naive()
    user.username = f"已注销用户-{user.id}"
    user.phone_number = None  # 清空手机号，释放该手机号供其他用户使用
    user.password_hash = "DELETED"  # 禁止登录
    user.avatar = None  # 清空头像
    api_key_result = await db.execute(select(APIKey).filter(APIKey.user_id == user.id))
    for api_key in api_key_result.scalars().all():
        api_key.is_enabled = False

    await db.commit()

    # 记录操作
    await log_operation(db, current_user.id, "删除用户", deletion_detail, request)

    return {"success": True, "message": "用户已删除"}


# 路由：验证用户名并生成user_id
@auth.post("/validate-username", response_model=UidGeneration)
async def validate_username_and_generate_uid(
    validation_data: UsernameValidation,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """验证用户名格式并生成可用的user_id"""
    # 验证用户名格式
    is_valid, error_msg = validate_username(validation_data.username)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # 检查用户名是否已存在
    result = await db.execute(select(User).filter(User.username == validation_data.username))
    existing_user = result.scalar_one_or_none()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在",
        )

    # 生成唯一的 uid
    result = await db.execute(select(User.uid))
    existing_uids = [uid for (uid,) in result.all()]
    uid = generate_unique_uid(validation_data.username, existing_uids)

    return UidGeneration(username=validation_data.username, uid=uid, is_available=True)


# 路由：检查 uid 是否可用
@auth.get("/check-uid/{uid}")
async def check_uid_availability(
    uid: str, current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)
):
    """检查 uid 是否可用"""
    result = await db.execute(select(User).filter(User.uid == uid))
    existing_user = result.scalar_one_or_none()
    return {"uid": uid, "is_available": existing_user is None}


# 路由：上传用户头像
@auth.post("/upload-avatar")
async def upload_user_avatar(
    file: UploadFile = File(...), current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)
):
    """上传用户头像"""
    try:
        avatar_url = await upload_image_to_minio(
            file,
            object_prefix=f"avatar/{current_user.id}",
            max_size_bytes=5 * 1024 * 1024,
            too_large_message="文件大小不能超过5MB",
        )

        current_user.avatar = avatar_url
        await db.commit()
        await log_operation(db, current_user.id, "上传头像", f"更新头像: {avatar_url}")

        return {"success": True, "avatar_url": avatar_url, "message": "头像上传成功"}

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"头像上传失败: {str(e)}")


# 路由：模拟用户登录（超级管理员专用）
@auth.post("/impersonate/{user_id}", response_model=Token)
async def impersonate_user(
    user_id: int,
    request: Request,
    current_user: User = Depends(get_superadmin_user),
    db: AsyncSession = Depends(get_db),
):
    """超级管理员模拟其他用户登录"""
    # 查找目标用户
    result = await db.execute(select(User).filter(User.id == user_id, User.is_deleted == 0))
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    # 不能模拟超级管理员
    if target_user.role == "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="不能模拟超级管理员账户",
        )

    # 生成访问令牌
    token_data = {"sub": str(target_user.id)}
    access_token = AuthUtils.create_access_token(token_data)

    # 获取部门名称
    department_name = None
    if target_user.department_id:
        result = await db.execute(select(Department.name).filter(Department.id == target_user.department_id))
        department_name = result.scalar_one_or_none()

    # 记录操作（危险操作标记）
    await log_operation(db, current_user.id, "⚠️ 危险操作-模拟用户", f"模拟用户: {target_user.username}", request)

    # 控制台警告日志
    logger.warning(f"⚠️ [危险操作] 超级管理员 {current_user.username} 模拟登录用户: {target_user.username}")

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": target_user.id,
        "username": target_user.username,
        "uid": target_user.uid,
        "phone_number": target_user.phone_number,
        "avatar": normalize_public_minio_url(target_user.avatar),
        "role": target_user.role,
        "department_id": target_user.department_id,
        "department_name": department_name,
    }


# =============================================================================
# === OIDC 认证分组 ===
# =============================================================================


@auth.get("/oidc/config", response_model=OIDCConfigResponse)
async def get_oidc_config():
    """获取 OIDC 配置（供前端使用）"""
    return await get_oidc_config_handler()


@auth.get("/oidc/login-url")
async def get_oidc_login_url(redirect_path: str = "/"):
    """获取 OIDC 登录 URL"""
    return await oidc_login_url_handler(redirect_path)


@auth.get("/oidc/callback", response_class=RedirectResponse)
async def oidc_callback(request: Request, code: str, state: str, db: AsyncSession = Depends(get_db)):
    """处理 OIDC 回调 - 重定向到前端 Vue 路由"""
    return await oidc_callback_handler(code, state, db, request)


@auth.post("/oidc/exchange-code", response_model=OIDCLoginResponse)
async def oidc_exchange_code(code: str = Body(..., embed=True)):
    """使用一次性 code 交换 OIDC 登录数据"""
    return await oidc_exchange_code_handler(code)
