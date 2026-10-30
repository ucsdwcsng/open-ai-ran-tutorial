#!/usr/bin/env bash
# Run one or more experiment configs, each on a FRESH single-UE pipeline (runbook rule:
# restart gNB + UE for every test). Results land in results/<name>[_<tag>]/.
#
#   bash buffer-control-experiments/run.sh configs/expA_cubic_down.json [more.json ...]
#   NO_SETUP=1 bash buffer-control-experiments/run.sh configs/x.json    # reuse the running pipeline
#   TAG=r2 bash buffer-control-experiments/run.sh configs/x.json         # results/<name>_r2
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; HR="$(dirname "$HERE")"
for c in "$@"; do
  name=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['name'])" "$c")
  out="$HERE/results/${name}${TAG:+_$TAG}"
  echo "################ $name -> $out ($(date +%H:%M:%S))"
  if [ -z "${NO_SETUP:-}" ]; then
    bash "$HR/scripts/setup_bufctl_1ue_demo.sh" 2>&1 | grep -E "UE_IP=|SETUP DONE|!!" | sort -u
  fi
  bash "$HERE/bufcap.sh" list | grep -q off || bash "$HERE/bufcap.sh" build
  MPLCONFIGDIR=/tmp/mpl python3 "$HERE/runner.py" "$c" --out "$out" 2>&1 | grep -v Matplotlib
done
