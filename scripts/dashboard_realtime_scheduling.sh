#!/usr/bin/env bash
# dashboard-realtime-scheduling: the jrtc xApp and Grafana dashboard that show what the DL scheduler does, per UE.
#
#   bash scripts/dashboard_realtime_scheduling.sh load      codelet + xApp, UE names, Grafana dashboard, the probes
#   bash scripts/dashboard_realtime_scheduling.sh unload    the xApp and its codelet (the probes keep running)
#   bash scripts/dashboard_realtime_scheduling.sh status
#   bash scripts/dashboard_realtime_scheduling.sh grafana   (re)provision the Grafana dashboard only
#   bash scripts/dashboard_realtime_scheduling.sh build     rebuild the codelet in the jbpf SDK image
#
# Grafana (http://localhost:30490): "EdgeRIC real-time scheduling" (uid rt-scheduling). On top, the UE identity
# table; below it, per UE: DL CQI | DL backlog / throughput received (TCP: tcpdump on the UPF; UDP: the UE receivers)
# | gNB throughput.
#
# load:
#   1. copies jrtc-apps/codelets/dashboard_realtime_scheduling into the gNB container and loads
#      jrtc-apps/jrtc_apps/dashboard_realtime_scheduling/deployment.yaml from jrtc-0: the rts_stats codelet on
#      mac_sched_slot_report buckets every slot like the upt codelets, the xApp writes to VictoriaMetrics.
#   2. UE names: scripts/ue_rnti_map.sh (ue<n> -> C-RNTI) and scripts/core_ue_info.sh (IMSI, IP, NGAP IDs).
#      Re-run both after the UEs re-attach.
#   3. the dashboard into the grafana-upt-dashboard ConfigMap (key rt-scheduling-dashboard.json).
#   4. the two host-side probes, unless they run already:
#      telemetry/tcp_probe.py   tcpdump in the UPF's tcpdumper container: TCP bytes the UEs ACK
#      telemetry/ue_rx_probe.py the UDP receivers' one-second logs in the UE container (traffic_nue.sh --udp): the UPF
#                               cannot see UDP delivery, as UDP has no ACKs
#
# Works with the gNB's own scheduler and with any EdgeRIC-RT muApp (scripts/edgeric_rt.sh, edgeric_muapp.sh).
# The xApp is the second Python xApp next to rt_bridge (one GIL): it handles ~8 messages/s and writes twice a
# second, so it should not cost the RT loop TTIs; check "TTIs run by the muApp" on the dashboard.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; GC=ocudujbpf
NAME=dashboard_realtime_scheduling
CODELETS="$HR/jrtc-apps/codelets/$NAME"
DEPLOY=/apps/$NAME/deployment.yaml
DASH="$HR/telemetry/rt-scheduling-dashboard.json"
DASH_KEY=rt-scheduling-dashboard.json
VM=http://localhost:30491
usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit "${1:-1}"; }
gx() { timeout "${2:-30}" kubectl exec -n $NS $POD -c $GC -- bash -c "$1"; }
jx() { timeout "${2:-60}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }

grafana() {
  python3 -c 'import json, sys; print(json.dumps({"data": {sys.argv[2]: open(sys.argv[1]).read()}}))' \
    "$DASH" "$DASH_KEY" > "${TMPDIR:-/tmp}/rts_dash_patch.json"
  kubectl patch configmap grafana-upt-dashboard -n $NS --type merge \
    --patch-file "${TMPDIR:-/tmp}/rts_dash_patch.json" >/dev/null \
    && echo "  grafana-upt-dashboard/$DASH_KEY updated (Grafana picks it up within 1-2 min)" \
    || echo "  !! could not patch the grafana-upt-dashboard ConfigMap"
}

tcp_probe() {
  if pgrep -f "[t]cp_probe.py" >/dev/null; then
    echo "  tcp_probe already running ($(pgrep -f '[t]cp_probe.py' | head -1))"; return
  fi
  local upf map
  upf=$(kubectl get pods -n open5gs --no-headers 2>/dev/null | awk '/upf/{print $1}' | head -1)
  [ -n "$upf" ] || { echo "  !! no UPF pod: tcp_probe not started"; return; }
  kubectl get pod -n open5gs "$upf" -o jsonpath='{.status.ephemeralContainerStatuses[*].name}' 2>/dev/null \
    | grep -qw tcpdumper || {
    echo "  adding the tcpdumper container to $upf"
    kubectl debug -n open5gs "$upf" --image=nicolaka/netshoot --image-pull-policy=IfNotPresent \
      -c tcpdumper --target=open5gs-upf --profile=sysadmin -- sleep infinity >/dev/null 2>&1; sleep 12; }
  map=$(bash "$HR/scripts/core_ue_info.sh" --tcp-map 2>/dev/null | tail -1)
  [ -n "$map" ] || { echo "  !! no UE IPs from the core: tcp_probe not started"; return; }
  setsid python3 "$HR/telemetry/tcp_probe.py" --map "$map" --container tcpdumper --vm $VM \
    >/tmp/tcp_probe.log 2>&1 </dev/null & disown
  sleep 3
  echo "  tcp_probe started (--map $map): $(grep -m1 -iE 'capturing|error|no ' /tmp/tcp_probe.log 2>/dev/null)"
}

ue_rx_probe() {
  if pgrep -f "[u]e_rx_probe.py" >/dev/null; then
    echo "  ue_rx_probe already running ($(pgrep -f '[u]e_rx_probe.py' | head -1))"; return
  fi
  setsid python3 "$HR/telemetry/ue_rx_probe.py" --vm $VM >/tmp/ue_rx_probe.log 2>&1 </dev/null & disown
  sleep 2
  echo "  ue_rx_probe started: $(head -1 /tmp/ue_rx_probe.log 2>/dev/null)"
}

[ $# -ge 1 ] || usage
CMD=$1; shift
case "$CMD" in
  build)
    (cd "$HR/jrtc-apps/codelets" && bash ./make.sh -d $NAME) 2>&1 | grep -E "error|Failed|terminates within" | sed 's/^/  /'
    ;;
  load)
    [ -f "$CODELETS/rts_stats.o" ] || { echo "ERROR: codelet not built: bash $0 build"; exit 1; }
    gx 'pgrep -x gnb >/dev/null' 15 || { echo "ERROR: no gNB running in $POD/$GC (setup_zmq_chan_demo.sh first)"; exit 1; }
    gx 'grep -aq mac_sched_slot_report /usr/local/bin/gnb' 15 || {
      echo "ERROR: this gNB build has no mac_sched_slot_report hook: rebuild ocudu-jbpf (EdgeRIC-RT hooks)"; exit 1; }
    echo "== codelet -> $POD/$GC:/codelets/$NAME =="
    gx "mkdir -p /codelets && rm -rf /codelets/$NAME" 15
    kubectl cp "$CODELETS" $NS/$POD:/codelets/$NAME -c $GC >/dev/null 2>&1 || { echo "ERROR: kubectl cp"; exit 1; }
    echo "== jbpf LCM forwarder in jrtc-0 =="
    if ! jx 'pgrep -f "[f]wd30450.py" >/dev/null' 15; then
      kubectl cp "$HR/telemetry/fwd30450.py" $NS/jrtc-0:/tmp/fwd30450.py -c jrtc >/dev/null 2>&1
      jx 'cd /tmp && nohup setsid python3 -u fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 3; echo "  fwd: $(head -1 /tmp/fwd30450.log 2>/dev/null)"' 20
    else
      echo "  fwd30450.py already running"
    fi
    echo "== load $DEPLOY =="
    jx "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c $DEPLOY --log-level error 2>&1 | grep -iE 'error|fatal' | tail -4" 120
    sleep 4
    gx 'cat /tmp/gnb.log /tmp/gnb.stdout 2>/dev/null | grep -aE "Codelet created and loaded successfully: rts_stats|rts_stats.*(fail|error)" | tail -2 | sed "s/^/  gnb: /"' 15
    kubectl logs -n $NS jrtc-0 -c jrtc --since=1m 2>/dev/null | grep -aE "\[rts\]|Failed to import|Python app terminated|Traceback" | tail -4 | sed 's/^/  jrtc: /'
    echo "== UE names =="
    bash "$HR/scripts/ue_rnti_map.sh" 2>&1 | sed 's/^/  /'
    bash "$HR/scripts/core_ue_info.sh" 2>&1 | sed 's/^/  /'
    echo "== Grafana =="
    grafana
    echo "== tcp_probe (TCP, tcpdump on the UPF) and ue_rx_probe (UDP, the UE receivers' logs) =="
    tcp_probe
    ue_rx_probe
    echo "Dashboard: http://localhost:30490/d/rt-scheduling"
    ;;
  unload)
    jx "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl unload -c $DEPLOY --log-level error 2>&1 | tail -2" 120
    gx 'cat /tmp/gnb.log /tmp/gnb.stdout 2>/dev/null | grep -aE "Codelet rts_stats removed" | tail -1 | sed "s/^/  gnb: /"' 15
    pgrep -f "[t]cp_probe.py|[u]e_rx_probe.py" >/dev/null && \
      echo "  the probes keep running: pkill -f '[t]cp_probe.py'; pkill -f '[u]e_rx_probe.py' to stop them"
    ;;
  grafana)
    grafana
    ;;
  status)
    echo "== xApp (jrtc-0 log) =="
    kubectl logs -n $NS jrtc-0 -c jrtc --since=30m 2>/dev/null | grep -aE "\[rts\]" | tail -4 | sed 's/^/  /'
    echo "== gNB codelet =="
    gx 'cat /tmp/gnb.log /tmp/gnb.stdout 2>/dev/null | grep -aE "Codelet .*rts_stats" | tail -2 | sed "s/^/  /"' 15
    echo "== latest samples (VictoriaMetrics) =="
    python3 - "$VM" <<'PY'
