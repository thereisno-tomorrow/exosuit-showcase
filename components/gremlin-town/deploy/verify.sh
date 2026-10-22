#!/usr/bin/env bash
set -euo pipefail

host_name="${GT_TAILNET_HOST:-hub.example.invalid}"
local_url=http://127.0.0.1:8765
private_url="https://${host_name}"
public_url="https://${host_name}:8443"

systemctl is-active --quiet gremlin-town.service
systemctl show gremlin-town.service -p User -p Group -p FragmentPath -p MainPID
ss -H -ltnp '( sport = :8765 )'
[[ "$(ss -H -ltn '( sport = :8765 )' | awk '{print $4}')" == "127.0.0.1:8765" ]]
[[ "$(ss -H -ltn '( sport = :8766 )' | awk '{print $4}')" == "127.0.0.1:8766" ]]
curl --fail --silent --show-error "$local_url/health/live"
curl --fail --silent --show-error "$local_url/health/ready"

# No credential: health may be public, every data/tool route must deny.
for path in /v1/participants /v1/requests /v1/events /overview; do
  code="$(curl --silent --output /dev/null --write-out '%{http_code}' "$private_url$path")"
  [[ "$code" == 401 || "$code" == 403 ]] || { echo "unauthenticated $path returned $code" >&2; exit 1; }
done

tailscale serve status
tailscale funnel status
for path in / /health/live /overview /login /logout /v1/recovery/dispose; do
  code="$(curl --silent --output /dev/null --write-out '%{http_code}' "$public_url$path")"
  [[ "$code" == 401 || "$code" == 403 || "$code" == 404 ]] || { echo "public narrow listener exposed $path ($code)" >&2; exit 1; }
done
journalctl -u gremlin-town.service --since '-10 minutes' --no-pager -n 200
echo "Host checks passed. Client-specific loading/auth checks remain separate."
