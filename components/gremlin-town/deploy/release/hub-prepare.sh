#!/usr/bin/env bash
# hub-prepare.sh NEW PREV COMMIT GT_SHA BC_SHA
# Run on the hub as root:  ssh operator@hub.example.invalid 'bash -s' -- NEW PREV COMMIT GT_SHA BC_SHA < hub-prepare.sh
# Steps 1-2 of the speed-03 checklist (components/gremlin-town/docs/operations.md), parameterized:
# pre-change copies, wheel hash check, the hard-link release with both packages reinstalled, entrypoint
# checks, and the backup validator on a private snapshot. Changes nothing live: `current` is not touched.
# Wheels must already be in /root/<tag>-staging/ (tag = NEW without its date, e.g. speed-04).
set -euo pipefail
NEW="$1"; PREV="$2"; COMMIT="$3"; GT_SHA="$4"; BC_SHA="$5"
R=/opt/gremlin-town/releases
TAG="${NEW#*-}"
DAY="${NEW%%-*}"
STAGE="/root/$TAG-staging"
PRE="/root/pre-$TAG-$DAY"
GT_WHEEL=gremlin_town-0.1.0-py3-none-any.whl
BC_WHEEL=brain_core-0.4.0-py3-none-any.whl

[ "$(readlink /opt/gremlin-town/current)" = "$R/$PREV" ] || { echo "STOP: current is $(readlink /opt/gremlin-town/current), not $R/$PREV"; exit 2; }
[ ! -e "$R/$NEW" ] || { echo "STOP: $R/$NEW already exists"; exit 2; }
systemctl is-active --quiet gremlin-town.service || { echo "STOP: gremlin-town is not active"; exit 2; }

cd "$STAGE"
printf '%s  %s\n%s  %s\n' "$GT_SHA" "$GT_WHEEL" "$BC_SHA" "$BC_WHEEL" | sha256sum -c -

install -d -m 0700 "$PRE"
cp /etc/gremlin-town/config.toml /etc/gremlin-town/participants.toml /etc/gremlin-town/brain-policy.json "$PRE/"
cp /var/lib/gremlin-town-brain/decision-staging/brain-usage.sqlite3 "$PRE/"
for w in snapshot-stream.sh restore.sh; do
  cp /usr/local/lib/gremlin-town/$w "/usr/local/lib/gremlin-town/$w.pre-$TAG-$DAY"
done
echo "copies: $PRE and the two .pre-$TAG-$DAY wrappers"

cp -al "$R/$PREV" "$R/$NEW"
install -d -o root -g root -m 0755 "$R/$NEW/$TAG-artifact"
install -m 0644 "$GT_WHEEL" "$BC_WHEEL" "$R/$NEW/$TAG-artifact/"
printf 'source_commit=%s\nbuilt_from=git -c core.autocrlf=false archive %s\n' "$COMMIT" "$COMMIT" > "$R/$NEW/$TAG-artifact/PROVENANCE.txt"
export UV_CACHE_DIR=/var/cache/gremlin-town/uv
/opt/gremlin-town/toolchain/uv pip install --python "$R/$NEW/.venv/bin/python" --no-deps \
  --reinstall-package brain-core "$R/$NEW/$TAG-artifact/$BC_WHEEL" 2>&1 | tail -2
/opt/gremlin-town/toolchain/uv pip install --python "$R/$NEW/.venv/bin/python" --no-deps \
  --reinstall-package gremlin-town "$R/$NEW/$TAG-artifact/$GT_WHEEL" 2>&1 | tail -2
chown -R root:gremlin-town "$R/$NEW"
chmod -R u=rwX,g=rX,o= "$R/$NEW"
"$R/$NEW/.venv/bin/brain" --version
"$R/$NEW/.venv/bin/gremlin-town" --help >/dev/null && echo "gremlin-town entrypoint OK"

umask 077
sudo -u gremlin-town /usr/local/lib/gremlin-town/snapshot-stream.sh > "/root/$TAG-precheck.tar"
SIZE=$(stat -c %s "/root/$TAG-precheck.tar")
install -d -o gremlin-town -g gremlin-town -m 0700 "/tmp/$TAG-precheck"
sudo -u gremlin-town tar -xf - -C "/tmp/$TAG-precheck" < "/root/$TAG-precheck.tar"
set +e
sudo -u gremlin-town "$R/$NEW/.venv/bin/python" -I "$R/$NEW/deploy/validate-backup.py" directory "/tmp/$TAG-precheck"
RC=$?
set -e
rm -rf "/tmp/$TAG-precheck" "/root/$TAG-precheck.tar"
[ "$RC" = 0 ] || { echo "STOP: validator refused (exit $RC); nothing switched"; exit 3; }
echo "validator: exit 0 on a $SIZE-byte snapshot"
echo "READY TO SWITCH: $NEW (previous $PREV)"
