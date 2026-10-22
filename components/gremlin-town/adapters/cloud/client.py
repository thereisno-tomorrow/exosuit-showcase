"""Structured CLI for the shared Gremlin Town participant HTTPS client."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .hub_client import (
    BRAIN_OPERATIONS,
    TEN_OPERATIONS,
    HubClientConfig,
    HubClientError,
    HubHTTPSClient,
)


# Compatibility names for callers that imported the earlier adapter draft.
CloudClientConfig = HubClientConfig
CloudClientError = HubClientError
CloudHTTPSClient = HubHTTPSClient

CLIENT_SETTINGS_FILE = Path(__file__).with_name("client-settings.json")

# orient/next/next.respond (`[orientation]`, step F) are off on the hub by default (see
# docs/operations.md). Until 2026-09-27 the launcher accepted the operation name anyway and
# relied on the hub's own 422 to refuse it (ROA-22) - unlike adapters/hermes/plugin.py, which
# has never registered its orientation tools unless plugin-settings.json's
# enabled_optional_features names "orientation" (commit 8587f33). This mirrors that mechanism
# for Fleet Boss: same key, same default-off, sibling file named for this adapter's entry
# point (client.py / client-settings.json) instead of Diablo's (plugin.py / plugin-settings.json).
# calendar.remove (step E §5, N8) exists on the hub only while calendar entries are on, the
# same as Diablo's bc_calendar_remove: "calendar" turns it on here too.
OPTIONAL_OPERATION_FEATURES: dict[str, str] = {
    "orient": "orientation",
    "next": "orientation",
    "next.respond": "orientation",
    "calendar.remove": "calendar",
}
KNOWN_OPTIONAL_FEATURES = frozenset(OPTIONAL_OPERATION_FEATURES.values())


def _enabled_optional_features() -> frozenset[str]:
    """Read which of ``KNOWN_OPTIONAL_FEATURES`` this install has turned on.

    A missing file, or an absent key, means none enabled - off, same as the hub's own default
    and the same fallback adapters/hermes/plugin.py uses for a missing plugin-settings.json.
    """

    if not CLIENT_SETTINGS_FILE.is_file():
        return frozenset()
    try:
        raw = json.loads(CLIENT_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HubClientError(
            "CONFIGURATION_INVALID", "Fleet Boss client settings unavailable or invalid"
        ) from exc
    if not isinstance(raw, dict) or set(raw) - {"enabled_optional_features"}:
        raise HubClientError(
            "CONFIGURATION_INVALID", "the client settings file has unsupported fields"
        )
    features = raw.get("enabled_optional_features", [])
    if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
        raise HubClientError(
            "CONFIGURATION_INVALID", "enabled_optional_features must be a list of strings"
        )
    unknown = sorted(set(features) - KNOWN_OPTIONAL_FEATURES)
    if unknown:
        raise HubClientError(
            "CONFIGURATION_INVALID", f"enabled_optional_features has unknown entries: {unknown}"
        )
    return frozenset(features)


def _allowed_operations() -> tuple[str, ...]:
    enabled = _enabled_optional_features()
    return tuple(
        operation
        for operation in (*TEN_OPERATIONS, *BRAIN_OPERATIONS)
        if OPTIONAL_OPERATION_FEATURES.get(operation) is None
        or OPTIONAL_OPERATION_FEATURES[operation] in enabled
    )


class CloudCLI:
    def __init__(self, client_factory: Any = CloudHTTPSClient) -> None:
        self.client_factory = client_factory

    def run(self, operation: str, payload_text: str) -> str:
        required_feature = OPTIONAL_OPERATION_FEATURES.get(operation)
        if required_feature is not None:
            try:
                enabled = _enabled_optional_features()
            except HubClientError as exc:
                return json.dumps({"ok": False, "error": exc.as_dict()}, sort_keys=True)
            if required_feature not in enabled:
                return json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "FEATURE_DISABLED",
                            "message": (
                                f"{operation} is disabled until '{required_feature}' is added to "
                                "client-settings.json's enabled_optional_features"
                            ),
                            "retryable": False,
                        },
                    },
                    sort_keys=True,
                )
        try:
            payload = json.loads(payload_text)
            if not isinstance(payload, dict):
                raise ValueError
            result = self.client_factory(CloudClientConfig.from_environment()).call(operation, payload)
            return json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True)
        except HubClientError as exc:
            return json.dumps(
                {"ok": False, "error": exc.as_dict()},
                sort_keys=True,
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            return json.dumps(
                {"ok": False, "error": {"code": "INVALID_JSON", "message": "payload must be a JSON object"}},
                sort_keys=True,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call a Gremlin Town or Brain operation on the hub over HTTPS",
        epilog=(
            "The credential authenticates this shared-cloud installation; it does not prove "
            "which bot on that installation invoked the client."
        ),
    )
    parser.add_argument("operation", choices=_allowed_operations())
    parser.add_argument("payload", nargs="?", default="{}", help="JSON object; URL and token come from environment")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = CloudCLI().run(args.operation, args.payload)
    print(output)
    return 0 if json.loads(output).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
