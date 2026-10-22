"""GitHub App installation tokens: minting, caching, narrowing and use by both GitHub clients."""

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from brain_core.github_storage import GitHubTransportError, HTTPResponse
from gremlin_town.github_app import (
    AppTokenHTTPTransport,
    GitHubAppError,
    GitHubAppTokenProvider,
    load_private_key,
)
from gremlin_town.transport.errors import ReconciliationRequiredError, TransportError
from gremlin_town.transport.github import GitHubConfig, GitHubTransport
from test_github_transport import ROOT, Requester, envelope, issue, response


NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
MINT_URL = "https://api.github.com/app/installations/22/access_tokens"


def _pem(key, *, encryption=None) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        encryption or serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class MintClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, "unexpected token request"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def minted(token="ghs_first", *, expires=NOW + timedelta(hours=1), permissions=None, repos=("owner/repo",)):
    return httpx.Response(201, json={
        "token": token,
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "permissions": permissions or {"issues": "write", "metadata": "read"},
        "repositories": [{"full_name": name} for name in repos],
    })


def provider(rsa_key, *responses, clock=None, repository="owner/repo", permissions=None):
    client = MintClient(*responses)
    made = GitHubAppTokenProvider(
        app_id=11, installation_id=22, private_key=_pem(rsa_key), repository=repository,
        permissions=permissions or {"issues": "write"}, client=client, clock=clock or Clock(),
    )
    return made, client


def test_mints_a_narrowed_token_with_a_signed_app_assertion(rsa_key):
    made, client = provider(rsa_key, minted())
    assert made.token() == "ghs_first"
    method, url, kwargs = client.calls[0]
    assert (method, url) == ("POST", MINT_URL)
    assert kwargs["json"] == {"repositories": ["repo"], "permissions": {"issues": "write"}}
    assert kwargs["follow_redirects"] is False
    assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assertion = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, rsa_key.public_key(), algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "11"
    assert claims["iat"] == int((NOW - timedelta(seconds=60)).timestamp())
    assert claims["exp"] - claims["iat"] == 540


def test_token_is_cached_until_the_refresh_margin(rsa_key):
    clock = Clock()
    made, client = provider(rsa_key, minted(), minted("ghs_second", expires=NOW + timedelta(hours=2)), clock=clock)
    assert made.token() == "ghs_first"
    clock.now = NOW + timedelta(minutes=49)
    assert made.token() == "ghs_first" and len(client.calls) == 1
    clock.now = NOW + timedelta(minutes=50)
    assert made.token() == "ghs_second" and len(client.calls) == 2


def test_invalidate_drops_only_the_rejected_token(rsa_key):
    made, client = provider(rsa_key, minted(), minted("ghs_second"))
    assert made.token() == "ghs_first"
    made.invalidate("some-other-token")
    assert made.token() == "ghs_first" and len(client.calls) == 1
    made.invalidate("ghs_first")
    assert made.token() == "ghs_second" and len(client.calls) == 2


@pytest.mark.parametrize(("status", "code", "retryable"), [
    (401, "TOKEN_REFUSED", False), (403, "TOKEN_REFUSED", False), (404, "TOKEN_REFUSED", False),
    (422, "TOKEN_REFUSED", False), (429, "TOKEN_UNAVAILABLE", True), (503, "TOKEN_UNAVAILABLE", True),
])
def test_refusals_and_outages_are_classified_without_secrets(rsa_key, status, code, retryable):
    made, client = provider(rsa_key, httpx.Response(status, json={"message": "Bad credentials"}))
    with pytest.raises(GitHubAppError) as caught:
        made.token()
    assert (caught.value.code, caught.value.status, caught.value.retryable) == (code, status, retryable)
    assertion = client.calls[0][2]["headers"]["Authorization"].removeprefix("Bearer ")
    assert assertion not in str(caught.value) and "PRIVATE KEY" not in str(caught.value)


def test_network_failure_is_retryable_and_leaves_nothing_cached(rsa_key):
    made, client = provider(rsa_key, httpx.ConnectError("refused"), minted())
    with pytest.raises(GitHubAppError) as caught:
        made.token()
    assert caught.value.code == "TOKEN_UNAVAILABLE" and caught.value.retryable is True
    assert made.token() == "ghs_first" and len(client.calls) == 2


