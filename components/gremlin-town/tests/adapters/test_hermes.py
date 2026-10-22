from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from adapters.hermes.hub_client import (
    HubClientConfig, HubClientError, HubHTTPSClient, TEN_OPERATIONS, BRAIN_OPERATIONS, DOOR_OPERATIONS, REVIEW_OPERATIONS,
)
from adapters.hermes import hub_client, plugin
from adapters.hermes.plugin import SETTINGS_FILE, TOOLSET, ALL_TOOL_DEFINITIONS, register
from tests.api.test_api import real_hub, _auth, _create_request, _return_result  # noqa: F401  (real_hub is a fixture)


class FakeHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: HubClientError | None = None

    def call(self, operation: str, payload: dict[str, Any]) -> Any:
        self.calls.append((operation, dict(payload)))
        if self.error is not None:
            raise self.error
        return {"operation": operation, "authenticated_actor": "from-token"}


def test_the_reply_definition_and_handler_pass_proof_without_trusting_it():
    client = FakeHubClient()
    definition = next(d for d in plugin.TOOL_DEFINITIONS if d.operation == "requests.reply")
    assert definition.parameters["properties"]["proof"]["maxLength"] == 300
    args = {"request_id": "req_test", "client_operation_id": "result-test", "expected_revision": 2,
            "kind": "result", "packet": {}, "proof": "https://example.test/result"}
    _registered_handler(client, "gremlin_town_requests_reply")(args)
    assert client.calls == [("requests.reply", args)]


class LoaderContext:
    def __init__(self) -> None:
        self.registrations: list[dict[str, Any]] = []

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Any,
        check_fn: Any = None,
        requires_env: Any = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ) -> None:
        self.registrations.append(
            {
                "name": name,
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                "check_fn": check_fn,
                "requires_env": requires_env,
                "is_async": is_async,
                "description": description,
                "emoji": emoji,
                "override": override,
            }
        )


def test_local_loader_contract_registers_twenty_two_non_overriding_json_tools_by_default() -> None:
    """Local loader-contract proof; installed-runtime loading remains a later gate.

    bc_orient/bc_next/bc_next_respond (`[orientation]`, step F) and gremlin_town_review_respond/
    gremlin_town_moments_signal (`[review]`, step E) are off on the real, unmodified
    plugin-settings.json this reads, so only the 10 GT + 12 always-on BC tools and private door register."""

    client = FakeHubClient()
    ctx = LoaderContext()
    register(ctx, client=client)  # type: ignore[arg-type]

    expected_names = [
        item.name for item in ALL_TOOL_DEFINITIONS if item.name not in plugin.OPTIONAL_TOOL_FEATURES
    ]
    assert len(ctx.registrations) == 23
    assert [item["name"] for item in ctx.registrations] == expected_names
    assert len({item["name"] for item in ctx.registrations}) == 23
    assert {item["toolset"] for item in ctx.registrations} == {TOOLSET}
    assert {item["override"] for item in ctx.registrations} == {False}
    assert all(item["schema"]["parameters"]["additionalProperties"] is False for item in ctx.registrations)
    assert set(plugin.OPTIONAL_TOOL_FEATURES).isdisjoint({item["name"] for item in ctx.registrations})

    expected_operations = [
        item.operation for item in ALL_TOOL_DEFINITIONS if item.name not in plugin.OPTIONAL_TOOL_FEATURES
    ]
    assert expected_operations == [*TEN_OPERATIONS, *BRAIN_OPERATIONS[:12], *DOOR_OPERATIONS]
    for expected_operation, registration in zip(expected_operations, ctx.registrations, strict=True):
        signature = inspect.signature(registration["handler"])
        assert list(signature.parameters) == ["args", "kwargs"]
        result = registration["handler"]({}, ignored_dispatch_metadata=True)
        assert isinstance(result, str)
        decoded = json.loads(result)
        assert decoded == {
            "ok": True,
            "result": {"operation": expected_operation, "authenticated_actor": "from-token"},
        }


def test_handler_preserves_hub_error_code_without_generic_collapse() -> None:
    client = FakeHubClient()
    client.error = HubClientError(
        "RECOVERY_REQUIRED",
        "operator recovery is required",
        status_code=409,
        retryable=False,
        details={"request_id": "req-1", "next_action": "contact operator"},
    )
    ctx = LoaderContext()
    register(ctx, client=client)  # type: ignore[arg-type]
    result = json.loads(ctx.registrations[3]["handler"]({"request_id": "req-1"}))
    assert result["ok"] is False
    assert result["error"] == {
        "code": "RECOVERY_REQUIRED",
        "message": "operator recovery is required",
        "next_action": "contact operator",
        "request_id": "req-1",
        "retryable": False,
        "status_code": 409,
    }
    assert "ADAPTER_ERROR" not in json.dumps(result)


