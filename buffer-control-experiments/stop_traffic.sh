#!/usr/bin/env bash
# Stop the experiment traffic: every iperf3 flow (traffic_A.sh, scripts/traffic.sh, runner.py)
# plus the flowmon per-flow monitor. Leaves codelets alone: use bufcap.sh off / radio.sh good.
#   bash buffer-control-experiments/stop_traffic.sh
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$HERE/flowmon.sh" stop
bash "$HERE/../scripts/stop_traffic.sh"
