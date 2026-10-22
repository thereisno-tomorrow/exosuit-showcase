"""Synthetic authenticators only: real signatures, no browser or network."""
import base64
import hashlib
import json

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from gremlin_town.dashboard.app import create_app, require_owner, require_write_owner
from gremlin_town.dashboard.settings import DashboardSettings

ORIGIN = "https://dashboard.example.test"
RP = "dashboard.example.test"


def enc(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class Authenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = hashlib.sha256(self.key.public_key().public_numbers().x.to_bytes(32, "big")).digest()
        self.user = None

    def response(self, options, *, register=False, origin=ORIGIN, rp=RP, uv=True,
                 challenge=None, count=1, cross_origin=False):
        public = options["publicKey"]
        client = json.dumps({"type": "webauthn.create" if register else "webauthn.get",
                             "challenge": challenge or public["challenge"], "origin": origin,
                             "crossOrigin": cross_origin}, separators=(",", ":")).encode()
        auth_data = hashlib.sha256(rp.encode()).digest() + bytes([1 | (4 if uv else 0) | (64 if register else 0)]) + count.to_bytes(4, "big")
        response = {"clientDataJSON": enc(client)}
        if register:
            self.user = public["user"]["id"]
            numbers = self.key.public_key().public_numbers()
            cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
            auth_data += b"\x00" * 16 + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cbor2.dumps(cose)
            response["attestationObject"] = enc(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data}))
            response["transports"] = ["internal"]
        else:
            response.update(authenticatorData=enc(auth_data), userHandle=self.user,
                            signature=enc(self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))))
        return {"challenge_id": options["challenge_id"], "credential": {
            "id": enc(self.credential_id), "rawId": enc(self.credential_id),
            "type": "public-key", "response": response}}


@pytest.fixture
def system(tmp_path):
    secret = tmp_path / "session-secret"
    secret.write_bytes(b"fictional-test-session-secret!!!!")
    secret.chmod(0o600)
    settings = DashboardSettings(enabled=True, origin=ORIGIN, rp_id=RP, owner_id="fictional-owner",
                                 state_file=tmp_path / "auth.sqlite", session_secret_file=secret)
    clock = [1_800_000_000]
    router = APIRouter()

    @router.get("/private")
    def private(owner=Depends(require_owner)):
        return {"owner": owner.owner_id}

    @router.post("/private")
    def mutate(owner=Depends(require_write_owner)):
        return {"owner": owner.owner_id}

    @router.get("/omitted-dependency")
    def omitted_dependency():
        return {"private": True}

    app = create_app(settings, clock=lambda: clock[0], router=router)
    client = TestClient(app, base_url=ORIGIN)
    return app, client, clock, settings


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/auth/context").json()["csrf"]}


def enrol(app, client, token=None, device=None):
    token = token or app.state.dashboard_auth.issue_enrollment()
    device = device or Authenticator()
    protection = headers(client)
    options = client.post("/auth/enrol/options", json={"token": token}, headers=protection)
    assert options.status_code == 200
    response = client.post("/auth/enrol/verify", json=device.response(options.json(), register=True), headers=protection)
    assert response.status_code == 200
    return device, token, options.json()


def sign_in(client, device, **changes):
    protection = headers(client)
    options = client.post("/auth/sign-in/options", json={}, headers=protection).json()
    body = device.response(options, count=2, **changes)
    return client.post("/auth/sign-in/verify", json=body, headers=protection), body, protection


def test_nothing_private_before_sign_in(system):
    app, client, _, _ = system
    assert client.get("/private").status_code == 401
    assert client.get("/omitted-dependency").status_code == 401
    assert client.get("/auth/session").status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert "fictional-owner" not in client.get("/login").text


