#!/usr/bin/env bash
# Measure the (channel MCS ceiling x buffer cap) grid on the running single-UE pipeline.
# For each channel level, caps are stepped UP (small -> large) to avoid big downward cuts.
#   bash buffer-control-experiments/grid_B.sh [DWELL_S]        -> results/grid_B_<time>.csv
set -u
E="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DWELL=${1:-30}; SKIP=10
CHANNELS="normal mcs16 mcs10 mcs5"
CAPS="32k 64k 96k 128k 256k"
OUT=$E/results/grid_B_$(date +%Y%m%d_%H%M%S).csv
mkdir -p "$E/results"
for c in $CAPS; do bash "$E/bufcap.sh" list | grep -qw "$c" || bash "$E/bufcap.sh" build "$c" >/dev/null; done
measure() {  # measure START END -> "tput,rtt_p50,rtt_p95,queue_kB"
  python3 - "$1" "$2" <<'PY'
import json,sys,urllib.request,urllib.parse
a,b=float(sys.argv[1]),float(sys.argv[2]); w="%ds"%(b-a)
def q(e):
    u="http://localhost:30491/api/v1/query?"+urllib.parse.urlencode({"query":e,"time":b})
    r=json.load(urllib.request.urlopen(u))["data"]["result"]
    return round(float(r[0]["value"][1]),1) if r else ""
print(",".join(str(x) for x in [
  q('avg_over_time(flow_tput_mbps{flow="bulk"}[%s])'%w),
  q('quantile_over_time(0.5,flow_rtt_ms{flow="video"}[%s])'%w),
  q('quantile_over_time(0.95,flow_rtt_ms{flow="video"}[%s])'%w),
  round(q('avg_over_time(upt_rlc_buffer_bytes{bearer="DRB1",ue="0"}[%s])'%w)/1e3,1)]))
PY
}
echo "channel,cap,download_mbps,video_rtt_p50_ms,video_rtt_p95_ms,rlc_queue_kB" | tee "$OUT"
bash "$E/bufcap.sh" load 256k >/dev/null
bash "$E/traffic_A.sh" start >/dev/null; sleep 25
for ch in $CHANNELS; do
  if [ "$ch" = normal ]; then bash "$E/badchan.sh" unload >/dev/null; else bash "$E/badchan.sh" load "$ch" >/dev/null; fi
  for cap in $CAPS; do
    bash "$E/bufcap.sh" load "$cap" >/dev/null
    s=$(date +%s); sleep "$DWELL"
    echo "$ch,$cap,$(measure $((s + SKIP)) $(date +%s))" | tee -a "$OUT"
  done
done
bash "$E/badchan.sh" unload >/dev/null; bash "$E/bufcap.sh" off >/dev/null; bash "$E/traffic_A.sh" stop >/dev/null
echo "-> $OUT"
