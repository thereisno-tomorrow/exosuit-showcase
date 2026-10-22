#!/usr/bin/env bash
# hub-switch.sh NEW PREV
# Run on the hub as root:  ssh operator@hub.example.invalid 'bash -s' -- NEW PREV < hub-switch.sh
# Step 3 of the speed-03 checklist: point `current` at NEW, restart, wait for ready, show listeners and
# journal warnings, then re-pin both backup wrappers (line 7) and prove only that line changed.
# If ready never comes, it rolls back to PREV by itself and says so.
set -euo pipefail
NEW="$1"; PREV="$2"
R=/opt/gremlin-town/releases
TAG="${NEW#*-}"
DAY="${NEW%%-*}"
[ -d "$R/$NEW" ] || { echo "STOP: $R/$NEW missing"; exit 2; }
[ "$(readlink /opt/gremlin-town/current)" = "$R/$PREV" ] || { echo "STOP: current is not $R/$PREV"; exit 2; }
SINCE=$(date -u '+%Y-%m-%d %H:%M:%S')
echo "switching at ${SINCE} UTC"
ln -sfn "$R/$NEW" /opt/gremlin-town/current
systemctl restart gremlin-town.service
CODE=000
for i in $(seq 1 45); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health/ready || true)
  [ "$CODE" = 200 ] && break
  sleep 1
done
if [ "$CODE" != 200 ]; then
  echo "NOT READY after 45 s (last $CODE): rolling back to $PREV"
  ln -sfn "$R/$PREV" /opt/gremlin-town/current
  systemctl restart gremlin-town.service
  sleep 5
  curl -s -o /dev/null -w 'after rollback health/ready %{http_code}\n' http://127.0.0.1:8765/health/ready || true
  exit 4
fi
echo "health/ready 200 after ${i}s; service $(systemctl is-active gremlin-town.service); current -> $(readlink /opt/gremlin-town/current)"
ss -ltn | grep -E ':(8765|8766|8769) ' | awk '{print "listening", $4}'
WARN=$(journalctl -u gremlin-town.service --since "$SINCE" --no-pager | grep -ciE 'warn|error|traceback' || true)
echo "journal warnings/errors since switch: $WARN"
[ "$WARN" = 0 ] || journalctl -u gremlin-town.service --since "$SINCE" --no-pager | grep -iE 'warn|error|traceback' | cut -c1-220 | tail -10
sed -i "s#^readonly GT_BACKUP_RELEASE_ROOT=$R/$PREV\$#readonly GT_BACKUP_RELEASE_ROOT=$R/$NEW#" \
  /usr/local/lib/gremlin-town/snapshot-stream.sh /usr/local/lib/gremlin-town/restore.sh
grep -n GT_BACKUP_RELEASE_ROOT= /usr/local/lib/gremlin-town/snapshot-stream.sh /usr/local/lib/gremlin-town/restore.sh
for w in snapshot-stream.sh restore.sh; do
  diff <(sed 7d /usr/local/lib/gremlin-town/$w) <(sed 7d /usr/local/lib/gremlin-town/$w.pre-$TAG-$DAY) >/dev/null \
    && echo "$w: only line 7 changed" || { echo "STOP: $w differs beyond line 7"; exit 5; }
done
echo "SWITCHED: $NEW"
