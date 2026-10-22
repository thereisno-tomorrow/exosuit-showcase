#!/usr/bin/env bash
set -euo pipefail
[[ "${GT_APPROVED_APPLY:-}" == "YES" ]] || { echo "set GT_APPROVED_APPLY=YES after rollback approval" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ $# -eq 1 ]] || { echo "usage: rollback.sh PREVIOUS_RELEASE_ID" >&2; exit 64; }
previous="$1"
[[ "$previous" =~ ^[0-9A-Za-z._-]{1,64}$ ]] || { echo "unsafe release id" >&2; exit 1; }
[[ -x "/opt/gremlin-town/releases/$previous/.venv/bin/gremlin-town" ]] || { echo "previous release missing" >&2; exit 1; }

systemctl stop gremlin-town.service
ln -sfn "/opt/gremlin-town/releases/$previous" /opt/gremlin-town/current
systemctl start gremlin-town.service
systemctl is-active --quiet gremlin-town.service
curl --fail --silent --show-error http://127.0.0.1:8765/health/ready
echo "Code pointer rolled back. No database, credential, backup, route, user, or release was deleted."
