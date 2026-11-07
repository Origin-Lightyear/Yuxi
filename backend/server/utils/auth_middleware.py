import hashlib
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import APIKey, User
from yuxi.utils.datetime_utils import utc_now_naive

from yuxi.utils.auth_utils import AuthUtils

# 定义OAuth2密码承载器，指定token URL
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


# 获取数据库会话（异步版本）
async def get_db():
    async with pg_manager.get_async_session_context() as db:
        yield db


async def _verify_api_key(key: str, db: AsyncSession) -> tuple[User | None, APIKey | None]:
    """验证 API Key 并返回关联用户和 APIKey 对象"""
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    result = await db.execute(select(APIKey).filter(APIKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()

    if api_key is None:
        return None, None

    if not api_key.is_enabled or api_key.revoked_at is not None:
        return None, None

    if api_key.expires_at and utc_now_naive() > api_key.expires_at:
        return None, None

    if not api_key.user_id:
        return None, None

    result = await db.execute(select(User).filter(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if user and not user.is_deleted:
        return user, api_key

    return None, None


@dataclass(frozen=True)
class AuthPrincipal:
    """当前请求的认证主体；``api_key_id`` 仅在 API Key 认证时存在。"""

    user: User
    api_key_id: int | None = None


async def _resolve_principal(authorization: str | None, db: AsyncSession) -> AuthPrincipal | None:
    """解析 Bearer 凭证并返回认证主体，JWT 与 API Key 都保留来源差异。"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if authorization is None:
        return None

    if not authorization.startswith("Bearer "):
        return None

    token = authorization.split("Bearer ")[1]
    if not token:
        return None

    # 根据 token 前缀判断认证方式
    if token.startswith("yxkey_"):
        # API Key 认证
        user, api_key_obj = await _verify_api_key(token, db)
        if user is not None and api_key_obj is not None:
            api_key_obj.last_used_at = utc_now_naive()
            await db.commit()
            return AuthPrincipal(user=user, api_key_id=api_key_obj.id)
        return None

    # JWT Token 认证
    try:
        payload = AuthUtils.verify_access_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).filter(User.id == int(user_id), User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    if user.is_login_locked():
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="登录被锁定，请稍后重试",
            headers={"X-Lock-Remaining": str(user.get_remaining_lock_time())},
        )

    from yuxi.services.saas_identity import get_saas_employee_context
    from yuxi.services.saas_session import get_saas_agent_session

    saas_context = await get_saas_employee_context(db, user.uid)
    if saas_context is not None:
        # 权限判断需要租户 ID 与租户部门 ID；本地 department_id 仅用于资料展示和本地管理。
        user.tenant_id = saas_context.tenant_id
        user.tenant_department_id = saas_context.tenant_department_id
        session = await get_saas_agent_session(user.uid)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Agent登录状态已失效，请重新登录",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return AuthPrincipal(user=user, api_key_id=None)


# 获取当前认证主体（异步版本）
async def get_current_principal(
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> AuthPrincipal | None:
    return await _resolve_principal(authorization, db)


# 获取当前用户（异步版本）
async def get_current_user(
    principal: AuthPrincipal | None = Depends(get_current_principal),
) -> User | None:
    return principal.user if principal is not None else None


# 获取已登录认证主体（抛出401如果未登录）
async def get_required_principal(
    principal: AuthPrincipal | None = Depends(get_current_principal),
) -> AuthPrincipal:
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请登录后再访问",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not principal.user.department_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前用户未绑定部门",
        )
    return principal


# 获取已登录用户（抛出401如果未登录）
async def get_required_user(
    principal: AuthPrincipal = Depends(get_required_principal),
) -> User:
    return principal.user


# 获取管理员用户
async def get_admin_user(current_user: User = Depends(get_required_user)):
    if current_user.role not in ["admin", "superadmin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


# 获取超级管理员用户
async def get_superadmin_user(current_user: User = Depends(get_required_user)):
    if current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要超级管理员权限",
        )
    return current_user
