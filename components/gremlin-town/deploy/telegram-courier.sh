#!/usr/bin/env bash
# telegram-courier.sh SPOOL_DIR
#
# Posts the 06:00 review and daytime returns to Nic's Telegram (step E spec §2; Proposed - Nic
# to confirm). Installed as /usr/local/lib/gremlin-town/telegram-courier.sh (root:root 0755) and
# run as root after each review or moments run: ExecStartPost=+... in both units.
#
# Why: Diablo's Hermes lets no agent send a message on its own. Gremlin Town writes each
# message's exact text to SPOOL_DIR/<client_operation_id>.txt once its request to Diablo exists
# ([review] telegram_spool_dir). This script hands each file, oldest first, to Hermes's own
# command inside Diablo's container:
#   docker exec -i -u 1000 hermes-wa hermes send --to telegram --json --file - < FILE
# No model is involved and nothing here reads a secret: the container sends with its own
# Telegram settings. `docker exec` is the only thing this script reaches.
#
# Sent ("success": true in the JSON result): the file moves to SPOOL_DIR/sent/ and one line is
# appended to SPOOL_DIR/sent/ledger.tsv: UTC time, file name, Telegram message ID ("unknown" if
# Hermes gave none). Not sent: the file stays for the next run (the moments unit runs every 10
# minutes), the reason goes to the journal, and the script exits 1 once every file is tried.
# Sent files older than 30 days are deleted; the ledger is kept.
#
# Overridable for tests: COURIER_DOCKER (docker), COURIER_CONTAINER (hermes-wa),
# COURIER_USER (1000), COURIER_TARGET (telegram), COURIER_PYTHON (python3),
# COURIER_SEND_TIMEOUT (120 seconds), COURIER_KEEP_DAYS (30).
set -euo pipefail

spool=${1:?usage: telegram-courier.sh SPOOL_DIR}
docker=${COURIER_DOCKER:-docker}
container=${COURIER_CONTAINER:-hermes-wa}
user=${COURIER_USER:-1000}
target=${COURIER_TARGET:-telegram}
python=${COURIER_PYTHON:-python3}
send_timeout=${COURIER_SEND_TIMEOUT:-120}
keep_days=${COURIER_KEEP_DAYS:-30}

log() { printf 'telegram-courier: %s\n' "$*" >&2; }
# Hermes's errors could quote a Telegram bot token (<digits>:<secret>); never log one.
redact() { sed -E 's/[0-9]{6,}:[A-Za-z0-9_-]{20,}/<redacted>/g'; }

# Prints the Telegram message ID when hermes send's JSON says "success": true; otherwise
# fails with the reason on stderr. Takes the whole output, or else its last JSON line.
read_result() {
  "$python" -c '
import json, sys
raw = sys.stdin.read()
result = None
for candidate in [raw, *reversed(raw.splitlines())]:
    try:
        value = json.loads(candidate)
    except ValueError:
        continue
    if isinstance(value, dict):
        result = value
        break
if result is None:
    sys.exit("no JSON result from hermes send")
if result.get("success") is not True:
    sys.exit("hermes send: " + str(result.get("error") or "success is not true")[:300])
message_id = result.get("message_id")
sys.stdout.write("unknown" if message_id in (None, "") else str(message_id))
'
}

[[ -d $spool ]] || { log "spool folder $spool does not exist"; exit 1; }

# One courier at a time: the review and moments timers both fire at 22:00 UTC.
exec 9>>"$spool/.courier.lock"
flock -w 300 9 || { log "another courier run still holds $spool/.courier.lock"; exit 1; }

sent=$spool/sent
# An explicit mode: the units' UMask=0077 would make it 0700, and Gremlin Town (as
# gremlin-town) looks in sent/ so that a replayed message is never spooled twice.
mkdir -p -m 0755 "$sent"

errors=$(mktemp)
trap 'rm -f "$errors"' EXIT

failed=0
while IFS= read -r -d '' name; do
  file=$spool/$name
  if output=$(timeout "$send_timeout" "$docker" exec -i -u "$user" "$container" \
      hermes send --to "$target" --json --file - <"$file" 2>"$errors"); then
    status=0
  else
    status=$?
  fi
  if message_id=$(printf '%s' "$output" | read_result 2>>"$errors"); then
    mv -f -- "$file" "$sent/$name"
    printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$message_id" >>"$sent/ledger.tsv"
    log "sent $name as Telegram message $message_id"
    [[ $status -eq 0 ]] || log "note: hermes send exited $status for $name but reported success"
  else
    failed=1
    log "not sent, will retry: $name (exit $status): $(redact <"$errors" | tail -n 3 | paste -sd ';' -)"
  fi
done < <(find "$spool" -maxdepth 1 -type f -name '*.txt' ! -name '.*' -printf '%T@\t%f\0' |
  sort -z -n | cut -z -f2-)

find "$sent" -maxdepth 1 -type f -name '*.txt' -mtime +"$keep_days" -delete
exit "$failed"