def test_real_registration_and_assertion_create_secure_session(system):
    app, client, _, _ = system
    device, _, _ = enrol(app, client)
    # Enrolment creates a verified owner session too.
    assert client.get("/private").json() == {"owner": "fictional-owner"}
    client.cookies.clear()
    response, _, _ = sign_in(client, device)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "__Host-dashboard=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie
    assert client.get("/private").status_code == 200
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", [{"origin": "https://other.example.test"}, {"rp": "example.test"},
                                    {"uv": False}, {"challenge": "wrong"}, {"cross_origin": True}])
def test_webauthn_binding_failures(system, change):
    app, client, _, _ = system
    device, _, _ = enrol(app, client)
    client.cookies.clear()
    response, body, protection = sign_in(client, device, **change)
    assert response.status_code == 401
    assert client.post("/auth/sign-in/verify", json=body, headers=protection).status_code == 401
    assert client.get("/private").status_code == 401


def test_signature_tamper_and_counter_replay(system):
    app, client, _, _ = system
    device, _, _ = enrol(app, client)
    protection = headers(client)
    options = client.post("/auth/sign-in/options", json={}, headers=protection).json()
    body = device.response(options, count=2)
    body["credential"]["response"]["signature"] = enc(b"invalid")
    assert client.post("/auth/sign-in/verify", json=body, headers=protection).status_code == 401
    assert sign_in(client, device)[0].status_code == 200
    assert sign_in(client, device)[0].status_code == 401


def test_challenge_is_bound_to_browser_single_use_and_expires(system):
    app, client, clock, _ = system
    device, _, _ = enrol(app, client)
    response, body, protection = sign_in(client, device)
    assert response.status_code == 200
    assert client.post("/auth/sign-in/verify", json=body, headers=protection).status_code == 401
    options = client.post("/auth/sign-in/options", json={}, headers=protection).json()
    second = TestClient(app, base_url=ORIGIN)
    assert second.post("/auth/sign-in/verify", json=device.response(options, count=3), headers=headers(second)).status_code == 401
    options = client.post("/auth/sign-in/options", json={}, headers=protection).json()
    clock[0] += 301
    assert client.post("/auth/sign-in/verify", json=device.response(options, count=3), headers=protection).status_code in (401, 403)


def test_enrollment_expires_and_is_one_time(system):
    app, client, clock, _ = system
    _, token, _ = enrol(app, client)
    assert client.post("/auth/enrol/options", json={"token": token}, headers=headers(client)).status_code == 401
    expired = app.state.dashboard_auth.issue_enrollment()
    clock[0] += 901
    assert client.post("/auth/enrol/options", json={"token": expired}, headers=headers(client)).status_code == 401


def test_multiple_passkeys_one_owner_and_revoke_all(system):
    app, client, _, _ = system
    first, _, _ = enrol(app, client)
    second, _, _ = enrol(app, client)
    assert first.user == second.user
    assert sign_in(client, first)[0].status_code == 200
    app.state.dashboard_auth.revoke_all()
    assert client.get("/private").status_code == 401
    assert sign_in(client, second)[0].status_code == 401


def test_writes_and_logout_require_origin_and_session_csrf(system):
    app, client, _, _ = system
    enrol(app, client)
    protection = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/auth/session").json()["csrf"]}
    assert client.post("/private", headers=protection).status_code == 200
    for bad in ({}, {"Origin": ORIGIN}, {**protection, "Origin": "https://other.example.test"},
                {**protection, "X-CSRF-Token": "wrong"}):
        assert client.post("/private", headers=bad).status_code == 403
        assert client.post("/auth/logout", headers=bad).status_code == 403
    assert client.post("/auth/logout", headers=protection).status_code == 200
    assert client.get("/private").status_code == 401


def test_session_tamper_expiry_and_server_revocation(system):
    app, client, clock, _ = system
    enrol(app, client)
    cookie = client.cookies.get("__Host-dashboard")
    client.cookies.clear()
    client.cookies.set("__Host-dashboard", cookie + "x")
    assert client.get("/private").status_code == 401
    client.cookies.clear()
    client.cookies.set("__Host-dashboard", cookie)
    clock[0] += 43_201
    assert client.get("/private").status_code == 401


