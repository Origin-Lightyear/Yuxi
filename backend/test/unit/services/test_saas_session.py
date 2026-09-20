from yuxi.services.saas_session import SaasAgentSession


def test_saas_agent_session_repr_hides_token():
    session = SaasAgentSession(
        token="sensitive-session-token",
        expires_at="1789842085",
        tenant_id=1,
        employee_id=1,
        mcp_instance_id="runtime-1",
    )

    assert "sensitive-session-token" not in repr(session)
