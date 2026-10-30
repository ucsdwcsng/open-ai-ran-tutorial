#!/usr/bin/env bash
# Start/stop the per-flow monitor (flowmon.py) in the background.
#   bash buffer-control-experiments/flowmon.sh start|stop|status
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-status}" in
  start)
    pgrep -f "[f]lowmon.py" >/dev/null && { echo "flowmon already running"; exit 0; }
    setsid python3 -u "$HERE/flowmon.py" >/tmp/flowmon.log 2>&1 </dev/null & disown
    sleep 2; echo "flowmon: $(head -1 /tmp/flowmon.log)" ;;
  stop)
    pkill -f "[f]lowmon.py" && echo "flowmon stopped" || echo "flowmon not running" ;;
  status)
    pgrep -af "[f]lowmon.py" >/dev/null && echo "flowmon running" || echo "flowmon not running" ;;
esac
