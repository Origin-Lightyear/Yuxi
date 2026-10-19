from yuxi_im_gateway.adapters.cards import approval_card, parse_card_action


def test_parse_card_action_round_trip():
    card = approval_card(thread_id="t1", run_id="r1", requester_id="ou_a")
    approve_button = card["actions"][0]
    decision, thread_id, run_id, requester_id = parse_card_action(approve_button["value"])
    assert (decision, thread_id, run_id, requester_id) == ("approve", "t1", "r1", "ou_a")


def test_parse_card_action_tolerates_string_value():
    decision, thread_id, run_id, requester_id = parse_card_action(
        '{"decision": "reject", "thread_id": "t", "run_id": "r", "requester_id": "o"}'
    )
    assert decision == "reject"
