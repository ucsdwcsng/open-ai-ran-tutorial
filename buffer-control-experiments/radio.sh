#!/usr/bin/env bash
# radio.sh - instructor's channel switch for experiment B (wraps badchan.sh; builds on first use).
#
#   bash buffer-control-experiments/radio.sh MCS                   # sustained: DL MCS <= MCS from now on
#   bash buffer-control-experiments/radio.sh MCS fade [ON PERIOD]  # fading: DL MCS <= MCS for ON ms every
#                                                           # PERIOD ms (default 100 400)
#   bash buffer-control-experiments/radio.sh trace NAME            # replay traces/NAME.csv (ped, veh), looping
#   bash buffer-control-experiments/radio.sh good                  # normal channel (unload)
#   bash buffer-control-experiments/radio.sh status
#
# MCS is 0..28; lower = worse channel. Measured download rates (no buffer cap):
#   normal ~32 Mbit/s | 20 -> ~20 | 16 -> ~16.5 | 10 -> ~9.5 | 5 -> ~4.6
#
# Examples:  radio.sh 10              radio.sh 16            radio.sh 5
#            radio.sh 10 fade         radio.sh 5 fade 50 200
# (Older forms still work: radio.sh bad [MCS], radio.sh fading [ON PERIOD] [MCS].)
set -uo pipefail
E="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

sustained() { bash "$E/badchan.sh" load "mcs$1"; }
fading() {  # fading MCS ON PERIOD
  local name="fade${2}of${3}"; [ "$1" != 10 ] && name="${name}_mcs$1"
  bash "$E/badchan.sh" load "$name"
}

case "${1:-status}" in
  good|normal|off) bash "$E/badchan.sh" unload ;;
  status)          bash "$E/badchan.sh" status ;;
  bad)             sustained "${2:-10}" ;;
  trace)           bash "$E/badchan.sh" load "trace_${2:?usage: radio.sh trace NAME}" ;;
  fading)          fading "${4:-10}" "${2:-100}" "${3:-400}" ;;
  [0-9]|[0-9][0-9])
    [ "$1" -le 28 ] || { echo "MCS must be 0..28"; exit 2; }
    case "${2:-}" in
      "")   sustained "$1" ;;
      fade) fading "$1" "${3:-100}" "${4:-400}" ;;
      *)    usage ;;
    esac ;;
  *) usage ;;
esac
