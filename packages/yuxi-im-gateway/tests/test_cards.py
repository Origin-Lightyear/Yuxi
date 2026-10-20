from yuxi_im_gateway.adapters.cards import approval_card, parse_card_action
from yuxi_im_gateway.adapters.feishu import _feishu_card


def test_parse_card_action_round_trip():
    card = approval_card(thread_id="t1", run_id="r1", requester_id="ou_a", chat_id="c1")
    approve_button = card["actions"][0]
    decision, thread_id, run_id, requester_id, chat_id = parse_card_action(approve_button["value"])
    assert (decision, thread_id, run_id, requester_id, chat_id) == ("approve", "t1", "r1", "ou_a", "c1")


def test_parse_card_action_tolerates_string_value():
    decision, _thread_id, _run_id, _requester_id, _chat_id = parse_card_action(
        '{"decision": "reject", "thread_id": "t", "run_id": "r", "requester_id": "o", "chat_id": "c"}'
    )
    assert decision == "reject"


def test_feishu_card_puts_action_value_verbatim_into_button_behavior():
    platform_card = approval_card(thread_id="t1", run_id="r1", requester_id="ou_a", chat_id="c1")
    buttons = _feishu_card(platform_card)["body"]["elements"][1]["actions"]

    approve_value = buttons[0]["behaviors"][0]["value"]
    reject_value = buttons[1]["behaviors"][0]["value"]

    assert approve_value == platform_card["actions"][0]["value"]
    assert reject_value == platform_card["actions"][1]["value"]
    assert approve_value["chat_id"] == "c1"
