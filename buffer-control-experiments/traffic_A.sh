#!/usr/bin/env bash
# Experiment A traffic on the single-UE testbed: a paced "video call" + a bulk download,
# both downlink on the same UE/DRB. Sender on the UPF so -C picks its congestion control.
#
#   bash buffer-control-experiments/traffic_A.sh start [--cc cubic|bbr] [--video-rate 2M] [--time 3600] [--ue N]
#   bash buffer-control-experiments/traffic_A.sh bulk-only|video-only [--cc ...]   # start just one
#   bash buffer-control-experiments/traffic_A.sh stop
#
# --ue N (default 1): the UE in network namespace ue<N> (scripts/setup_zmq_chan_demo.sh). Without that
# namespace the UE is the single direct-mode UE of scripts/setup_bufctl_1ue_demo.sh.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0
CMD="${1:-start}"; shift 2>/dev/null || true
CC=cubic; VRATE=2M; T=3600; UEN=1
while [ $# -gt 0 ]; do
  case "$1" in
    --cc) CC="$2"; shift 2 ;;
    --video-rate) VRATE="$2"; shift 2 ;;
    --time) T="$2"; shift 2 ;;
    --ue) UEN="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
UPF=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name | grep '^open5gs-upf-')
ue()  { kubectl exec -n $NS $POD -c durue1 -- bash -c "$1" 2>/dev/null; }
upf() { kubectl exec -n open5gs "$UPF" -c open5gs-upf -- bash -c "$1" 2>/dev/null; }
# iperf3 runs under a backgrounded HOST-SIDE kubectl exec (not "setsid ... & disown" inside
# the container), so the runtime reaps it when it exits instead of leaving a zombie under the
# container's PID 1. Same pattern as scripts/traffic.sh.
ue_bg()  { setsid nohup kubectl exec -n $NS $POD -c durue1 -- "$@" >/dev/null 2>&1 </dev/null & }
upf_bg() { setsid nohup kubectl exec -n open5gs "$UPF" -c open5gs-upf -- "$@" >/dev/null 2>&1 </dev/null & }
# The UE's receivers run where its tunnel is: in netns ue<N>, else in the container's own namespace.
NSX=(); ue "ip netns list | grep -qw ue$UEN" && NSX=(ip netns exec "ue$UEN")
UEIP=$(ue "${NSX[*]} ip -o -4 addr show oaitun_ue1" | grep -o '10\.45\.0\.[0-9]*' | head -1)

flow() {  # flow PORT CC EXTRA
  ue_bg  "${NSX[@]}" iperf3 -s -1 -B "$UEIP" -p "$1"; sleep 1
  upf_bg iperf3 -c "$UEIP" -p "$1" -C "$2" -t "$T" $3 --logfile "/tmp/trafficA_$1.log"
}
case "$CMD" in
  start|bulk-only|video-only)
    [ -n "$UEIP" ] || { echo "UE not attached"; exit 1; }
    [ "$CMD" != bulk-only ]  && { flow 5202 cubic "-b $VRATE"; echo "video: cubic paced $VRATE -> $UEIP:5202"; }
    [ "$CMD" != video-only ] && { flow 5201 "$CC" "";        echo "bulk:  $CC unlimited  -> $UEIP:5201"; }
    bash "$(dirname "${BASH_SOURCE[0]}")/flowmon.sh" start ;;
  stop)
    upf 'pkill -9 -x iperf3; true'; ue 'pkill -9 -x iperf3; true'; echo "traffic stopped"
    bash "$(dirname "${BASH_SOURCE[0]}")/flowmon.sh" stop ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
