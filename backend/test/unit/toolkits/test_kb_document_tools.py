"""知识库文档工具权限分支单元测试（KB 级 share_config 语义）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import yuxi.agents.toolkits.kbs.tools as tools


def _runtime(uid="u1", visible_kbs=None):
    context = SimpleNamespace(
        uid=uid,
        thread_id="t-1",
        file_thread_id="t-1",
        _visible_knowledge_bases=visible_kbs or [{"kb_id": "kb-1", "name": "库"}],
    )
    return SimpleNamespace(context=context)


@pytest.fixture(autouse=True)
def _patch_visible(monkeypatch):
    async def fake_visible(runtime):
        context = getattr(runtime, "context", None)
        if context is None:
            return []
        return context._visible_knowledge_bases

    monkeypatch.setattr(tools, "_resolve_visible_knowledge_bases_for_query", fake_visible)


def _patch_kb_manage(monkeypatch, *, user=None, kb=None):
    """mock 用户仓库与知识库信息，走真实 resolve 逻辑。"""

    class FakeUser:
        pass

    fake_user = user or SimpleNamespace(uid="u1", role="user", department_id=1)

    class FakeUserRepo:
        async def get_by_uid(self, uid):
            return fake_user

    share_config = kb.get("share_config") if kb else None
    fake_kb = SimpleNamespace(created_by="someone-else", share_config=share_config)

    class FakeKB:
        async def get_database_info(self, kb_id):
            return fake_kb

    monkeypatch.setattr("yuxi.repositories.user_repository.UserRepository", lambda: FakeUserRepo())
    monkeypatch.setattr(tools, "_get_knowledge_base", lambda: FakeKB())


def _global_share(manage_user_uids=None):
    return {
        "version": 2,
        "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
        "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": manage_user_uids or []},
    }


def test_kb_write_error_missing_uid():
    import asyncio

    runtime = SimpleNamespace(context=SimpleNamespace())
    assert asyncio.run(tools._kb_write_error(runtime, "kb-1")) == "无法获取当前会话的用户信息"


def test_kb_write_error_employee_without_manage(monkeypatch):
    _patch_kb_manage(monkeypatch, kb={"share_config": _global_share([])})
    import asyncio

    assert asyncio.run(tools._kb_write_error(_runtime(), "kb-1")) == "需要知识库管理权限才能上传/删除文档"


def test_kb_write_error_employee_with_manage_passes(monkeypatch):
    _patch_kb_manage(monkeypatch, kb={"share_config": _global_share(["u1"])})
    import asyncio

    assert asyncio.run(tools._kb_write_error(_runtime(), "kb-1")) is None


def test_delete_kb_file_rejects_folder_target(monkeypatch):
    record = SimpleNamespace(file_id="fld-1", kb_id="kb-1", parent_id=None, is_folder=True)

    class FakeFileRepo:
        async def get_by_file_id(self, file_id):
            return record

    monkeypatch.setattr("yuxi.repositories.knowledge_file_repository.KnowledgeFileRepository", lambda: FakeFileRepo())

    import asyncio

    result = asyncio.run(tools.delete_kb_file.coroutine("kb-1", "fld-1", _runtime()))
    assert result == "无权删除目录"


def test_upload_kb_file_returns_error_for_missing_sandbox_file(monkeypatch):
    _patch_kb_manage(monkeypatch, kb={"share_config": _global_share(["u1"])})

    import asyncio

    result = asyncio.run(
        tools.upload_kb_file.coroutine("kb-1", "/home/gem/user-data/outputs/missing.pdf", "fld-a", _runtime())
    )
    assert "不存在" in result


def test_query_kb_passes_no_filtering_args(monkeypatch):
    captured = {}

    class FakeKB:
        async def retrieve(self, kb_id, query, **options):
            captured["kb_id"] = kb_id
            captured["options"] = options
            return {"kb_id": kb_id, "results": []}

    monkeypatch.setattr(tools, "_get_knowledge_base", lambda: FakeKB())

    import asyncio

    asyncio.run(tools.query_kb.coroutine("kb-1", "问题", None, _runtime()))
    assert captured["kb_id"] == "kb-1"
    assert "allowed_file_ids" not in captured["options"]
