#!/usr/bin/env bash
# Run every expD config REPS times: one fresh bring-up per repeat block, configs in a new
# random order each block. Results: results/expD_<scenario>_<policy>_r<k>/
#   bash buffer-control-experiments/run_expD.sh [REPS]      (resumable: finished runs are skipped)
set -u
E="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; HR="$(dirname "$E")"
REPS=${1:-3}
export PDCP_DISCARD_MS=-1   # these runs need the discard timer off (see EXPERIMENTS.md)
for c in 203k 289k 64k 128k 735k 1470k 2940k 5880k dyn; do
  bash "$E/bufcap.sh" list | tr ' ' '\n' | grep -qx "$c" || bash "$E/bufcap.sh" build "$c" | tail -1
done
for r in $(seq 1 "$REPS"); do
  todo=""
  for c in $(ls "$E"/configs/expD_*.json | shuf); do   # resume: skip runs that already finished
    n=$(basename "$c" .json)
    [ -f "$E/results/${n}_r$r/summary.json" ] || todo="$todo $c"
  done
  [ -z "$todo" ] && { echo "repeat $r: already complete"; continue; }
  echo "################ repeat $r: fresh bring-up ($(date +%H:%M)), $(echo $todo | wc -w) runs to do"
  bash "$HR/scripts/setup_bufctl_1ue_demo.sh" 2>&1 | grep -E "UE_IP=|!!" | sort -u
  for c in $todo; do
    TAG=r$r NO_SETUP=1 bash "$E/run.sh" "$c" 2>&1 | grep -E "####|WINDOW|!!"
  done
done
echo "ALL DONE $(date +%H:%M)"
