"""租户平台服务端调用的静态管理鉴权。"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException


@dataclass(frozen=True, slots=True)
class TenantAdminContext:
    """已通过静态 Key 校验的租户管理上下文。"""

    tenant_id: int


def require_tenant_admin(
    x_tenant_admin_key: str | None = Header(None, alias="X-Tenant-Admin-Key"),
    x_tenant_id: int | None = Header(None, alias="X-Tenant-Id"),
) -> TenantAdminContext:
    """校验静态管理 Key 与目标租户；SaaS 未启用时隐藏路由存在性。"""
    from yuxi.config import config as app_config

    if not app_config.is_saas_enabled or not app_config.tenant_admin_api_key:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_tenant_admin_key or not hmac.compare_digest(x_tenant_admin_key, app_config.tenant_admin_api_key):
        raise HTTPException(status_code=401, detail="无效的管理 API Key")
    if x_tenant_id is None:
        raise HTTPException(status_code=422, detail="缺少 X-Tenant-Id 请求头")
    return TenantAdminContext(tenant_id=x_tenant_id)
