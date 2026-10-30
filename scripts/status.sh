#!/usr/bin/env bash
# Is the demo up, and what is in force?  Read-only: safe to run any time.
#   bash scripts/status.sh
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS=ran; POD=srs-gnb-du1-0
VM=http://localhost:30491
k() { timeout "${3:-20}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2" 2>/dev/null; }

# The pod mounts /apps and /codelets as hostPath from whichever REPO_ROOT the Helm
# release was installed with. If that is not THIS clone, the xApp reads someone
# else's files and commands written here are silently ignored.
POD_APPS=$(timeout 15 kubectl get statefulset -n $NS jrtc \
  -o jsonpath='{range .spec.template.spec.volumes[*]}{.name}{"="}{.hostPath.path}{"\n"}{end}' 2>/dev/null \
  | sed -n 's/^apps=//p')
MY_APPS="$HR/jrtc-apps/jrtc_apps"
if [ -n "$POD_APPS" ] && [ "$POD_APPS" != "$MY_APPS" ]; then
  echo "!! TREE MISMATCH"
  echo "   jrtc-0 reads /apps from : $POD_APPS"
  echo "   this clone is           : $MY_APPS"
  echo "   Commands from this clone's bufctl_cli.py will be IGNORED (the xApp reads the"
  echo "   path above). Either run the CLI from that tree, or re-install the Helm release"
  echo "   with your own REPO_ROOT (see README section 4)."
  echo
fi

GNB_UP=$(k ocudujbpf 'pgrep -c -x gnb' 15 | tr -d '[:space:]'); GNB_UP=${GNB_UP:-0}

echo "== radio =="
k ocudujbpf 'echo "  gnb:    procs=$(pgrep -c -x gnb)  log=$(du -h /tmp/gnb.log 2>/dev/null | cut -f1)"'
UEIP=$(k durue1 'ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o "10\.45\.0\.[0-9]*" | head -1' 15 | tr -d '[:space:]')
UELIVE=$(k durue1 'ps -eo stat,comm | grep nr-uesoftmodem | grep -v Z | grep -vc grep' 15 | tr -d '[:space:]')
echo "  UE:     ip=${UEIP:-none}  live procs=${UELIVE:-0}"
k grbroker 'echo "  broker: procs=$(pgrep -c -x python3)  (single-UE direct ZMQ needs none)"'

echo "== traffic =="
UPF=$(timeout 15 kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name 2>/dev/null | grep "^open5gs-upf-")
[ -n "$UPF" ] && timeout 20 kubectl exec -n open5gs "$UPF" -c open5gs-upf -- bash -c \
  'echo "  UPF iperf3 live=$(ps -eo stat,comm | grep iperf3 | grep -v Z | grep -vc grep)  (1 = idle server, 2 = flow running)"' 2>/dev/null
UEIPERF=$(k durue1 'ps -eo stat,comm | grep iperf3 | grep -v Z | grep -vc grep' 15 | tr -d '[:space:]'); UEIPERF=${UEIPERF:-0}
echo "  UE  iperf3 live=$UEIPERF"
if [ "$UEIPERF" -gt 0 ]; then
  k durue1 'tail -1 /tmp/iperf_manual.log 2>/dev/null | sed "s/^/          now: /"'
else
  k durue1 'tail -1 /tmp/iperf_manual.log 2>/dev/null | sed "s/^/          (last line of a FINISHED flow: /;s/$/)/"'
fi

echo "== codelets =="
if [ "$GNB_UP" -gt 0 ]; then
  k ocudujbpf 'grep -ao "Codelet created and loaded successfully: [a-z_]*" /tmp/gnb.log 2>/dev/null | sed "s/.*: /    /" | sort -u | sed "1i\  registered since this gNB started:"'
  k ocudujbpf 'L=$(grep -ac "Codelet created and loaded successfully: rlc_ctrl" /tmp/gnb.log); R=$(grep -ac "Codelet rlc_ctrl removed successfully" /tmp/gnb.log);
    echo "  bufctl (rlc_ctrl): loads=$L removes=$R -> $([ "$L" = "$R" ] && echo "NOT attached (good when idle)" || echo "ATTACHED: runs on EVERY DL packet")"'
else
  echo "  (gNB is stopped -- nothing is attached)"
fi
echo -n "  bufctl_cli running: "; pgrep -af bufctl_cli.py || echo no

echo "== buffer limits =="
if [ "$GNB_UP" -gt 0 ]; then
  echo "  (live values; a limit applies to the bearer's next DL packet)"
else
  echo "  (gNB is STOPPED: the values below are only the last report on record."
  echo "   Limits live in the gNB's queue, so a restart returns every bearer to its config.)"
fi
python3 - "$HR/jrtc-apps/jrtc_apps/bufctl/state.json" <<'PY'
import json,sys,time
try: st=json.load(open(sys.argv[1]))
except Exception: print("    (no state.json yet -- run bufctl_cli.py and 'scan')"); raise SystemExit
for b in st.get("bearers",[]):
    tag = "" if (b["cur_bytes"],b["cur_sdus"])==(b["cfg_bytes"],b["cfg_sdus"]) else "   <- differs from config"
    print("    ue=%s drb=%s  cur=%sB/%sSDU  cfg=%sB/%sSDU  (%s, reported %.0fs ago)%s"%(
        b["ue"],b["drb"],b["cur_bytes"],b["cur_sdus"],b["cfg_bytes"],b["cfg_sdus"],
        b["reason"], time.time()-b["t"], tag))
PY

echo "== telemetry =="
# NOTE: an instant query returns the EVALUATION time in value[0], not the sample's
# own timestamp, so age must come from timestamp() or every value looks 1s old.
python3 - "$VM" <<'PY'
import json,sys,urllib.request,urllib.parse
VM=sys.argv[1]
def q(expr):
    u=VM+"/api/v1/query?"+urllib.parse.urlencode({"query":expr})
    try:
        r=json.load(urllib.request.urlopen(u,timeout=8))["data"]["result"]
        return float(r[0]["value"][1]) if r else None
    except Exception:
        return None
for m,sel in [("upt_rlc_buffer_pkts",'{bearer="DRB1"}'),
              ("upt_rlc_buffer_pkts_max",'{bearer="DRB1"}'),
              ("upt_rlc_queue_latency_ms",'{bearer="DRB1"}'),
              ("tcp_rtt_ms","")]:
    val=q(m+sel)
    age=q("time() - timestamp(%s%s)"%(m,sel))
    if val is None:
        print("  %-26s no samples in the last 5 min (no DL traffic)"%m)
    elif age is not None and age > 30:
        print("  %-26s %-10s STALE: %.0fs old"%(m,round(val,1),age))
    else:
        print("  %-26s %-10s (%.0fs old)"%(m,round(val,1),age if age is not None else -1))
PY
echo -n "  grafana: "; curl -s -o /dev/null -w "%{http_code}" http://localhost:30490/api/health 2>/dev/null
echo "  -> http://localhost:30490/d/upt-userplane"
