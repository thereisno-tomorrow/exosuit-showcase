#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: test_wheel_linux.sh REQUIREMENTS WHEEL PROJECT" >&2
  exit 2
fi

requirements=$1
wheel=$2
project=$3
workdir=$(mktemp -d /tmp/brain-wheel-linux.XXXXXX)
uv=${BRAIN_UV:-$(command -v uv || true)}
if [[ -z "$uv" || ! -x "$uv" ]]; then
  echo "a pinned Linux uv binary is required via BRAIN_UV" >&2
  exit 61
fi
"$uv" venv --python python3 "$workdir/venv"
python="$workdir/venv/bin/python"

"$uv" pip install --python "$python" --require-hashes -r "$requirements"
"$uv" pip install --python "$python" --no-deps "$wheel"
"$python" -c 'import brain_core, pathlib, sys; loaded=pathlib.Path(brain_core.__file__).resolve(); prefix=pathlib.Path(sys.prefix).resolve(); assert loaded.is_relative_to(prefix), (loaded, prefix); print(loaded)'
cd "$project"
if [[ ${BRAIN_SKIP_SUITE:-0} != 1 ]]; then
  "$python" -m pytest --cov=brain_core --cov-branch --cov-fail-under=100 -q
  "$python" -m brain_core --version
fi
if [[ ${BRAIN_VERIFY_UPGRADE:-0} == 1 ]]; then
  "$python" "$project/scripts/verify_upgrade_rollback.py"
fi
if [[ ${BRAIN_RUN_BENCHMARK:-0} == 1 ]]; then
  "$python" "$project/scripts/run_phase1_benchmarks.py" \
    --runs 20 \
    --label ubuntu-24.04-python-3.12-installed-wheel \
    --output "$project/docs/receipts/phase1-benchmark-linux.json"
fi
printf 'temp=%s\n' "$workdir"
