"""IM 账号绑定：用户侧绑定会话、解绑与公开 OAuth 回调接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services import im_binding_service
from yuxi.storage.postgres.models_business import User
from yuxi.utils import logger

user_im_bindings = APIRouter(prefix="/user/im-bindings", tags=["im-bindings"])
im_bindings_callback = APIRouter(prefix="/im-bindings/callback", tags=["im-bindings"])


def _result_page(message: str) -> HTMLResponse:
    """渲染最小 HTML 结果页，供手机端扫码后直接查看。"""
    html = (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        f"<title>IM 账号绑定</title></head><body><p>{message}</p></body></html>"
    )
    return HTMLResponse(html)


def _http_exception_reason(exc: HTTPException) -> str:
    """从结构化 detail 里取可展示的原因，不落日志、不回传 code 细节。"""
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or "绑定失败")
    return str(detail or "绑定失败")


@user_im_bindings.get("")
async def list_bindings(current_user: User = Depends(get_required_user), db: AsyncSession = Depends(get_db)):
    """列出当前用户的全部平台绑定。"""
    return {"success": True, "data": await im_binding_service.list_bindings(db, user_id=current_user.id)}


@user_im_bindings.post("/{platform}/sessions")
async def create_session(
    platform: str,
    request: Request,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """为用户创建一次性绑定会话，返回授权 URL（前端渲染二维码）。"""
    try:
        result = await im_binding_service.create_binding_session(
            db, user=current_user, platform=platform, base_url=str(request.base_url)
        )
        await db.commit()
        return {"success": True, "data": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@user_im_bindings.delete("/{platform}")
async def delete_binding(
    platform: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """解绑当前用户在某平台的绑定。"""
    try:
        deleted = await im_binding_service.delete_binding(db, user=current_user, platform=platform)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="该平台未绑定")
    return {"success": True}


@im_bindings_callback.get("/{platform}")
async def binding_callback(
    platform: str,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """平台 OAuth 回调：换取身份并写绑定，成功/失败都返回结果页，不向手机端抛 500。"""
    try:
        await im_binding_service.complete_binding(db, platform=platform, code=code, state=state)
    except HTTPException as exc:
        return _result_page(f"绑定失败：{_http_exception_reason(exc)}")
    except ValueError as exc:
        return _result_page(f"绑定失败：{exc}")
    except Exception:  # 平台网络/协议异常等，避免向手机端泄露内部错误。
        logger.exception("IM 绑定回调未预期异常")
        return _result_page("绑定失败：平台授权换取失败，请稍后重试")
    return _result_page("绑定成功，可关闭此页")
