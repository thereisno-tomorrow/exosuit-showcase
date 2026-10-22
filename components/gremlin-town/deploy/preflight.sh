#!/usr/bin/env bash
set -euo pipefail

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf 'CHECK: %s\n' "$*"; }

[[ "${EUID}" -eq 0 ]] || fail "run as root (read-only checks only)"
[[ "$(uname -m)" == "x86_64" ]] || fail "expected x86_64 host"
[[ -r /etc/os-release ]] || fail "/etc/os-release missing"
command -v systemctl >/dev/null || fail "systemd unavailable"
command -v tailscale >/dev/null || fail "tailscale CLI unavailable"
command -v curl >/dev/null || fail "curl unavailable"
command -v sha256sum >/dev/null || fail "sha256sum unavailable"
command -v tar >/dev/null || fail "tar unavailable (do not install without approval)"
[[ -x /usr/bin/bzip2 ]] || fail "/usr/bin/bzip2 unavailable (do not install without approval)"

for unit in gremlin-town.service; do
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q "$unit"; then
    fail "unit already exists: $unit"
  fi
done
getent passwd gremlin-town >/dev/null && fail "user gremlin-town already exists"
getent group gremlin-town >/dev/null && fail "group gremlin-town already exists"
[[ ! -e /opt/gremlin-town ]] || fail "/opt/gremlin-town already exists"
[[ ! -e /etc/gremlin-town ]] || fail "/etc/gremlin-town already exists"
[[ ! -e /var/lib/gremlin-town ]] || fail "/var/lib/gremlin-town already exists"

for port in 8765 8766; do
  ss -H -ltn "( sport = :$port )" | grep -q . && fail "TCP port $port is already listening"
done
note "existing listeners (must remain unchanged)"
ss -H -ltnp || true
note "Tailscale Serve status (must be empty before first apply)"
tailscale serve status || true
note "Tailscale Funnel status (must be empty before required public-route gate)"
tailscale funnel status || true
note "capacity"
df -h / /opt /var
df -B1 --output=avail /var | tail -n 1
free -m
note "preflight is read-only and made no changes"
