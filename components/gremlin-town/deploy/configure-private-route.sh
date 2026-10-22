#!/usr/bin/env bash
set -euo pipefail
[[ "${GT_APPROVED_PRIVATE_ROUTE:-}" == "YES" ]] || { echo "approval flag missing" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
curl --fail --silent --show-error http://127.0.0.1:8765/health/ready >/dev/null
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve status
