"""SaaS 员工 KB 权限依赖的角色判定单元测试。"""

from types import SimpleNamespace

from server.utils.employee_kb_permissions import is_employee


def test_is_employee_requires_local_user_role():
    assert is_employee(SimpleNamespace(uid="u1", role="user")) is True


def test_is_employee_excludes_admin_roles():
    assert is_employee(SimpleNamespace(uid="u1", role="admin")) is False
    assert is_employee(SimpleNamespace(uid="u1", role="superadmin")) is False


def test_is_employee_treats_missing_role_as_non_employee():
    # 生产 User.role 非空；缺字段的非模型对象不得进入员工权限路径
    assert is_employee(SimpleNamespace(uid="u1")) is False