def test_no_cross_origin_auth_or_unbounded_body(system):
    app, client, _, _ = system
    assert client.post("/auth/sign-in/options", json={}).status_code == 403
    assert client.post("/auth/sign-in/options", content=b"x" * 65_537, headers=headers(client)).status_code == 413
    assert client.get("/login", headers={"Host": "other.example.test"}).status_code == 400


def test_off_means_no_routes_or_files(tmp_path):
    app = create_app(DashboardSettings())
    client = TestClient(app)
    for path in ("/", "/login", "/auth/context", "/auth/session"):
        assert client.get(path).status_code == 404
    assert list(tmp_path.iterdir()) == []


def test_config_is_strict_and_default_off(tmp_path):
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    from gremlin_town.dashboard.settings import ALLOWED_KEYS
    defaults = DashboardSettings.from_table(None, base=tmp_path)
    assert not any((defaults.enabled, defaults.reads, defaults.writes, defaults.ask_diablo))
    assert RUNTIME_CONFIG_SCHEMA["dashboard"] == ALLOWED_KEYS
    for table in ({"enabled": "false"}, {"enabled": True}, {"host": "0.0.0.0"},
                  {"origin": "http://example.test"}, {"unknown": True}, {"writes": True},
                  {"origin": ORIGIN + "/path"}, {"origin": ORIGIN, "rp_id": "example.test"}):
        with pytest.raises(ValueError):
            DashboardSettings.from_table(table, base=tmp_path)


@pytest.mark.parametrize("change", [{"origin": "https://other.example.test"}, {"rp": "example.test"},
                                    {"uv": False}, {"challenge": "wrong"}, {"cross_origin": True}])
def test_registration_checks_same_bindings_and_does_not_consume_link_on_failure(system, change):
    app, client, _, _ = system
    token = app.state.dashboard_auth.issue_enrollment()
    protection = headers(client)
    options = client.post("/auth/enrol/options", json={"token": token}, headers=protection).json()
    device = Authenticator()
    response = client.post("/auth/enrol/verify", json=device.response(options, register=True, **change), headers=protection)
    assert response.status_code == 401
    assert client.get("/private").status_code == 401
    enrol(app, client, token=token, device=device)


def test_wrong_user_handle_unknown_key_and_client_supplied_owner(system):
    app, client, _, _ = system
    device, _, _ = enrol(app, client)
    client.cookies.clear()
    device.user = enc(b"unrelated-user")
    assert sign_in(client, device)[0].status_code == 401
    assert sign_in(client, Authenticator())[0].status_code == 401
    device.user = enc(app.state.dashboard_auth.store.user_handle())
    protection = headers(client)
    options = client.post("/auth/sign-in/options", json={}, headers=protection).json()
    body = device.response(options, count=2)
    body["owner_id"] = "injected-owner"
    assert client.post("/auth/sign-in/verify", json=body, headers=protection).status_code == 200
    assert client.get("/private").json() == {"owner": "fictional-owner"}


def test_concurrent_verify_consumes_challenge_once(system):
    from concurrent.futures import ThreadPoolExecutor
    from gremlin_town.dashboard.store import AuthRejected
    app, client, _, _ = system
    device, _, _ = enrol(app, client)
    auth = app.state.dashboard_auth
    browser, _ = auth.browser_context(None)
    options = auth.options("sign-in", browser)
    body = device.response(options, count=2)
    def verify():
        try:
            auth.verify("sign-in", browser, body)
            return True
        except AuthRejected:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: verify(), range(2))) == [False, True]


