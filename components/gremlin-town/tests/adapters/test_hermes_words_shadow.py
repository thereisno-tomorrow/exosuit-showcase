"""Fixture-only keyed observation; never edits or refuses Nic's turn."""
import json
from pathlib import Path

from adapters.hermes import plugin


def observer(tmp_path, **kwargs):
    return plugin.WordsShadow(key=b"fixture-install-key-one-32-bytes!!", log_path=tmp_path / "shadow.jsonl",
                              owner_sender_id="fixture-nic", **kwargs)


def rows(shadow):
    return [json.loads(line) for line in shadow.log_path.read_text().splitlines()]


def test_hook_hashes_exact_supplied_reply_prefix_apart_without_filling(tmp_path):
    prefix = "fixture-reply-quote\n"
    shadow = observer(tmp_path, reply_quote_prefix=prefix)
    message = prefix + "Pay venue invoice"
    assert shadow.pre_llm_call(session_id="s", turn_id="t", user_message=message,
                               sender_id="fixture-nic", platform="telegram") is None
    payload = {"message": "Pay venue invoice", "said_by": "owner"}
    assert shadow.observe_work(payload, session_id="s", turn_id="t") is None
    turn, work = rows(shadow)
    assert turn["message_hash"] != turn["words_hash"]
    assert work["matches_words"] is True and work["matches_message"] is False
    assert work["owner_sender_mismatch"] is False
    assert payload == {"message": "Pay venue invoice", "said_by": "owner"}
    assert "Pay venue invoice" not in shadow.log_path.read_text()
    assert prefix not in shadow.log_path.read_text()


def test_hashes_cannot_be_compared_across_install_keys(tmp_path):
    one = observer(tmp_path)
    two = plugin.WordsShadow(key=b"fixture-install-key-two-32-bytes!!", log_path=tmp_path / "two.jsonl",
                             owner_sender_id="fixture-nic")
    for shadow in (one, two):
        shadow.pre_llm_call(user_message="Pay venue invoice")
    assert rows(one)[0]["message_hash"] != rows(two)[0]["message_hash"]


def test_claimed_owner_with_other_sender_or_parent_is_counted_only(tmp_path):
    shadow = observer(tmp_path)
    for sender, parent in (("other", None), ("fixture-nic", "parent")):
        shadow.pre_llm_call(session_id="s", turn_id="t", user_message="Pay venue invoice",
                            sender_id=sender, parent_session_id=parent)
        shadow.observe_work({"message": "Pay venue invoice", "said_by": "owner"}, session_id="s", turn_id="t")
    assert all(r["owner_sender_mismatch"] for r in rows(shadow) if r["phase"] == "work")


def test_hook_failure_logs_no_words_and_leaves_turn_untouched(tmp_path, monkeypatch, caplog):
    shadow = observer(tmp_path)
    def broken(entry):
        raise OSError("SECRET text must never be logged")
    monkeypatch.setattr(shadow, "_append", broken)
    assert shadow.pre_llm_call(user_message="SECRET owner words") is None
    assert shadow.observe_work({"message": "SECRET owner words"}) is None
    assert "words-shadow" in caplog.text and "SECRET" not in caplog.text
    assert not shadow.log_path.exists()


def test_sessions_never_borrow_another_turn_and_missing_observation_is_recorded(tmp_path):
    shadow = observer(tmp_path)
    shadow.pre_llm_call(session_id="one", turn_id="t", user_message="Private")
    shadow.observe_work({"message": "Private", "said_by": "owner"}, session_id="two", turn_id="t")
    assert rows(shadow)[-1]["observed"] is False
    assert "matches_words" not in rows(shadow)[-1]


def test_native_post_tool_hook_uses_turn_identity_and_ignores_other_tools(tmp_path):
    shadow = observer(tmp_path)
    shadow.pre_llm_call(session_id="s", turn_id="t", user_message="Private",
                        sender_id="fixture-nic", platform="discord")
    shadow.post_tool_call(tool_name="cronjob_manage", args={}, session_id="s", turn_id="t")
    shadow.post_tool_call(tool_name="bc_work", args={"message": "Private", "said_by": "owner"},
                          session_id="s", turn_id="t", result="fixture refusal")
    assert len(rows(shadow)) == 2
    assert rows(shadow)[-1]["observed"] and rows(shadow)[-1]["matches_message"]
    assert not rows(shadow)[-1]["owner_sender_mismatch"]


def test_register_uses_the_installed_hook_and_missing_observer_is_visible(tmp_path, caplog):
    from tests.adapters.test_hermes import LoaderContext
    from tests.adapters.test_cloud import Requester
    from adapters.hermes.hub_client import HubHTTPSClient, HubClientConfig
    ctx = LoaderContext()
    hooks = {}
    ctx.register_hook = lambda name, callback: hooks.update({name: callback})
    client = HubHTTPSClient(HubClientConfig("https://hub.test", "fixture-token"), client=Requester(), door=True)
    plugin.register(ctx, client=client)
    assert hooks["pre_llm_call"](user_message="private") is None
    assert "install-supplied observer missing" in caplog.text and "private" not in caplog.text
    shadow = observer(tmp_path)
    plugin.register(ctx, client=client, words_shadow=shadow)
    assert hooks["pre_llm_call"](user_message="private", sender_id="fixture-nic") is None
    assert rows(shadow)[0]["owner_sender"] is True
