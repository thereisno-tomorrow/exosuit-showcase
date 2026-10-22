#!/usr/bin/env bash
set -euo pipefail

# Pure renderer: installed backup tooling outlives application pointer changes.
# Only apply.sh supplies this root; it is never taken from the caller environment.
render_backup_wrapper() {
  local source="$1" release_root="$2" line matches=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      'readonly GT_BACKUP_RELEASE_ROOT='*)
        printf 'readonly GT_BACKUP_RELEASE_ROOT=%q\n' "$release_root"
        matches=$((matches + 1)) ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$source"
  [[ "$matches" -eq 1 ]] || { echo "backup release assignment missing or duplicated" >&2; return 1; }
}

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
[[ "${GT_APPROVED_APPLY:-}" == "YES" ]] || fail "set GT_APPROVED_APPLY=YES after approval"
[[ "${EUID}" -eq 0 ]] || fail "run as root"
[[ $# -eq 2 ]] || fail "usage: apply.sh RELEASE_DIR RELEASE_ID"

release_source="$(readlink -f "$1")"
release_id="$2"
[[ -d "$release_source" ]] || fail "release directory not found"
[[ "$release_id" =~ ^[0-9A-Za-z._-]{1,64}$ ]] || fail "unsafe release id"
[[ "$release_id" != . && "$release_id" != .. ]] || fail "unsafe release id"
[[ -f "$release_source/uv.lock" && -f "$release_source/pyproject.toml" ]] || fail "locked Python project required"
[[ -f "$release_source/RELEASE-MANIFEST.sha256" ]] || fail "root-prepared RELEASE-MANIFEST.sha256 required"

while IFS= read -r release_path; do
  [[ "$release_path" != *[$'\t\r\n ']* ]] || fail "release artifact paths may not contain whitespace: $release_path"
  case "/$release_path/" in
    */.git/*|*/.venv/*|*/venv/*|*/__pycache__/*|*/.pytest_cache/*|*/.mypy_cache/*|*/.ruff_cache/*|*/tests/*|*/test/*)
      fail "forbidden development/cache path in release artifact: $release_path" ;;
  esac
  case "$release_path" in
    deploy/service.env.example) ;;
    *.pyc|*.pyo|*.db|*.sqlite|*.sqlite3|*.pem|*.key|*.token|*.env|*.env.*)
      fail "forbidden runtime/secret-like file in release artifact: $release_path" ;;
  esac
  case "$release_path" in
    pyproject.toml|uv.lock|RELEASE-MANIFEST.sha256|src|src/*|adapters|adapters/*|deploy|deploy/*|docs|docs/*) ;;
    *) fail "unexpected path in clean release artifact: $release_path" ;;
  esac
done < <(find "$release_source" -mindepth 1 -printf '%P\n')
[[ -z "$(find "$release_source" -mindepth 1 ! -type d ! -type f -print -quit)" ]] || fail "release artifact contains a symlink or special file"
(
  cd "$release_source"
  mapfile -t manifest_paths < <(awk 'NF == 2 {sub(/^\*/, "", $2); print $2}' RELEASE-MANIFEST.sha256 | LC_ALL=C sort)
  mapfile -t artifact_paths < <(find . -type f ! -name RELEASE-MANIFEST.sha256 -printf '%P\n' | LC_ALL=C sort)
  [[ ${#manifest_paths[@]} -eq ${#artifact_paths[@]} ]] || fail "release manifest does not cover every artifact file"
  for index in "${!artifact_paths[@]}"; do
    [[ "${manifest_paths[$index]}" == "${artifact_paths[$index]}" ]] || fail "release manifest path set differs from artifact"
  done
  sha256sum --check --strict RELEASE-MANIFEST.sha256
)

install -d -m 0755 /opt/gremlin-town/python /opt/gremlin-town/bin /opt/gremlin-town/toolchain
install -d -m 0755 /opt/gremlin-town/releases
[[ ! -e "/opt/gremlin-town/releases/$release_id" ]] || fail "release already exists"

if ! getent group gremlin-town >/dev/null; then groupadd --system gremlin-town; fi
if ! getent passwd gremlin-town >/dev/null; then
  useradd --system --gid gremlin-town --home-dir /var/lib/gremlin-town --create-home --shell /usr/sbin/nologin gremlin-town
fi
install -d -o root -g gremlin-town -m 0750 /etc/gremlin-town
install -d -o gremlin-town -g gremlin-town -m 0750 /var/lib/gremlin-town
install -d -o gremlin-town -g gremlin-town -m 0700 /var/lib/gremlin-town/backup-staging

uv_version=0.11.21
uv_asset=uv-x86_64-unknown-linux-gnu.tar.gz
uv_expected_sha256=8c88519b0ef0af9801fcdee419bbb12116bd9e6b18e162ae093c932d8b264050
uv_base_url="https://github.com/astral-sh/uv/releases/download/${uv_version}"
uv_tmp="$(mktemp -d)"
case "$uv_tmp" in /tmp/*) ;; *) fail "unsafe uv temporary path" ;; esac
trap 'rm -rf -- "$uv_tmp"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 \
  --output "$uv_tmp/$uv_asset" "$uv_base_url/$uv_asset"
curl --fail --location --proto '=https' --tlsv1.2 \
  --output "$uv_tmp/$uv_asset.sha256" "$uv_base_url/$uv_asset.sha256"
uv_actual_sha256="$(sha256sum "$uv_tmp/$uv_asset" | awk '{print $1}')"
mapfile -t uv_official_sums < <(awk 'NF {print $1}' "$uv_tmp/$uv_asset.sha256")
[[ ${#uv_official_sums[@]} -eq 1 ]] || fail "uv official checksum sidecar must contain exactly one entry"
uv_official_sha256="${uv_official_sums[0]}"
[[ "$uv_actual_sha256" == "$uv_expected_sha256" ]] || fail "uv archive checksum mismatch"
[[ "$uv_official_sha256" == "$uv_expected_sha256" ]] || fail "uv official checksum differs from reviewed pin"
tar -xzf "$uv_tmp/$uv_asset" -C "$uv_tmp"
mapfile -t uv_bins < <(find "$uv_tmp" -type f -name uv -print)
mapfile -t uvx_bins < <(find "$uv_tmp" -type f -name uvx -print)
[[ ${#uv_bins[@]} -eq 1 && ${#uvx_bins[@]} -eq 1 ]] || fail "uv archive did not contain exactly one uv and one uvx"
install -o root -g root -m 0755 "${uv_bins[0]}" /opt/gremlin-town/toolchain/uv
install -o root -g root -m 0755 "${uvx_bins[0]}" /opt/gremlin-town/toolchain/uvx
read -r uv_name uv_semver _ < <(/opt/gremlin-town/toolchain/uv --version)
[[ "$uv_name" == uv && "$uv_semver" == 0.11.21 ]] || fail "unexpected installed uv semantic version: $uv_name $uv_semver"

cp -a "$release_source" "/opt/gremlin-town/releases/$release_id"
chown -R root:gremlin-town "/opt/gremlin-town/releases/$release_id"
chmod -R u=rwX,g=rX,o= "/opt/gremlin-town/releases/$release_id"
(
  cd "/opt/gremlin-town/releases/$release_id"
  export UV_CACHE_DIR=/var/cache/gremlin-town/uv
  export UV_PYTHON_INSTALL_DIR=/opt/gremlin-town/python
  export UV_PYTHON_BIN_DIR=/opt/gremlin-town/bin
  install -d -o gremlin-town -g gremlin-town -m 0750 "$UV_CACHE_DIR"
  /opt/gremlin-town/toolchain/uv python install 3.13.5
  /opt/gremlin-town/toolchain/uv sync --frozen --no-dev --no-editable --python 3.13.5
  .venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 13), sys.version'
  [[ -x .venv/bin/gremlin-town ]] || fail "required console entrypoint .venv/bin/gremlin-town missing"
)
ln -sfn "/opt/gremlin-town/releases/$release_id" /opt/gremlin-town/current

install -d -m 0755 /usr/local/lib/gremlin-town
installed_release="/opt/gremlin-town/releases/$release_id"
install -o root -g root -m 0755 "$installed_release/deploy/sqlite_backup.py" /usr/local/lib/gremlin-town/sqlite_backup.py
for wrapper in snapshot-stream.sh restore.sh; do
  render_backup_wrapper "$installed_release/deploy/$wrapper" "$installed_release" > "$uv_tmp/$wrapper"
  install -o root -g root -m 0755 "$uv_tmp/$wrapper" "/usr/local/lib/gremlin-town/$wrapper"
done
install -o root -g root -m 0644 "$installed_release/deploy/gremlin-town.service" /etc/systemd/system/gremlin-town.service

systemctl daemon-reload
printf '%s\n' "Files installed. Before start: install reviewed config, participants, and credentials; then run:" \
  "  systemctl enable --now gremlin-town.service"