def _registered_handler(client: Any, name: str) -> Any:
    ctx = LoaderContext()
    register(ctx, client=client)  # type: ignore[arg-type]
    return next(item["handler"] for item in ctx.registrations if item["name"] == name)


def test_diablo_request_list_asks_for_ten_rows_unless_he_names_a_limit() -> None:
    """30 Sep 2026 timing audit: one unfiltered list, a full page of the hub's 100 rows, put 95,148
    characters into Nic's chat. Diablo's plugin now asks for DIABLO_LIST_LIMIT rows when he names no
    limit; an explicit limit, the other filters and the cursor pass through unchanged."""

    client = FakeHubClient()
    handler = _registered_handler(client, "gremlin_town_requests_list")
    handler({})
    handler({"filters": {"recipient": "hermes-wa"}, "cursor": "10"})
    handler({"filters": {"open": True, "limit": 50}})
    handler({"filters": None})
    assert plugin.DIABLO_LIST_LIMIT == 10
    assert client.calls == [
        ("requests.list", {"filters": {"limit": 10}}),
        ("requests.list", {"filters": {"recipient": "hermes-wa", "limit": 10}, "cursor": "10"}),
        ("requests.list", {"filters": {"open": True, "limit": 50}}),
        ("requests.list", {"filters": {"limit": 10}}),
    ]


def test_a_list_filter_the_hub_does_not_read_is_refused_not_silently_dropped() -> None:
    """30 Sep 2026 hub checks: Diablo sent {"filters": {"sender_id": "exosuit-review"}}; the hub ignores
    unknown query fields, so the list came back unfiltered (95,148 characters). The plugin now refuses
    it with the filters that exist, before anything is sent."""

    client = FakeHubClient()
    handler = _registered_handler(client, "gremlin_town_requests_list")
    refused = json.loads(handler({"filters": {"sender_id": "exosuit-review"}}))
    assert refused["ok"] is False and refused["error"]["code"] == "INVALID_ARGUMENT"
    assert "no filter sender_id" in refused["error"]["message"]
    assert "requester_id" in refused["error"]["message"]
    assert client.calls == []
    assert plugin.LIST_FILTERS == {"open", "stale", "recipient", "project", "work_state", "attention", "limit"}
    # Every filter the hub does read still passes, with the default page size.
    handler({"filters": {"open": True, "recipient": "hermes-wa", "project": "gremlin-town",
                         "work_state": "requested", "attention": False}})
    assert client.calls == [("requests.list", {"filters": {
        "open": True, "recipient": "hermes-wa", "project": "gremlin-town", "work_state": "requested",
        "attention": False, "limit": 10,
    }})]


def test_a_misplaced_list_argument_gets_a_clear_refusal() -> None:
    """A top-level limit used to reach hub_client as "filter names conflict with top-level query fields";
    a cursor inside filters, or any other top-level name, is refused before sending with the shape."""

    client = FakeHubClient()
    handler = _registered_handler(client, "gremlin_town_requests_list")
    top_limit = json.loads(handler({"limit": 5}))["error"]
    assert top_limit["code"] == "INVALID_ARGUMENT" and top_limit["message"].startswith("put limit inside filters")
    assert "cursor" in top_limit["message"]
    other = json.loads(handler({"since": "2026-09-30"}))["error"]
    assert other["message"].startswith("requests.list has no argument since")
    cursor_inside = json.loads(handler({"filters": {"recipient": "hermes-wa", "cursor": "3"}}))["error"]
    assert cursor_inside["message"].startswith("cursor goes at the top level")
    assert client.calls == []
    # Protected names are still the client's own refusal, unchanged.
    requester_calls: list[str] = []

    class NeverCalled:
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            requester_calls.append(url)
            raise AssertionError("never sent")

    real = HubHTTPSClient(HubClientConfig("https://hub.test", "fixture-token"), client=NeverCalled())
    protected = json.loads(_registered_handler(real, "gremlin_town_requests_list")({"token": "x"}))["error"]
    assert protected["message"] == "operation arguments cannot set protected field: token"
    assert requester_calls == []


