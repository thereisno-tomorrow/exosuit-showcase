from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
import httpx

from gremlin_town.contracts import (
    Actor,
    CreateRequest,
    MessageKind,
    OutboundEnvelope,
    Packet,
    RequestMode,
)
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import (
    AgentMailConfig,
    AgentMailTransport,
    FakeTransport,
    ProviderTransportError,
)
from tests.integration.test_runtime_flow import _headers, _packet, _write_runtime_config


class RateLimitedOnce(FakeTransport):
    def __init__(self, retry_after: float) -> None:
        super().__init__()
        self.retry_after = retry_after
        self.attempt_times: list[float] = []

    def send(self, envelope: OutboundEnvelope):
        self.attempt_times.append(time.monotonic())
        if len(self.attempt_times) == 1:
            raise ProviderTransportError(
                "RATE_LIMITED",
                "provider returned HTTP 429",
                retryable=True,
                retry_after=self.retry_after,
                status=429,
            )
        return super().send(envelope)


class _JSONResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def json(self) -> dict[str, list[object]]:
        return {"messages": []}


class TimeoutPostThenEmptyDiscovery:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.retry_keys: list[str] = []

    def request(self, method: str, url: str, **kwargs):
        self.methods.append(method)
        if method == "POST":
            self.retry_keys.append(kwargs["headers"]["Idempotency-Key"])
            raise httpx.ReadTimeout("provider response was lost", request=httpx.Request(method, url))
        return _JSONResponse()


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for worker state")


def test_actual_runtime_worker_honors_retry_after_before_resend(tmp_path: Path) -> None:
    config, tokens = _write_runtime_config(tmp_path)
    text = config.read_text(encoding="utf-8").replace(
        "outbound_dispatch_allowed = false",
        "outbound_dispatch_allowed = true\n[worker]\npoll_seconds = 0.05",
    )
    config.write_text(text, encoding="utf-8")
    provider = RateLimitedOnce(retry_after=0.4)
    runtime = build_runtime(config, provider=provider, start_worker=True)

    with TestClient(runtime.app) as client:
        response = client.post(
            "/v1/requests",
            headers=_headers(tokens["alice"]),
            json={
                "client_operation_id": "worker-rate-limit-1",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "bob",
                "packet": _packet("request", "Honor the provider retry boundary."),
            },
        )
        assert response.status_code == 200
        operation_id = response.json()["operation_id"]
        _wait_until(lambda: len(provider.attempt_times) == 1)
        first_attempt = provider.attempt_times[0]
        time.sleep(0.2)
        assert provider.attempt_times == [first_attempt]
        operation = runtime.store.operation(operation_id)
        assert operation.operation_state.value == "queued"
        assert operation.transport_error_code == "RATE_LIMITED"
        assert operation.next_attempt_at is not None

        _wait_until(lambda: len(provider.attempt_times) == 2)
        assert provider.attempt_times[1] - first_attempt >= 0.38
        _wait_until(
            lambda: runtime.store.operation(operation_id).operation_state.value == "accepted"
        )


def test_actual_worker_reconciles_then_same_key_retries_without_resetting_risk_window(
    tmp_path: Path,
) -> None:
    config, tokens = _write_runtime_config(tmp_path)
    text = config.read_text(encoding="utf-8").replace(
        "outbound_dispatch_allowed = false",
        "outbound_dispatch_allowed = true",
    )
    config.write_text(text, encoding="utf-8")
    clock_value = [datetime(2026, 9, 21, 12, 0, tzinfo=UTC)]
    requester = TimeoutPostThenEmptyDiscovery()
    provider = AgentMailTransport(
        AgentMailConfig(
            inbox_id="inbox-test",
            api_key="provider-test-key",
            base_url="https://agentmail.example.test/v0/",
        ),
        client=requester,
        clock=lambda: clock_value[0],
    )
    runtime = build_runtime(config, provider=provider, start_worker=False)
    runtime.worker.now = lambda: clock_value[0]
    runtime.store.now = lambda: clock_value[0]

    with TestClient(runtime.app) as client:
        response = client.post(
            "/v1/requests",
            headers=_headers(tokens["alice"]),
            json={
                "client_operation_id": "worker-ambiguous-post-1",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "bob",
                "packet": _packet("request", "Never blind-resend an ambiguous POST."),
            },
        )
        assert response.status_code == 200
        operation_id = response.json()["operation_id"]

        runtime.worker.dispatch_once()
        uncertain = runtime.store.operation(operation_id)
        assert uncertain.operation_state.value == "uncertain"
        assert uncertain.uncertain_since == clock_value[0]
        assert uncertain.transport_error_code == "TIMEOUT"
        assert requester.methods == ["POST"]

        runtime.worker.reconcile_once()
        reconciled = runtime.store.operation(operation_id)
        assert reconciled.operation_state.value == "queued"
        assert reconciled.uncertain_since == clock_value[0]
        assert reconciled.recovery_required is False
        assert requester.methods == ["POST", "GET"]

        clock_value[0] += timedelta(seconds=2)
        runtime.worker.dispatch_once()
        retried = runtime.store.operation(operation_id)
        assert retried.operation_state.value == "uncertain"
        assert retried.uncertain_since == datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
        assert requester.retry_keys[0] == requester.retry_keys[1]

        clock_value[0] += timedelta(hours=25)
        runtime.worker.reconcile_once()
        expired = runtime.store.operation(operation_id)
        assert expired.operation_state.value == "uncertain"
        assert expired.recovery_required is True
        assert expired.error_code == "RECOVERY_REQUIRED"
        assert requester.methods == ["POST", "GET", "POST", "GET"]


