import json
import logging

import httpx
import pytest

from gremlin_town.review_card.client import DiscordClient, DiscordUnavailable, gateway_address, read_credential


def client(handler, **options):
    return DiscordClient("fictional.bot.credential", http=httpx.Client(transport=httpx.MockTransport(handler)), **options)


def test_post_uses_nonce_mentions_and_callback_hides_credential(caplog):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(204 if "/callback" in request.url.path else 200, json=None if "/callback" in request.url.path else {"id": "108"})
    api = client(handler)
    with caplog.at_level(logging.INFO, logger="httpx"):
        api.post("104", {"content": "fictional text", "allowed_mentions": {"parse": ["everyone"]}}, nonce="first")
        api.callback({"id": "201", "token": "fictional-interaction-secret"}, {"type": 6})
    assert json.loads(seen[0].content)["enforce_nonce"] is True
    assert json.loads(seen[0].content)["allowed_mentions"] == {"parse": []}
    assert seen[0].headers["authorization"].startswith("Bot ")
    assert "authorization" not in seen[1].headers
    assert "fictional-interaction-secret" not in caplog.text
    assert "fictional.bot.credential" not in caplog.text


def test_rate_limit_does_not_blindly_retry_and_callbacks_do_not_wait():
    now = [1.0]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(204) if "/callback" in request.url.path else httpx.Response(429, json={"retry_after": 12})
    api = client(handler, clock=lambda: now[0])
    for _ in range(2):
        with pytest.raises(DiscordUnavailable) as error:
            api.post("104", {"content": "fictional"}, nonce="first")
        assert error.value.retry_after == 12
    assert len(calls) == 1
    api.callback({"id": "201", "token": "fictional-interaction"}, {"type": 6})
    assert len(calls) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404, 500])
def test_provider_errors_never_reflect_private_response(code):
    api = client(lambda request: httpx.Response(code, text="fictional-private-text-and-token"))
    with pytest.raises(DiscordUnavailable) as error:
        api.post("104", {"content": "fictional"}, nonce="first")
    assert "fictional-private" not in str(error.value)
    assert error.value.uncertain is (code >= 500)


def test_lost_create_reply_is_uncertain_and_not_retried():
    seen = []
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("fictional-private-url-and-token")
    api = client(handler)
    with pytest.raises(DiscordUnavailable) as error:
        api.post("104", {"content": "fictional"}, nonce="first")
    assert len(seen) == 1 and error.value.uncertain
    assert "fictional-private" not in str(error.value)


def test_thread_recovery_and_message_reconciliation_use_own_bot():
    seen = []
    def handler(request):
        seen.append(request)
        if "limit=" in str(request.url):
            return httpx.Response(200, json=[{"id": "108", "nonce": "first", "author": {"id": "102"}},
                                            {"id": "109", "nonce": "first", "author": {"id": "999"}}])
        return httpx.Response(200, json={"id": "108", "channel_id": "104", "thread": {"id": "107"}})
    api = client(handler)
    assert api.find_nonce("104", "first", bot_user_id="102")["id"] == "108"
    assert api.thread("104", "108") == "107"
    assert all(row.method == "GET" for row in seen)


@pytest.mark.parametrize("address", ["ws://gateway.discord.gg", "wss://discord.gg.evil.test", "wss://evil.test", "wss://user:pass@gateway.discord.gg", "wss://gateway.discord.gg/path"])
def test_gateway_never_sends_credential_to_untrusted_address(address):
    with pytest.raises(DiscordUnavailable):
        gateway_address(address)


def test_credential_read_is_private_and_failures_sanitized(tmp_path):
    path = tmp_path / "credential"
    path.write_text("fictional.bot.credential")
    path.chmod(0o600)
    assert read_credential(path) == "fictional.bot.credential"
    path.write_text("bad\ncredential")
    with pytest.raises(DiscordUnavailable) as error:
        read_credential(path)
    assert str(path) not in str(error.value)


def test_bot_identity_checks_application_and_followups_do_not_log_tokens(caplog):
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json={'id':'102','bot':{'id':'103','bot':True}})
    api=client(handler)
    try:
        assert api.bot_user('102')=='103'
        with pytest.raises(DiscordUnavailable,match='different application'):
            api.bot_user('999')
        with caplog.at_level(logging.INFO,logger='httpx'):
            api.followup({'token':'fictional-private-token'},'102','A safe refusal')
        assert 'authorization' not in seen[-1].headers
        assert 'fictional-private-token' not in caplog.text
    finally:
        api.close()
