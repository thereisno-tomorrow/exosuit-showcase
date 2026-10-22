#!/usr/bin/env bash
set -euo pipefail
[[ "${GT_APPROVED_PUBLIC_GROK:-}" == "YES" ]] || { echo "separate public-route approval flag missing" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ "$(ss -H -ltn '( sport = :8766 )' | awk '{print $4}')" == "127.0.0.1:8766" ]] || { echo "narrow loopback listener 8766 missing" >&2; exit 1; }
for path in / /health/live /overview /v1/recovery/dispose; do
  code="$(curl --silent --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:8766$path")"
  [[ "$code" == 401 || "$code" == 403 || "$code" == 404 ]] || { echo "narrow listener exposed $path ($code)" >&2; exit 1; }
done
# No --set-path: Funnel forwards the path unchanged to a dedicated listener.
# Isolation is enforced by that listener's route table, not by proxy rewriting.
tailscale funnel --bg --https=8443 http://127.0.0.1:8766
tailscale funnel status