def test_one_enrollment_link_cannot_register_two_keys_concurrently(system):
    from concurrent.futures import ThreadPoolExecutor
    from gremlin_town.dashboard.store import AuthRejected
    app, _, _, _ = system
    auth = app.state.dashboard_auth
    token = auth.issue_enrollment()
    browser, _ = auth.browser_context(None)
    bodies = [Authenticator().response(auth.options("enrol", browser, token=token), register=True) for _ in range(2)]
    def verify(body):
        try:
            auth.verify("enrol", browser, body)
            return True
        except AuthRejected:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(verify, bodies)) == [False, True]
    assert len(auth.store.credential_ids()) == 1


def test_rate_limit_and_redacted_audit(system):
    app, client, _, _ = system
    auth = app.state.dashboard_auth
    for _ in range(120):
        assert client.get("/auth/context").status_code == 200
    assert client.get("/auth/context").status_code == 429
    with auth.store.connect() as db:
        assert db.execute("SELECT count(*) FROM browsers").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM challenges").fetchone()[0] == 0


def test_owner_can_sign_out_when_anonymous_sign_in_limit_is_exhausted(system):
    app, client, clock, _ = system
    enrol(app, client)
    session_cookie = client.cookies.get("__Host-dashboard")
    protection = {"Origin": ORIGIN, "X-CSRF-Token": client.get("/auth/session").json()["csrf"]}
    clock[0] += 60
    anonymous = TestClient(app, base_url=ORIGIN)
    for _ in range(120):
        assert anonymous.get("/auth/context").status_code == 200
    assert anonymous.get("/auth/context").status_code == 429
    # Flooding cannot prevent logout, and does not weaken its normal checks.
    assert client.post("/auth/logout", headers={**protection, "X-CSRF-Token": "wrong"}).status_code == 403
    assert client.get("/private").status_code == 200
    assert anonymous.post("/auth/logout", headers=protection).status_code == 401
    assert client.post("/auth/logout", headers=protection).status_code == 200
    client.cookies.set("__Host-dashboard", session_cookie)
    assert client.get("/private").status_code == 401


@pytest.mark.parametrize("host", [2130706433, True, None, b"127.0.0.1", [], "", " "])
def test_listener_host_rejects_values_that_are_not_nonempty_text(tmp_path, host):
    with pytest.raises(ValueError, match="host"):
        DashboardSettings.from_table({"host": host}, base=tmp_path)


def test_signed_in_does_not_refresh_expired_session_and_restart_preserves_revocation(system):
    app, client, clock, settings = system
    enrol(app, client)
    session_cookie = client.cookies.get("__Host-dashboard")
    restarted = TestClient(create_app(settings, clock=lambda: clock[0]), base_url=ORIGIN)
    restarted.cookies.set("__Host-dashboard", session_cookie)
    assert restarted.get("/auth/session").status_code == 200
    app.state.dashboard_auth.revoke_all()
    assert restarted.get("/auth/session").status_code == 401


def test_malformed_payloads_never_reflect_input_or_traceback(system):
    _, client, _, _ = system
    protection = headers(client)
    for payload in ([], "private-content-do-not-echo", {"credential": {"id": "private-content-do-not-echo"}},
                    {"challenge_id": 42}, {"challenge_id": None}):
        result = client.post("/auth/sign-in/verify", json=payload, headers=protection)
        assert result.status_code in (400, 401)
        assert "private-content-do-not-echo" not in result.text
        assert "Traceback" not in result.text


def test_runtime_reads_disabled_dashboard_table_without_starting_listener(tmp_path):
    from tests.integration.test_runtime_flow import _write_runtime_config
    from gremlin_town.runtime import build_runtime
    from gremlin_town.transport import FakeTransport
    path, _ = _write_runtime_config(tmp_path)
    with path.open("a") as target:
        target.write('\n[dashboard]\nenabled=false\nreads=false\nwrites=false\nask_diablo=false\n')
    runtime = build_runtime(path, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.dashboard.enabled is False
        assert TestClient(runtime.app).get("/auth/context").status_code == 404
    finally:
        runtime.close()
