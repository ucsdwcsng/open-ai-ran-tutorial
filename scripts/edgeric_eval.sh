#!/usr/bin/env bash
# Compare EdgeRIC-RT schedulers on the same channel: each one runs for the same air time from the same trace start.
#
#   bash scripts/edgeric_eval.sh [options] SCHED [SCHED ...]
#
# SCHED: stock | rr | pf | pf-rate | maxcqi | bestcqi | maxweight | fixed:0.25,0.25,0.25,0.25 | rl:<model.npz>
#        (rl model paths are relative to edgeric-rt/, e.g. rl:runs/ppo_4ue_fullbuf/best.npz)
#
#   --traces FILE   trace table re-applied before every run, so every scheduler sees the same channel from t=0
#                   (default duranta-oai-ue/ue_traces_cqi_4ue.conf; "none" for a static or clean channel)
#   --duration S    seconds per scheduler (default 60)
#   --prefix P      run tags P-<sched> under edgeric-rt/runs (default eval-<time>)
#   --loads R,R,..  finite UDP load per UE in Mbit/s (ue1,ue2,...). Before every run: stop the traffic, drain every
#                   DL queue with bestcqi for --drain seconds, then restart each UE's flow at its rate, so no run
#                   inherits the backlog of the one before. The flows are plain UDP (traffic_nue.sh --plain), which a
#                   starved UE cannot lose. Without --loads the traffic already running is used.
#   --drain S       seconds of draining between runs with --loads (default 12)
#   --             further options passed to every sched_muapp.py run (e.g. -- --work-conserving)
#
# Needs the system up with traffic running (setup_zmq_chan_demo.sh 4 --edgeric ..., traffic_nue.sh start --udp 100M)
# and no other muApp consuming reports. Ends with the comparison table (muapps/eval.py).
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit "${1:-1}"; }

TRACES="$HR/duranta-oai-ue/ue_traces_cqi_4ue.conf"; DUR=60; PREFIX="eval-$(date +%m%d-%H%M)"; EXTRA=(); SCHEDS=()
LOADS=""; DRAIN=12
while [ $# -gt 0 ]; do
  case "$1" in
    --traces)   TRACES="$2"; shift 2 ;;
    --duration) DUR="$2"; shift 2 ;;
    --prefix)   PREFIX="$2"; shift 2 ;;
    --loads)    LOADS="$2"; shift 2 ;;
    --drain)    DRAIN="$2"; shift 2 ;;
    --) shift; EXTRA=("$@"); break ;;
    -h|--help) usage 0 ;;
    *) SCHEDS+=("$1"); shift ;;
  esac
done
[ ${#SCHEDS[@]} -ge 1 ] || usage

# UDP delivered at the UEs: every one-second interval line of the UE-side iperf3 server logs (traffic_nue.sh)
udp_snap() {
  timeout 30 kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- bash -c \
    'for f in /tmp/ues/dl_ue*.log; do echo "## $f"; grep -E "sec .*bits/sec" $f | grep -vE "receiver|sender"; done' \
    > "$1" 2>/dev/null
}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
UDP_LINES=()

bash "$HR/scripts/edgeric_muapp.sh" stop sched >/dev/null 2>&1
TAGS=()
for sp in "${SCHEDS[@]}"; do
  name=${sp%%:*}; arg=${sp#*:}; [ "$arg" = "$sp" ] && arg=""
  case "$name" in
    fixed)   A=(--scheduler fixed --weights "$arg"); label="fixed-${arg//,/_}" ;;
    rl)      A=(--scheduler rl --model "$arg"); label="rl-$(basename "$arg" .npz)" ;;
    pf-rate) A=(--scheduler pf --pf-mode rate); label="pf-rate" ;;
    *)       A=(--scheduler "$name"); label="$name" ;;
  esac
  tag="$PREFIX-$label"
  echo "== $tag: ${A[*]} ${EXTRA[*]} (${DUR}s) =="
  if [ -n "$LOADS" ]; then
    # same start for every scheduler: empty queues, then each UE's flow at its own rate
    bash "$HR/scripts/traffic_nue.sh" stop >/dev/null 2>&1
    kubectl exec -n ran edgeric-0 -c edgeric -- bash -c \
      "cd /edgeric && timeout $((DRAIN + 20)) python3 muapps/sched_muapp.py --scheduler bestcqi --duration $DRAIN --no-log --stats 1000 >/dev/null 2>&1"
    IFS=, read -ra RATES <<< "$LOADS"
    for i in "${!RATES[@]}"; do
      bash "$HR/scripts/traffic_nue.sh" start --udp "${RATES[$i]}M" --plain --ue $((i + 1)) --time $((DUR + 120)) | grep -E "started|ERROR" | sed 's/^/  /'
    done
    sleep 2
  fi
  if [ "$TRACES" != none ]; then
    bash "$HR/scripts/trace_ctl.sh" apply "$TRACES" 2>&1 | grep -E "switch at|ERROR" | sed 's/^/  /'
    # trace_ctl switches every UE at the same air time a few seconds ahead: start once the traces have taken over
    sleep 4
  fi
  bash "$HR/scripts/edgeric_muapp.sh" start sched "${A[@]}" --duration "$DUR" --tag "$tag" "${EXTRA[@]}" | tail -1 | sed 's/^/  /'
  udp_snap "$TMP/start"
  until kubectl exec -n ran edgeric-0 -c edgeric -- bash -c "grep -q 'saved\|Traceback' /edgeric/runs/.sched.out" 2>/dev/null; do sleep 3; done
  udp_snap "$TMP/end"
  kubectl exec -n ran edgeric-0 -c edgeric -- bash -c "grep -E '^\[sched ' /edgeric/runs/.sched.out | tail -1 | cut -c1-220; grep -A3 Traceback /edgeric/runs/.sched.out" | sed 's/^/  /'
  # UDP delivered at the UEs over this run (mean of its one-second iperf3 intervals), when traffic is running
  U=$(python3 -I "$HR/edgeric-rt/tools/udp_window.py" "$TMP/start" "$TMP/end" | tail -1)
  echo "  UDP delivered: $U"
  UDP_LINES+=("$tag $(echo "$U" | awk '{print $2}')")
  bash "$HR/scripts/edgeric_muapp.sh" stop sched >/dev/null 2>&1
  TAGS+=("$tag")
done

echo
echo "== comparison (edgeric-rt/runs/$PREFIX.csv) =="
printf '%s\n' "${UDP_LINES[@]}" | kubectl exec -i -n ran edgeric-0 -c edgeric -- bash -c "cat > /edgeric/runs/$PREFIX.udp"
kubectl exec -n ran edgeric-0 -c edgeric -- bash -c \
  "cd /edgeric && python3 muapps/eval.py ${TAGS[*]} --udp runs/$PREFIX.udp --csv runs/$PREFIX.csv"
