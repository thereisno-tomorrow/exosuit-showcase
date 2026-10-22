#!/usr/bin/env bash
set -euo pipefail
umask 077

# Source copies resolve their own release. apply.sh replaces this assignment
# with an immutable release path in installed copies; caller overrides are ignored.
readonly GT_BACKUP_RELEASE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

[[ "${GT_APPROVED_RESTORE:-}" == "YES" ]] || { echo "restore approval flag missing" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ $# -eq 2 ]] || { echo "usage: restore.sh ARCHIVE EMPTY_RESTORE_DIR" >&2; exit 64; }
archive="$(readlink -f "$1")"
target="$(readlink -m "$2")"
[[ "$archive" == /var/lib/gremlin-town/restore/incoming/*.tar ]] || { echo "unsafe archive path" >&2; exit 1; }
[[ "$target" == /var/lib/gremlin-town/restore/* && "$target" != /var/lib/gremlin-town/restore/incoming* ]] || { echo "unsafe restore target" >&2; exit 1; }
[[ ! -e "$target" ]] || { echo "restore target must not exist" >&2; exit 1; }
# Header allowlist is checked before extraction; checksums, configuration and
# work-only SQLite schema are checked in private staging before target creation.
"$GT_BACKUP_RELEASE_ROOT/.venv/bin/python" -I \
  "$GT_BACKUP_RELEASE_ROOT/deploy/validate-backup.py" extract "$archive" "$target"
chown -R gremlin-town:gremlin-town "$target"
chmod -R u=rwX,g=,o= "$target"
echo "Restored and verified into isolation only. The live database is unchanged."
