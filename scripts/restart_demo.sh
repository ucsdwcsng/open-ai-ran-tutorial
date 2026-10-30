#!/usr/bin/env bash
# Convenience: setup + traffic in one command (the original one-shot demo).
#   bash scripts/restart_demo.sh [seconds]        (default 3600)
#
# The two halves are also usable on their own, which is what you want if you
# intend to drive your own traffic:
#   bash scripts/setup_bufctl_1ue_demo.sh                    # pipeline only, no data
#   bash scripts/traffic.sh start --cc bbr ...    # whatever data you like
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECS="${1:-3600}"

bash "$HR/scripts/setup_bufctl_1ue_demo.sh" || exit 1

echo
echo "################ traffic ################"
bash "$HR/scripts/traffic.sh" start --time "$SECS" 2>&1 | tail -3
sleep 25
echo "  telemetry check:"
for m in upt_rlc_buffer_pkts upt_rlc_queue_latency_ms; do
  printf "    %-24s " "$m"
  curl -s -m 8 "http://localhost:30491/api/v1/query?query=$m" | python3 -c '
import json,sys
r=[x for x in json.load(sys.stdin).get("data",{}).get("result",[]) if x["metric"].get("bearer") in (None,"DRB1")]
print(round(float(r[0]["value"][1]),1) if r else "NO SERIES -- telemetry not flowing")' 2>/dev/null
done
echo
echo "UP. Control: python3 $HR/jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py"
