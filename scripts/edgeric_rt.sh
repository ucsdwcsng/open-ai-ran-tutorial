#!/usr/bin/env bash
# EdgeRIC-RT: the per-TTI synchronized scheduling loop (edgeric_rt codelets + rt_bridge xApp) on the running gNB.
#
#   bash scripts/edgeric_rt.sh load [--inline P] [--lead N] [--cpu LIST] [--fifo PRIO]
#   bash scripts/edgeric_rt.sh unload
#   bash scripts/edgeric_rt.sh status          bridge counters, codelet load lines, bridge errors
#   bash scripts/edgeric_rt.sh build           rebuild the codelets in the jbpf SDK image
#
# load writes jrtc-apps/jrtc_apps/edgeric_rt/bridge.conf, copies the codelets into the gNB container and loads
# jrtc-apps/jrtc_apps/edgeric_rt/rt_deployment.yaml from jrtc-0. Run it with the gNB up, e.g. after
# setup_zmq_chan_demo.sh (or use setup_zmq_chan_demo.sh --edgeric, which also starts a fresh jrtc-0 so the bridge
# is the only Python xApp: Python xApps share one GIL).
#
#   --inline P   off (default: reports go to the muApp in the edgeric container) | equal | maxweight |
#                fixed:w0,w1,... (the bridge acts by itself: measures the bare jbpf/jrtc loop)
#   --lead N     inline mode: target the first DL slot >= N TTIs after the report (default 1)
#   --cpu LIST   pin the bridge thread, e.g. 30 or 28-29 (outside the gNB's cores)
#   --fifo PRIO  SCHED_FIFO priority of the bridge thread
#
# Then start a muApp:  bash scripts/edgeric_muapp.sh start sched --scheduler maxweight
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; GC=ocudujbpf
APPDIR="$HR/jrtc-apps/jrtc_apps/edgeric_rt"
CODELETS="$HR/jrtc-apps/codelets/edgeric_rt"
DEPLOY=/apps/edgeric_rt/rt_deployment.yaml
usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit "${1:-1}"; }

gx() { timeout "${2:-30}" kubectl exec -n $NS $POD -c $GC -- bash -c "$1"; }
jx() { timeout "${2:-60}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }

[ $# -ge 1 ] || usage
CMD=$1; shift

case "$CMD" in
  build)
    (cd "$HR/jrtc-apps/codelets" && bash ./make.sh -d edgeric_rt) 2>&1 | grep -E "error|Failed|terminates within" | sed 's/^/  /'
    ;;

  load)
    INLINE=off; LEAD=1; CPU=""; FIFO=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --inline) INLINE="$2"; shift 2 ;;
        --lead)   LEAD="$2"; shift 2 ;;
        --cpu)    CPU="$2"; shift 2 ;;
        --fifo)   FIFO="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option $1"; usage ;;
      esac
    done
    [ -f "$CODELETS/rt_ctrl.o" ] && [ -f "$CODELETS/rt_report.o" ] || {
      echo "ERROR: codelets not built: bash scripts/edgeric_rt.sh build"; exit 1; }
    gx 'pgrep -x gnb >/dev/null' 15 || { echo "ERROR: no gNB running in $POD/$GC (setup_zmq_chan_demo.sh first)"; exit 1; }
    gx 'grep -aq mac_sched_slot_ctrl /usr/local/bin/gnb' 15 || {
      echo "ERROR: this gNB build has no mac_sched_slot_ctrl hook: rebuild ocudu-jbpf (EdgeRIC-RT hooks)"; exit 1; }

    cat > "$APPDIR/bridge.conf" <<EOF
# EdgeRIC-RT bridge settings, written by scripts/edgeric_rt.sh load. See rt_bridge_app.py.
inline = $INLINE
lead = $LEAD
cpu = $CPU
fifo = $FIFO
stats_s = 5
EOF
    echo "== bridge.conf: inline=$INLINE lead=$LEAD cpu=${CPU:--} fifo=$FIFO =="

    echo "== codelets -> $POD/$GC:/codelets/edgeric_rt =="
    gx 'mkdir -p /codelets && rm -rf /codelets/edgeric_rt' 15
    kubectl cp "$CODELETS" $NS/$POD:/codelets/edgeric_rt -c $GC >/dev/null 2>&1 || { echo "ERROR: kubectl cp"; exit 1; }

    echo "== jbpf LCM forwarder in jrtc-0 =="
    if ! jx 'pgrep -f "[f]wd30450.py" >/dev/null' 15 2>/dev/null; then
      kubectl cp "$HR/telemetry/fwd30450.py" $NS/jrtc-0:/tmp/fwd30450.py -c jrtc >/dev/null 2>&1
      jx 'cd /tmp && nohup setsid python3 -u fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 3; echo "  fwd: $(head -1 /tmp/fwd30450.log 2>/dev/null)"' 20
    else
      echo "  fwd30450.py already running"
    fi

    echo "== load $DEPLOY =="
    jx ": >/tmp/edgeric_rt_bridge.log; export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c $DEPLOY --log-level error 2>&1 | grep -iE 'error|fatal' | tail -4" 120
    sleep 4
    gx 'grep -aE "Codelet created and loaded successfully: rt_(ctrl|report)|rt_(ctrl|report).*(fail|error)" /tmp/gnb.log | tail -4 | sed "s/^/  gnb: /"' 15
    kubectl logs -n $NS jrtc-0 -c jrtc --since=1m 2>/dev/null | grep -aE "rt_bridge|Failed to import|Python app terminated|Traceback" | tail -6 | sed 's/^/  jrtc: /'
    ;;

  unload)
    jx "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl unload -c $DEPLOY --log-level error 2>&1 | tail -2" 120
    gx 'grep -aE "Codelet rt_(ctrl|report) removed" /tmp/gnb.log | tail -2 | sed "s/^/  gnb: /"' 15
    ;;

  status)
    echo "== bridge (jrtc-0:/tmp/edgeric_rt_bridge.log) =="
    jx 'tail -n 4 /tmp/edgeric_rt_bridge.log 2>/dev/null | sed "s/^/  /"' 15
    echo "== gNB codelets =="
    gx 'grep -aE "Codelet .*rt_(ctrl|report)" /tmp/gnb.log | tail -4 | sed "s/^/  /"' 15
    echo "== errors =="
    kubectl logs -n $NS jrtc-0 -c jrtc --since=10m 2>/dev/null | grep -aE "Traceback|Error|rt_bridge.*fail" | tail -5 | sed 's/^/  /'
    ;;

  -h|--help) usage 0 ;;
  *) echo "unknown command $CMD"; usage ;;
esac
