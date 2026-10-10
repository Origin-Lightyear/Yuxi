"""SaaS 员工 KB 权限依赖的角色判定单元测试。"""

from types import SimpleNamespace

import pytest

from server.utils import employee_kb_permissions as permissions
from server.utils.employee_kb_permissions import is_employee


def test_is_employee_requires_local_user_role():
    assert is_employee(SimpleNamespace(uid="u1", role="user")) is True


def test_is_employee_excludes_admin_roles():
    assert is_employee(SimpleNamespace(uid="u1", role="admin")) is False
    assert is_employee(SimpleNamespace(uid="u1", role="superadmin")) is False


def test_is_employee_treats_missing_role_as_non_employee():
    # 生产 User.role 非空；缺字段的非模型对象不得进入员工权限路径
    assert is_employee(SimpleNamespace(uid="u1")) is False


@pytest.mark.asyncio
async def test_require_kb_read_or_employee_tolerates_missing_role(monkeypatch):
    # 缺 role 的非模型对象不得在管理员门槛处 AttributeError，按非管理员进入员工路径
    user = SimpleNamespace(uid="u1")

    async def fake_ensure_employee_kb(kb_id, current_user):
        assert kb_id == "kb-1"
        return object()

    monkeypatch.setattr(permissions, "ensure_employee_kb", fake_ensure_employee_kb)

    result = await permissions.require_kb_read_or_employee("kb-1", user)

    assert result is user


@pytest.mark.asyncio
async def test_require_documents_edit_or_kb_manage_tolerates_missing_role(monkeypatch):
    user = SimpleNamespace(uid="u1")

    async def fake_get_database_info(kb_id):
        assert kb_id == "kb-1"
        return object()

    class _EmptyRepository:
        async def list_by_file_ids(self, file_ids):
            del file_ids
            return []

    monkeypatch.setattr(permissions, "knowledge_base", SimpleNamespace(get_database_info=fake_get_database_info))
    monkeypatch.setattr(permissions, "require_knowledge_base_permission", lambda *args, **kwargs: None)
    monkeypatch.setattr(permissions, "KnowledgeFileRepository", _EmptyRepository)

    await permissions.require_documents_edit_or_kb_manage("kb-1", [], user)
