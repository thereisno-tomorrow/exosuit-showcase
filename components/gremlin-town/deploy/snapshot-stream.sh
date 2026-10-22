#!/usr/bin/env bash
set -euo pipefail
umask 077

# Source copies resolve their own release. apply.sh replaces this assignment
# with an immutable release path in installed copies; caller overrides are ignored.
readonly GT_BACKUP_RELEASE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Runs as gremlin-town over the approved SSH route. Emits one tar stream to
# stdout; diagnostics go to stderr. No secret file is included.
db="${GT_DB:-/var/lib/gremlin-town/gremlin-town.sqlite3}"
base=/var/lib/gremlin-town/backup-staging
[[ "$(id -un)" == gremlin-town ]] || { echo "must run as gremlin-town" >&2; exit 1; }
[[ -f "$db" ]] || { echo "database missing" >&2; exit 1; }
available="$(df -B1 --output=avail "$base" | tail -n 1 | tr -d ' ')"
db_size="$(stat -c %s "$db")"
required=$((1073741824 + db_size + 268435456))
(( available >= required )) || { echo "insufficient staging capacity" >&2; exit 1; }

run_dir="$(mktemp -d "$base/export.XXXXXXXX")"
case "$run_dir" in
  /var/lib/gremlin-town/backup-staging/export.*) ;;
  *) echo "unsafe staging path" >&2; exit 1 ;;
esac
trap 'rm -rf -- "$run_dir"' EXIT

"$GT_BACKUP_RELEASE_ROOT/.venv/bin/python" -I \
  "$GT_BACKUP_RELEASE_ROOT/deploy/sqlite_backup.py" "$db" "$run_dir/gremlin-town.sqlite3"
install -m 0600 /etc/gremlin-town/config.toml "$run_dir/config.toml"
install -m 0600 /etc/gremlin-town/participants.toml "$run_dir/participants.toml"
{
  printf 'created_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'release=%s\n' "$(readlink -f /opt/gremlin-town/current)"
  printf 'source_host=%s\n' "$(hostname)"
} > "$run_dir/MANIFEST"
(cd "$run_dir" && sha256sum MANIFEST config.toml participants.toml gremlin-town.sqlite3 > SHA256SUMS)
# Validate the exact work schema and non-secret configuration before emitting
# any bytes. OAuth databases, tokens, signing keys and session stores stay out.
"$GT_BACKUP_RELEASE_ROOT/.venv/bin/python" -I \
  "$GT_BACKUP_RELEASE_ROOT/deploy/validate-backup.py" directory "$run_dir"
tar --format=pax --numeric-owner --owner=0 --group=0 -C "$run_dir" -cf - \
  MANIFEST SHA256SUMS config.toml participants.toml gremlin-town.sqlite3