import json, sys, urllib.parse, urllib.request
vm = sys.argv[1]
def q(expr):
    url = f"{vm}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    return json.load(urllib.request.urlopen(url, timeout=5))["data"]["result"]
try:
    for r in sorted(q("last_over_time(rts_ue_ids[10s])"), key=lambda r: str(r["metric"].get("n"))):
        m = r["metric"]
        print(f"  UE {m.get('n')}: du {m.get('du')} rnti {m.get('rnti')} imsi {m.get('imsi')} ip {m.get('ip')}")
    for name in ("rts_ue_cqi", "rts_ue_backlog_bytes", "rts_ue_dl_mbps", "tcp_throughput_mbps", "ue_rx_mbps"):
        vals = {r["metric"].get("du", r["metric"].get("ip", r["metric"].get("n"))): float(r["value"][1])
                for r in q(f"last_over_time({name}[10s])")}  # tcp_probe's samples trail by ~3 s (kubectl exec)
        print(f"  {name:22s} " + "  ".join(f"{k}={v:.1f}" for k, v in sorted(vals.items())) if vals else
              f"  {name:22s} no samples in the last 10 s")
    c = q("last_over_time(rts_cell_hit_pct[3s])")
    print(f"  DL slots run by the muApp: {float(c[0]['value'][1]):.1f}%" if c else "  no cell samples")
except Exception as e:
    print(f"  VictoriaMetrics query failed: {e}")
PY
    pgrep -f "[t]cp_probe.py" >/dev/null && echo "  tcp_probe running" || echo "  tcp_probe not running"
    pgrep -f "[u]e_rx_probe.py" >/dev/null && echo "  ue_rx_probe running" || echo "  ue_rx_probe not running"
    ;;
  -h|--help) usage 0 ;;
  *) echo "unknown command $CMD"; usage ;;
esac
