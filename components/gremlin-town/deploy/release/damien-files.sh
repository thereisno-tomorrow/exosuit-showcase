#!/usr/bin/env bash
# damien-files.sh TAG TAR MAP PRE
# Run on the hub as root:  ssh operator@hub.example.invalid 'bash -s' -- TAG TAR MAP PRE < damien-files.sh
# Replace Diablo's files from a `git archive` tar, exactly as MAP says. MAP lines (made on the PC by
# damien-map.py from both install manifests) are "<sha256> <path in tar> <destination under /srv/hermes-example>".
# PRE is a `sha256sum -c` file (paths relative to /srv/hermes-example) of what must be installed now.
# Backs up every file it replaces, installs as uid 1000 mode 0644, and checks each new hash.
# Does not restart hermes-wa: the caller checks Diablo is idle and restarts.
set -euo pipefail
TAG="$1"; TAR="$2"; MAP="$3"; PRE="$4"
H=/srv/hermes-example
B="$H/gt-plugin-backup-20260927-$TAG"
cd "$H"
sha256sum -c --quiet "$PRE" || { echo "STOP: installed files are not the expected preimages"; exit 2; }
[ ! -e "$B" ] || { echo "STOP: $B exists"; exit 2; }
install -d -o 1000 -g 1000 -m 0700 "$B"
cp "$PRE" "$B/preimages.txt"
S=$(mktemp -d)
tar -xf "$TAR" -C "$S"
while read -r sha src dest; do
  [ -n "$sha" ] || continue
  [ -f "$S/$src" ] || { echo "STOP: $src not in the tar"; exit 3; }
  got=$(sha256sum "$S/$src" | cut -c1-64)
  [ "$got" = "$sha" ] || { echo "STOP: $src in the tar is $got, expected $sha"; exit 3; }
  if [ -e "$dest" ]; then
    mkdir -p "$B/$(dirname "$dest")"
    cp -p "$dest" "$B/$dest"
  fi
  install -o 1000 -g 1000 -m 0644 -D "$S/$src" "$dest"
  now=$(sha256sum "$dest" | cut -c1-64)
  [ "$now" = "$sha" ] || { echo "STOP: $dest is $now after install, expected $sha"; exit 4; }
  echo "installed $dest ($sha)"
done < "$MAP"
rm -rf "$S"
awk '{print $1"  "$3}' "$MAP" > "$B/postimages.txt"
chown -R 1000:1000 "$B"
echo "INSTALLED: backup in $B (restore: copy each file back from there, then docker restart hermes-wa)"