def test_restart_preserves_old_sending_risk_time_and_expires_without_send(
    tmp_path: Path,
) -> None:
    config, tokens = _write_runtime_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "outbound_dispatch_allowed = false",
            "outbound_dispatch_allowed = true",
        ),
        encoding="utf-8",
    )
    first_risk = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    clock_value = [first_risk]
    requester = TimeoutPostThenEmptyDiscovery()
    provider = AgentMailTransport(
        AgentMailConfig(
            inbox_id="inbox-test",
            api_key="provider-test-key",
            base_url="https://agentmail.example.test/v0/",
        ),
        client=requester,
        clock=lambda: clock_value[0],
    )
    runtime = build_runtime(config, provider=provider, start_worker=False)
    runtime.worker.now = lambda: clock_value[0]
    runtime.store.now = lambda: clock_value[0]

    with TestClient(runtime.app) as client:
        created = client.post(
            "/v1/requests",
            headers=_headers(tokens["alice"]),
            json={
                "client_operation_id": "worker-old-sending-restart-1",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "bob",
                "packet": _packet("request", "Preserve the first send-risk timestamp."),
            },
        )
        assert created.status_code == 200
        operation_id = created.json()["operation_id"]
        runtime.worker.force_sending(operation_id)

        clock_value[0] += timedelta(hours=25)
        assert runtime.worker.recover_interrupted() == 1
        recovered = runtime.store.operation(operation_id)
        assert recovered.uncertain_since == first_risk

        runtime.worker.reconcile_once()
        expired = runtime.store.operation(operation_id)
        assert expired.recovery_required is True
        assert expired.error_code == "RECOVERY_REQUIRED"
        assert requester.methods == ["GET"]


def test_actual_worker_loop_tolerates_held_queue_then_dispatches_new_work(
    tmp_path: Path,
) -> None:
    config, tokens = _write_runtime_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "outbound_dispatch_allowed = false",
            "outbound_dispatch_allowed = true\n[worker]\npoll_seconds = 0.02",
        ),
        encoding="utf-8",
    )
    provider = FakeTransport()
    runtime = build_runtime(config, provider=provider, start_worker=True)
    held = runtime.service.requests_create(
        Actor("alice"),
        CreateRequest(
            "held-before-worker-start",
            RequestMode.ACTION_REQUESTED,
            "demo",
            "bob",
            Packet(
                schema_version=1,
                kind=MessageKind.REQUEST,
                session_reference="held-fixture",
                objective="Remain held while unrelated work proceeds.",
                context="Synthetic recovery-hold fixture.",
                constraints={"scope": "bounded"},
                sources=(),
                next_action="Do not dispatch until operator release.",
                expected_reply="None while held.",
                sensitivity="public-test",
            ),
        ),
    )
    runtime.store.apply_restore_holds()

    with TestClient(runtime.app) as client:
        time.sleep(0.08)
        assert runtime.worker_error is None
        assert runtime.worker_task is not None and not runtime.worker_task.done()
        assert client.get("/health/ready").json() == {"status": "ready"}
        held_operation = runtime.store.operation(held.operation_id)
        assert held_operation.operation_state.value == "queued"
        assert held_operation.recovery_hold is True

        response = client.post(
            "/v1/requests",
            headers=_headers(tokens["alice"]),
            json={
                "client_operation_id": "unheld-after-worker-start",
                "mode": "action_requested",
                "project": "demo",
                "recipient": "bob",
                "packet": _packet("request", "Dispatch without restarting the worker."),
            },
        )
        assert response.status_code == 200
        new_operation_id = response.json()["operation_id"]
        _wait_until(
            lambda: runtime.store.operation(new_operation_id).operation_state.value
            == "accepted"
        )
        assert runtime.worker_error is None
        assert runtime.store.operation(held.operation_id).operation_state.value == "queued"