def test_the_list_cursor_needs_the_same_filters_and_the_tool_text_says_so(real_hub) -> None:
    """The hub's cursor is a plain row offset, not tied to the filters: a bare cursor pages the unfiltered
    list. Diablo's tool text says to pass the same filters with next_cursor."""

    client, worker = real_hub

    class LoopbackOnly:
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            kwargs.pop("timeout", None)
            return client.request(method, urlsplit(url).path, **kwargs)

    to_bob = {
        _create_request(client, worker, client_operation_id=f"to-bob-{number}")["request_id"] for number in range(3)
    }
    to_alice = {
        _create_request(client, worker, requester="bob", recipient="alice", client_operation_id=f"to-alice-{n}")[
            "request_id"
        ]
        for n in range(3)
    }
    handler = _registered_handler(
        HubHTTPSClient(HubClientConfig("https://hub.test", "bob-token"), client=LoopbackOnly()),
        "gremlin_town_requests_list",
    )
    first = json.loads(handler({"filters": {"recipient": "bob", "limit": 2}}))["result"]
    assert first["next_cursor"] == "2"
    same_filters = json.loads(handler({"filters": {"recipient": "bob", "limit": 2}, "cursor": "2"}))["result"]
    assert {item["request_id"] for item in first["items"] + same_filters["items"]} == to_bob
    bare = json.loads(handler({"cursor": "2"}))["result"]
    assert {item["request_id"] for item in bare["items"]} & to_alice  # the filter is gone
    description = next(item for item in ALL_TOOL_DEFINITIONS if item.name == "gremlin_town_requests_list").description
    assert "next_cursor gets more with the same filters" in description


def test_the_hub_route_reads_exactly_the_filters_the_plugin_allows() -> None:
    import inspect as inspect_module

    from gremlin_town.api.routes import requests_list

    parameters = set(inspect_module.signature(requests_list).parameters) - {"request", "actor", "cursor"}
    assert parameters == plugin.LIST_FILTERS


def test_only_the_request_list_gets_the_default() -> None:
    client = FakeHubClient()
    _registered_handler(client, "gremlin_town_events_list")({"filters": {}})
    _registered_handler(client, "gremlin_town_requests_read")({"request_id": "req-1"})
    assert client.calls == [("events.list", {"filters": {}}), ("requests.read", {"request_id": "req-1"})]


def test_a_malformed_list_filter_is_still_refused_by_the_client() -> None:
    requester_calls: list[Any] = []

    class NeverCalled:
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            requester_calls.append(url)
            raise AssertionError("a malformed filter must not reach the hub")

    client = HubHTTPSClient(HubClientConfig("https://hub.test", "fixture-token"), client=NeverCalled())
    result = json.loads(_registered_handler(client, "gremlin_town_requests_list")({"filters": "open"}))
    assert result["ok"] is False and result["error"]["code"] == "INVALID_ARGUMENT"
    assert requester_calls == []


def test_tool_arguments_expose_no_endpoint_token_or_actor_selector() -> None:
    protected = {"actor", "actor_id", "authorization", "base_url", "endpoint", "sender", "token", "url"}
    for definition in ALL_TOOL_DEFINITIONS:
        assert protected.isdisjoint(definition.parameters["properties"])
        assert definition.parameters["additionalProperties"] is False


def test_runtime_check_accepts_protected_token_file_without_token_env(monkeypatch: Any) -> None:
    configured_path = (Path.cwd() / "protected-hermes.token").resolve()
    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN", raising=False)
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN_FILE", str(configured_path))
    monkeypatch.setattr(hub_client, "_read_protected_token_file", lambda path: "file-token")
    assert plugin._runtime_configuration_available() is True


def test_runtime_check_denies_when_both_token_sources_are_missing(monkeypatch: Any) -> None:
    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN", raising=False)
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN_FILE", raising=False)
    monkeypatch.setattr(
        hub_client,
        "_read_protected_token_file",
        lambda path: (_ for _ in ()).throw(HubClientError("CONFIGURATION_MISSING", "missing")),
    )
    assert plugin._runtime_configuration_available() is False


