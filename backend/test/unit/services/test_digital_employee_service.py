from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services.digital_employee_service import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_DRAFT,
    ensure_digital_employee_can_start,
    normalize_profile_patch,
    validate_profile_for_publish,
)


def _agent(*, status="active", share_config=None, created_by="owner", slug="employee"):
    return SimpleNamespace(
        slug=slug,
        kind="digital_employee",
        created_by=created_by,
        is_subagent=False,
        share_config=share_config
        or {
            "version": 2,
            "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
            "manage_scope": None,
        },
        digital_employee_profile={
            "position": "经营分析官",
            "department": "经营管理",
            "responsibilities": "输出分析",
            "capabilities": ["数据查询"],
            "boundaries": "不代替决策",
            "acceptance": "包含数据口径",
            "status": status,
        },
    )


def _user(role="user", uid="reader"):
    return SimpleNamespace(uid=uid, role=role, department_id=1, tenant_department_id=None)


def test_normalize_profile_patch_preserves_publish_metadata_and_ignores_client_status():
    current = {
        "position": "旧岗位",
        "capabilities": ["旧能力"],
        "status": STATUS_ACTIVE,
        "published_at": "2026-10-26T10:00:00",
        "published_by": "admin",
    }
    merged = normalize_profile_patch(
        {"position": " 新岗位 ", "capabilities": [" 数据查询 ", "数据查询", "报告生成"], "status": "draft"},
        current,
    )
    assert merged["position"] == "新岗位"
    assert merged["capabilities"] == ["数据查询", "报告生成"]
    assert merged["status"] == STATUS_ACTIVE
    assert merged["published_at"] == "2026-10-26T10:00:00"
    assert merged["published_by"] == "admin"


def test_validate_profile_for_publish_rejects_missing_position_and_capabilities():
    with pytest.raises(ValueError, match="岗位"):
        validate_profile_for_publish(normalize_profile_patch({"capabilities": ["数据查询"]}))
    with pytest.raises(ValueError, match="能力"):
        validate_profile_for_publish(
            normalize_profile_patch(
                {
                    "position": "经营分析官",
                    "department": "经营管理",
                    "responsibilities": "输出分析",
                    "boundaries": "不代替决策",
                    "acceptance": "包含数据口径",
                    "capabilities": [],
                }
            )
        )


def test_normalize_profile_patch_rejects_too_many_capabilities():
    with pytest.raises(ValueError, match="12"):
        normalize_profile_patch({"capabilities": [f"能力{i}" for i in range(13)]})


def test_digital_employee_start_guard_hides_draft_from_non_manager_and_blocks_disabled():
    draft = _agent(status=STATUS_DRAFT)
    disabled = _agent(status=STATUS_DISABLED)
    active = _agent(status=STATUS_ACTIVE)

    with pytest.raises(HTTPException) as hidden:
        ensure_digital_employee_can_start(_user(), draft)
    assert hidden.value.status_code == 404

    with pytest.raises(HTTPException) as blocked:
        ensure_digital_employee_can_start(_user(), disabled)
    assert blocked.value.status_code == 409

    ensure_digital_employee_can_start(_user(), active)

    owner = _user(uid="owner")
    with pytest.raises(HTTPException) as manager_blocked:
        ensure_digital_employee_can_start(owner, draft)
    assert manager_blocked.value.status_code == 409


def test_ensure_can_start_allows_non_employee_agents():
    normal = SimpleNamespace(kind="agent", share_config={}, created_by="owner", slug="normal")
    ensure_digital_employee_can_start(_user(), normal)
