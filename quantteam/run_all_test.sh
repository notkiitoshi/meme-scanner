#!/usr/bin/env bash
# End-to-end test of the paper-only quant team. Times each step; stops on first failure.
# Usage: ./run_all_test.sh [--skip-coingecko]
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-python3}
RADAR_FLAGS=${1:-}
step () {
  local name=$1; shift
  local t0=$(date +%s)
  echo "=== $name: $* ==="
  if "$@" > "logs/test_${name}.out" 2> "logs/test_${name}.err"; then
    echo "$name OK in $(( $(date +%s) - t0 ))s  (stdout: logs/test_${name}.out)"
  else
    rc=$?
    echo "$name FAILED (exit $rc) after $(( $(date +%s) - t0 ))s"; tail -20 "logs/test_${name}.err"; tail -5 "logs/test_${name}.out"; exit $rc
  fi
}
step radar  $PY radar.py $RADAR_FLAGS
step lab    $PY lab.py
step forge  $PY forge.py
step bot    $PY bot.py
step shield $PY shield.py
step brief  $PY brief.py
step weekly $PY weekly.py
echo "ALL STEPS PASSED"