@pytest.mark.parametrize("reply", [
    minted(permissions={"issues": "write", "contents": "write", "metadata": "read"}),
    minted(permissions={"issues": "read", "metadata": "read"}),
    minted(permissions={"issues": "write", "metadata": "write"}),
    minted(repos=("owner/other",)),
    minted(repos=("owner/repo", "owner/other")),
    minted(expires=NOW + timedelta(minutes=5)),
    httpx.Response(201, json={"expires_at": "2026-09-25T13:00:00Z", "permissions": {"issues": "write"},
                              "repositories": [{"full_name": "owner/repo"}]}),
    httpx.Response(201, json={"token": "ghs_naive", "expires_at": "2026-09-25T13:00:00",
                              "permissions": {"issues": "write"}, "repositories": [{"full_name": "owner/repo"}]}),
    httpx.Response(201, content=b"not json"),
    httpx.Response(201, json=["not", "an", "object"]),
])
def test_malformed_or_broader_grants_are_refused_and_not_cached(rsa_key, reply):
    made, client = provider(rsa_key, reply, minted())
    with pytest.raises(GitHubAppError) as caught:
        made.token()
    assert caught.value.code == "TOKEN_MALFORMED"
    assert made.token() == "ghs_first" and len(client.calls) == 2


def test_metadata_may_be_omitted_from_the_grant(rsa_key):
    made, _ = provider(rsa_key, minted(permissions={"issues": "write"}))
    assert made.token() == "ghs_first"


@pytest.mark.parametrize("changes", [
    {"app_id": 0}, {"installation_id": True}, {"repository": "no-owner"},
    {"permissions": {}}, {"permissions": {"issues": "admin"}}, {"permissions": {"metadata": "read"}},
    {"private_key": "not a key"}, {"api_base": "http://api.github.com"},
    {"refresh_margin": timedelta(minutes=55)},
])
def test_constructor_refuses_unsafe_settings(rsa_key, changes):
    settings = {"app_id": 11, "installation_id": 22, "private_key": _pem(rsa_key), "repository": "owner/repo",
                "permissions": {"issues": "write"}, "client": MintClient(), **changes}
    with pytest.raises(ValueError):
        GitHubAppTokenProvider(**settings)


def test_private_key_loading_accepts_only_unencrypted_rsa(tmp_path, rsa_key):
    good = tmp_path / "app.pem"
    good.write_text(_pem(rsa_key), encoding="ascii")
    assert load_private_key(good) == _pem(rsa_key)
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a key", encoding="ascii")
    elliptic = tmp_path / "ec.pem"
    elliptic.write_text(_pem(ec.generate_private_key(ec.SECP256R1())), encoding="ascii")
    encrypted = tmp_path / "encrypted.pem"
    encrypted.write_text(_pem(rsa_key, encryption=serialization.BestAvailableEncryption(b"pw")), encoding="ascii")
    for path in (tmp_path / "missing.pem", garbage, elliptic, encrypted):
        with pytest.raises(GitHubAppError) as caught:
            load_private_key(path)
        assert caught.value.code == "CONFIG_ERROR" and "PRIVATE KEY-----" not in str(caught.value)


