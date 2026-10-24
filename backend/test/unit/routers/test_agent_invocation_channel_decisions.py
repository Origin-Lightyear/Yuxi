import pytest

from server.routers.agent_invocation_channel_router import decision_type_for_command


def test_approve_maps_to_approve():
    assert decision_type_for_command("approve", ()) == "approve"


def test_reject_maps_to_reject():
    assert decision_type_for_command("reject", ()) == "reject"


def test_other_commands_return_none():
    assert decision_type_for_command("state", ()) is None


def test_decision_command_rejects_args():
    with pytest.raises(ValueError):
        decision_type_for_command("reject", ("x",))