def test_plugin_local_non_secret_settings_bind_endpoint_and_token_file(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hub_url": "https://hub.example.test",
                "token_file": "/opt/data/secrets/gremlin-town.token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GREMLIN_TOWN_HUB_URL", raising=False)
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN", raising=False)
    monkeypatch.delenv("GREMLIN_TOWN_HUB_TOKEN_FILE", raising=False)
    observed: list[Path] = []
    monkeypatch.setattr(
        hub_client,
        "_read_protected_token_file",
        lambda path: observed.append(path) or "file-token",
    )

    config = hub_client.HubClientConfig.from_environment(settings_path=settings)

    assert config.base_url == "https://hub.example.test"
    assert observed == [Path("/opt/data/secrets/gremlin-town.token")]
    assert set(json.loads(settings.read_text(encoding="utf-8"))) == {
        "hub_url",
        "token_file",
    }


def test_plugin_settings_reject_secret_or_unknown_fields(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text('{"hub_url":"https://hub.example.test","hub_token":"secret"}', encoding="utf-8")
    try:
        hub_client.HubClientConfig.from_environment(settings_path=settings)
    except HubClientError as exc:
        assert exc.code == "CONFIGURATION_INVALID"
    else:
        raise AssertionError("secret-bearing settings were accepted")


def test_manifest_matches_registered_tools() -> None:
    manifest = Path(__file__).parents[2] / "adapters" / "hermes" / "plugin.yaml"
    text = manifest.read_text(encoding="utf-8")
    assert "kind: standalone" in text
    assert "requires_env: []" in text
    assert "GREMLIN_TOWN_HUB_URL" in text
    assert "GREMLIN_TOWN_HUB_TOKEN" in text
    requires_block, optional_block = text.split("optional_env:", 1)
    assert "GREMLIN_TOWN_HUB_TOKEN" not in requires_block
    assert "GREMLIN_TOWN_HUB_TOKEN_FILE" in optional_block
    assert [line.strip()[2:] for line in text.splitlines() if line.strip().startswith(("- gremlin_town_", "- bc_"))] == [
        definition.name for definition in ALL_TOOL_DEFINITIONS
    ]
    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    assert settings == {
        "hub_url": "https://hub.example.invalid",
        "token_file": "/opt/data/secrets/gremlin-town.token",
    }


def test_review_operations_map_to_the_real_review_routes() -> None:
    """Step E: review.respond and moments.signal, through the real HubHTTPSClient
    mapping (not the FakeHubClient tool tests use elsewhere in this file)."""

    from tests.adapters.test_cloud import Requester

    requester = Requester()
    client = HubHTTPSClient(HubClientConfig("https://hub.test", "fixture-token"), client=requester)

    client.call("moments.signal", {"kind": "pc_on"})
    method, url, kwargs = requester.calls[0]
    assert (method, url) == ("POST", "https://hub.test/v1/moments/signals")
    assert kwargs["json"] == {"kind": "pc_on"}

    client.call("review.respond", {"item_ref": "brn_bill", "response": "picked_today"})
    method, url, kwargs = requester.calls[1]
    assert (method, url) == ("POST", "https://hub.test/v1/review/responses")
    assert kwargs["json"] == {"item_ref": "brn_bill", "response": "picked_today"}


def test_optional_tools_are_not_registered_by_default() -> None:
    """bc_orient/bc_next/bc_next_respond and the two review tools are steps E/F: off on the hub
    by default (found live 2026-09-26: a default install offered orient and it failed
    every call), so a default plugin install must not register them either."""

    client = FakeHubClient()
    ctx = LoaderContext()
    register(ctx, client=client)  # type: ignore[arg-type]
    registered = {item["name"] for item in ctx.registrations}
    assert set(plugin.OPTIONAL_TOOL_FEATURES).isdisjoint(registered)


def test_review_operations_are_registered_as_hermes_tools_only_when_enabled(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = tmp_path / "plugin-settings.json"
    settings.write_text(json.dumps({"enabled_optional_features": ["review"]}), encoding="utf-8")
    monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", settings)

    client = FakeHubClient()
    ctx = LoaderContext()
    register(ctx, client=client)  # type: ignore[arg-type]
    by_name = {item["name"]: item for item in ctx.registrations}
    assert {"gremlin_town_review_respond", "gremlin_town_moments_signal"} <= set(by_name)
    assert {"bc_orient", "bc_next", "bc_next_respond"}.isdisjoint(by_name)

    result = json.loads(by_name["gremlin_town_moments_signal"]["handler"]({"kind": "pc_on"}))
    assert result == {"ok": True, "result": {"operation": "moments.signal", "authenticated_actor": "from-token"}}
    assert client.calls[-1] == ("moments.signal", {"kind": "pc_on"})


def test_orientation_tools_are_registered_as_hermes_tools_only_when_enabled(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = tmp_path / "plugin-settings.json"
    settings.write_text(json.dumps({"enabled_optional_features": ["orientation"]}), encoding="utf-8")
    monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", settings)

    client = FakeHubClient()
    ctx = LoaderContext()
    register(ctx, client=client)  # type: ignore[arg-type]
    by_name = {item["name"]: item for item in ctx.registrations}
    assert {"bc_orient", "bc_next", "bc_next_respond", "bc_agenda", "bc_weekly"} <= set(by_name)
    assert "bc_project_save" not in by_name
    assert "bc_orient_respond" not in by_name  # renamed bc_next_respond (Ciel slice 4, gap G9)
    # bc_agenda, bc_weekly and bc_project_save joined them in release ciel-03 (PRD Â§4.8-Â§4.10).
    assert len(by_name) == 28
    assert {"gremlin_town_review_respond", "gremlin_town_moments_signal"}.isdisjoint(by_name)

    body = {"surfacing_id": "brn_20261004_120000_ab12", "response": "yes", "first_step": "Open the deck"}
    result = json.loads(by_name["bc_next_respond"]["handler"](body))
    assert result == {"ok": True, "result": {"operation": "next.respond", "authenticated_actor": "from-token"}}
    assert client.calls[-1] == ("next.respond", body)


def test_the_calendar_removal_tool_registers_only_with_its_own_calendar_feature(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """bc_calendar_remove (step E Â§5, N8) exists on the hub only while calendar entries are on,
    which needs Google as well as [review]: "review" alone does not register it."""

    settings = tmp_path / "plugin-settings.json"
    monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", settings)
    for features, expected in ((["review"], False), (["calendar"], True)):
        settings.write_text(json.dumps({"enabled_optional_features": features}), encoding="utf-8")
        client = FakeHubClient()
        ctx = LoaderContext()
        register(ctx, client=client)  # type: ignore[arg-type]
        by_name = {item["name"]: item for item in ctx.registrations}
        assert ("bc_calendar_remove" in by_name) is expected
    assert len(by_name) == 24
    body = {"operation_id": "remove-1", "record_id": "brn_20260928_101500_ab12",
            "said_by": "owner", "came_by": "direct", "words": "yes"}
    result = json.loads(by_name["bc_calendar_remove"]["handler"](body))
    assert result == {"ok": True, "result": {"operation": "calendar.remove", "authenticated_actor": "from-token"}}
    assert client.calls[-1] == ("calendar.remove", body)


def test_all_optional_tools_register_when_every_feature_is_enabled(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = tmp_path / "plugin-settings.json"
    settings.write_text(
        json.dumps({"enabled_optional_features": ["review", "orientation", "calendar"]}), encoding="utf-8"
    )
    monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", settings)

    client = FakeHubClient()
    ctx = LoaderContext()
    register(ctx, client=client)  # type: ignore[arg-type]
    assert len(ctx.registrations) == 31
    assert all(item["name"] != "bc_project_save" for item in ctx.registrations)
    assert [item["name"] for item in ctx.registrations] == [item.name for item in ALL_TOOL_DEFINITIONS]
    for expected_operation, registration in zip(
        TEN_OPERATIONS + BRAIN_OPERATIONS + DOOR_OPERATIONS + REVIEW_OPERATIONS, ctx.registrations, strict=True
    ):
        result = json.loads(registration["handler"]({}))
        assert result == {
            "ok": True,
            "result": {"operation": expected_operation, "authenticated_actor": "from-token"},
        }


def test_optional_feature_settings_reject_unknown_or_malformed_entries(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = tmp_path / "plugin-settings.json"
    settings.write_text(json.dumps({"enabled_optional_features": ["not-a-real-feature"]}), encoding="utf-8")
    monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", settings)
    try:
        plugin._enabled_optional_features()
    except HubClientError as exc:
        assert exc.code == "CONFIGURATION_INVALID"
    else:
        raise AssertionError("unknown optional feature name was accepted")

    settings.write_text(json.dumps({"enabled_optional_features": ["review"], "extra": 1}), encoding="utf-8")
    try:
        plugin._enabled_optional_features()
    except HubClientError as exc:
        assert exc.code == "CONFIGURATION_INVALID"
    else:
        raise AssertionError("an unsupported field was accepted")


def test_missing_plugin_settings_file_means_every_optional_feature_is_off(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(plugin, "PLUGIN_SETTINGS_FILE", tmp_path / "absent-plugin-settings.json")
    assert plugin._enabled_optional_features() == frozenset()


def test_directory_entrypoint_loads_with_hermes_equivalent_package_semantics(monkeypatch: Any) -> None:
    """Exercise the same package import shape used by the inspected loader."""

    plugin_dir = Path(__file__).parents[2] / "adapters" / "hermes"
    module_name = "hermes_plugins.gremlin_town_local_contract"
    parent_name = "hermes_plugins"
    monkeypatch.setenv("GREMLIN_TOWN_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("GREMLIN_TOWN_HUB_TOKEN", "local-loader-token")
    if parent_name not in sys.modules:
        parent = type(sys)(parent_name)
        parent.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent_name] = parent
    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert list(inspect.signature(module.register).parameters) == ["ctx"]
        ctx = LoaderContext()
        module.register(ctx)
        assert len(ctx.registrations) == 23
        assert all(registration["check_fn"]() is True for registration in ctx.registrations)
    finally:
        for name in [key for key in sys.modules if key == module_name or key.startswith(module_name + ".")]:
            del sys.modules[name]


def test_requests_check_travels_through_the_real_hub_and_refuses_the_doer(real_hub) -> None:
    """ROA-13/D19 end-to-end proof through the real shared adapter mapping (not a fake
    Requester or FakeHubClient): HubHTTPSClient.call("requests.check", ...) is exactly what
    both gremlin_town_requests_check (this file) and Fleet Boss's requests.check (test_cloud.py)
    dispatch to. One participant (bob) returns a result on a real in-process hub; a different
    one (alice) checks it and the request leaves the open set; bob's own check of its own
    result is refused the same way test_api.py's hub-only proof expects (404)."""

    client, worker = real_hub

    class LoopbackOnly:
        """Route HubHTTPSClient's absolute https://hub.test/... URLs into the real_hub
        TestClient, which only understands paths - the same shape adapters/hermes/plugin.py's
        damien_http fixture uses for its own loopback shim."""

        def __init__(self, test_client: Any) -> None:
            self._test_client = test_client

        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            kwargs.pop("timeout", None)
            return self._test_client.request(method, urlsplit(url).path, **kwargs)

    created = _create_request(client, worker, client_operation_id="adapter-check-create")
    result = _return_result(client, worker, created["request_id"], key="adapter-check-result")

    alice = HubHTTPSClient(HubClientConfig("https://hub.test", "alice-token"), client=LoopbackOnly(client))
    bob = HubHTTPSClient(HubClientConfig("https://hub.test", "bob-token"), client=LoopbackOnly(client))

    with pytest.raises(HubClientError) as refusal:
        bob.call("requests.check", {
            "client_operation_id": "adapter-check-self",
            "request_id": created["request_id"],
            "expected_revision": result["revision"],
            "outcome": "accepted",
            "evidence": "looks fine to me",
        })
    assert refusal.value.status_code == 404

    alice.call("requests.check", {
        "client_operation_id": "adapter-check-accept",
        "request_id": created["request_id"],
        "expected_revision": result["revision"],
        "outcome": "accepted",
        "evidence": "reviewed the artifact end to end",
    })

    listing = client.get("/v1/requests?open=true", headers=_auth("alice"))
    assert created["request_id"] not in {item["request_id"] for item in listing.json()["items"]}


def test_diablo_list_default_pages_through_the_real_hub_without_losing_a_row(real_hub) -> None:
    """End to end through the real hub: with no limit Diablo's list returns 10 rows and a cursor, and
    the cursor reaches the rest; the hub's own default (100) is unchanged for everyone else."""

    client, worker = real_hub

    class LoopbackOnly:
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            kwargs.pop("timeout", None)
            return client.request(method, urlsplit(url).path, **kwargs)

    created = {
        _create_request(client, worker, client_operation_id=f"list-default-{number}")["request_id"]
        for number in range(12)
    }
    handler = _registered_handler(
        HubHTTPSClient(HubClientConfig("https://hub.test", "bob-token"), client=LoopbackOnly()),
        "gremlin_town_requests_list",
    )
    first = json.loads(handler({}))["result"]
    assert len(first["items"]) == 10 and first["next_cursor"] is not None
    rest = json.loads(handler({"cursor": first["next_cursor"]}))["result"]
    assert rest["next_cursor"] is None
    assert {item["request_id"] for item in first["items"] + rest["items"]} == created
    # Another agent calling the hub directly still gets the hub's own default page.
    assert len(client.get("/v1/requests", headers=_auth("bob")).json()["items"]) == 12