class Inner:
    """Brain Core HTTPTransport double that records the Authorization it receives."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.seen = []

    def request(self, method, url, *, headers, body):
        self.seen.append(dict(headers))
        return HTTPResponse(self.statuses.pop(0), {}, b"{}")


def test_brain_transport_replaces_the_placeholder_with_a_fresh_token(rsa_key):
    made, _ = provider(rsa_key, minted(permissions={"contents": "write"}), permissions={"contents": "write"})
    inner = Inner(200)
    headers = {"Authorization": "Bearer placeholder", "Accept": "application/vnd.github+json"}
    assert AppTokenHTTPTransport(made, inner).request("GET", "https://api.github.com/x", headers=headers,
                                                      body=None).status == 200
    assert inner.seen == [{"Authorization": "Bearer ghs_first", "Accept": "application/vnd.github+json"}]
    assert headers["Authorization"] == "Bearer placeholder"


def test_brain_transport_retries_once_after_a_rejected_token(rsa_key):
    grants = {"permissions": {"contents": "write"}}
    made, client = provider(rsa_key, minted(**grants), minted("ghs_second", **grants), minted("ghs_third", **grants),
                            permissions={"contents": "write"})
    inner = Inner(401, 201)
    assert AppTokenHTTPTransport(made, inner).request("POST", "https://api.github.com/x", headers={},
                                                      body=b"{}").status == 201
    assert [seen["Authorization"] for seen in inner.seen] == ["Bearer ghs_first", "Bearer ghs_second"]
    rejected = Inner(401, 401)
    assert AppTokenHTTPTransport(made, rejected).request("GET", "https://api.github.com/x", headers={},
                                                         body=None).status == 401
    assert len(rejected.seen) == 2 and len(client.calls) == 3


def test_brain_transport_reports_an_unavailable_token_without_sending(rsa_key):
    made, _ = provider(rsa_key, httpx.Response(401, json={}), permissions={"contents": "write"})
    inner = Inner()
    with pytest.raises(GitHubTransportError) as caught:
        AppTokenHTTPTransport(made, inner).request("GET", "https://api.github.com/x", headers={}, body=None)
    assert caught.value.code == "github_transport_unavailable" and inner.seen == []


def gt_transport(rsa_key, requester, *mints, writer=42, legacy=()):
    made, client = provider(rsa_key, *mints, repository="test-owner/mailbox")
    config = GitHubConfig("test-owner", "mailbox", "", writer, token_provider=made, legacy_writer_user_ids=legacy)
    return GitHubTransport(config, client=requester, clock=lambda: NOW), client


def test_conversation_transport_sends_with_the_app_token(rsa_key):
    requester = Requester(response(issue(), 201))
    transport, _ = gt_transport(rsa_key, requester, minted(repos=("test-owner/mailbox",)))
    assert transport.send(envelope()).message_id == ROOT
    assert requester.calls[0][2]["headers"]["Authorization"] == "Bearer ghs_first"


def test_conversation_transport_retries_one_rejected_token_without_duplicating(rsa_key):
    requester = Requester(response({"message": "Bad credentials"}, 401), response(issue(), 201))
    repos = {"repos": ("test-owner/mailbox",)}
    transport, _ = gt_transport(rsa_key, requester, minted(**repos), minted("ghs_second", **repos))
    assert transport.send(envelope()).message_id == ROOT
    assert [call[2]["headers"]["Authorization"] for call in requester.calls] == ["Bearer ghs_first",
                                                                                  "Bearer ghs_second"]
    twice = Requester(response({"message": "Bad credentials"}, 401), response({"message": "Bad credentials"}, 401))
    transport, _ = gt_transport(rsa_key, twice, minted(**repos), minted("ghs_second", **repos))
    with pytest.raises(TransportError) as caught:
        transport.send(envelope())
    assert caught.value.code == "AUTHENTICATION_FAILED" and len(twice.calls) == 2


@pytest.mark.parametrize(("mint", "code", "retryable"), [
    (httpx.Response(401, json={}), "AUTHENTICATION_FAILED", False),
    (httpx.ConnectError("refused"), "PROVIDER_UNAVAILABLE", True),
])
def test_conversation_transport_token_failure_is_never_an_uncertain_send(rsa_key, mint, code, retryable):
    requester = Requester()
    transport, _ = gt_transport(rsa_key, requester, mint)
    with pytest.raises(TransportError) as caught:
        transport.send(envelope())
    assert (caught.value.code, caught.value.retryable, caught.value.accepted) == (code, retryable, False)
    assert requester.calls == []


def test_reconcile_recognises_legacy_writers_but_send_does_not(rsa_key):
    repos = {"repos": ("test-owner/mailbox",)}
    found, _ = gt_transport(rsa_key, Requester(response([issue(user=7)])), minted(**repos), legacy=(7,))
    assert found.reconcile(envelope()).message_id == ROOT
    unknown, _ = gt_transport(rsa_key, Requester(response([issue(user=7)])), minted(**repos))
    with pytest.raises(ReconciliationRequiredError):
        unknown.reconcile(envelope())
    sent_as_legacy, _ = gt_transport(rsa_key, Requester(response(issue(user=7), 201)), minted(**repos), legacy=(7,))
    with pytest.raises(TransportError) as caught:
        sent_as_legacy.send(envelope())
    assert caught.value.code == "MALFORMED_RESPONSE" and caught.value.accepted is True


def test_conversation_config_needs_exactly_one_credential(rsa_key):
    made, _ = provider(rsa_key, repository="test-owner/mailbox")
    elsewhere, _ = provider(rsa_key, repository="test-owner/other")
    for config in (
        GitHubConfig("test-owner", "mailbox", "token", 42, token_provider=made),
        GitHubConfig("test-owner", "mailbox", "", 42),
        GitHubConfig("test-owner", "mailbox", "", 42, token_provider=elsewhere),
        GitHubConfig("test-owner", "mailbox", "", 42, token_provider=made, legacy_writer_user_ids=(42,)),
        GitHubConfig("test-owner", "mailbox", "", 42, token_provider=made, legacy_writer_user_ids=(0,)),
    ):
        with pytest.raises(ValueError):
            GitHubTransport(config, client=Requester())
