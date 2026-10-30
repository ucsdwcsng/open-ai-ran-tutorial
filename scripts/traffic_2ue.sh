#!/usr/bin/env bash
# Independent downlink traffic per UE on the 2-UE testbed.
# Bring-up is separate: scripts/setup_zmq_chan_demo.sh 2 (or setup_bufctl_2ue_demo.sh) must have run first.
#
#   bash scripts/traffic_2ue.sh start [options]
#   bash scripts/traffic_2ue.sh stop
#   bash scripts/traffic_2ue.sh status
#
# Options:
#   --ue N          only this UE (1 or 2); default: both
#   --cc1 NAME      congestion control for UE1   (default bbr)
#   --cc2 NAME      congestion control for UE2   (default cubic)
#   --cc NAME       same algorithm on both UEs (overrides --cc1/--cc2)
#   --time N        seconds                                        (default 3600)
#   --rate R        cap the flow, e.g. 2M (default: unlimited/bulk)
#
# Default is deliberately MIXED -- BBR on UE1, CUBIC on UE2 -- so the two bearers react
# differently to the same buffer limit. CUBIC fills whatever buffer it is given (loss-based),
# BBR paces to its own bandwidth-delay estimate, so a cap that cripples one barely moves the
# other. Set the same per-UE cap in bufctl_cli.py and compare.
#
# Direction: iperf3 -C sets congestion control on the process that SENDS, and for a
# downlink flow the sender is the core. So `iperf3 -s` runs inside the UE's netns and
# `iperf3 -c` runs on the UPF -- the same arrangement as scripts/traffic.sh.
#
# Ports: UE1 -> 5201, UE2 -> 5202, so the two flows never share a server.
#
# Long-running iperf3 is launched by a BACKGROUNDED HOST-SIDE kubectl exec rather than
# "setsid ... & disown" inside the container: an orphan inside the container is reparented
# to a PID 1 that never reaps it, which is how the old scripts accumulated zombies.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0
CMD="${1:-start}"; shift 2>/dev/null || true
case "$CMD" in -h|--help|help) sed -n '2,24p' "$0" | sed 's/^# \?//'; exit 0 ;; esac
ONLY=""; CC1=bbr; CC2=cubic; T=3600; RATE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ue)   ONLY="$2"; shift 2 ;;
    --cc)   CC1="$2"; CC2="$2"; shift 2 ;;
    --cc1)  CC1="$2";  shift 2 ;;
    --cc2)  CC2="$2";  shift 2 ;;
    --time) T="$2";    shift 2 ;;
    --rate) RATE="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
UPF=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name 2>/dev/null | grep '^open5gs-upf-')
if [ -z "$UPF" ]; then
  echo "ERROR: no UPF pod found. Is the cluster up, and is KUBECONFIG pointing at it?"
  echo "  export KUBECONFIG=\"\$(k3d kubeconfig write janus-cluster)\""
  if [ "$(id -u)" = "0" ]; then
    echo "  NOTE: you are running as root. Do NOT use sudo for this script -- sudo resets the"
    echo "        environment, so KUBECONFIG is lost and root falls back to /root/.kube/config."
    echo "        Only 'modprobe tcp_bbr' needs sudo. Re-run as your normal user."
  fi
  exit 1
fi

ue()     { kubectl exec -n $NS $POD -c durue1 -- bash -c "$1" 2>/dev/null; }
upf()    { kubectl exec -n open5gs "$UPF" -c open5gs-upf -- bash -c "$1" 2>/dev/null; }
ue_bg()  { setsid nohup kubectl exec -n $NS $POD -c durue1 -- "$@" >/dev/null 2>&1 </dev/null & }
upf_bg() { setsid nohup kubectl exec -n open5gs "$UPF" -c open5gs-upf -- "$@" >/dev/null 2>&1 </dev/null & }
ueip()   { ue "ip netns exec ue$1 ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o '10\.45\.0\.[0-9]*' | head -1" | tr -d '[:space:]'; }
ues()    { [ -n "$ONLY" ] && echo "$ONLY" || echo "1 2"; }

case "$CMD" in
  start)
    for n in $(ues); do
      IP=$(ueip $n)
      [ -z "$IP" ] && { echo "ue$n: NOT attached (no oaitun_ue1). Bring up two UEs first: bash scripts/setup_zmq_chan_demo.sh 2"; exit 1; }
    done
    # clear only what we are about to replace
    for n in $(ues); do
      ue "for p in \$(pgrep -f 'netns exec ue$n.*iperf3'); do kill -9 \$p 2>/dev/null; done; true"
    done
    [ -z "$ONLY" ] && upf 'for p in $(pgrep -x iperf3); do kill -9 $p 2>/dev/null; done; true'
    sleep 1
    # The sender is the UPF, so the algorithm must exist in the node kernel it shares.
    AVAIL=$(upf 'cat /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null')
    for n in $(ues); do
      eval "CC=\$CC$n"
      case " $AVAIL " in
        *" $CC "*) : ;;
        *) echo "ERROR: congestion control '$CC' not available on the UPF node."
           echo "  available: ${AVAIL:-<unknown>}"
           echo "  load it on the HOST (k3d nodes share the host kernel), then retry:"
           echo "      sudo modprobe tcp_$CC"
           echo "      echo tcp_$CC | sudo tee /etc/modules-load.d/tcp_$CC.conf   # persist"
           exit 1 ;;
      esac
    done
    for n in $(ues); do
      eval "CC=\$CC$n"
      IP=$(ueip $n); PORT=$((5200+n))
      OPTS="-p $PORT -C $CC -t $T -i 10"
      [ -n "$RATE" ] && OPTS="$OPTS -b $RATE"
      ue_bg  ip netns exec ue$n iperf3 -s -B "$IP" -p "$PORT" -1
      sleep 1
      upf_bg iperf3 -c "$IP" $OPTS --logfile /tmp/dl_ue$n.log
      echo "ue$n: DL started  UPF -> $IP:$PORT  (cc=$CC rate=${RATE:-unlimited} ${T}s)"
    done
    echo
    echo "Per-UE buffer control:  python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py"
    ;;
  stop)
    ue  'for p in $(pgrep -x iperf3); do kill -9 $p 2>/dev/null; done; true'
    upf 'for p in $(pgrep -x iperf3); do kill -9 $p 2>/dev/null; done; true'
    sleep 2
    echo "stopped. live iperf3 -- ue container: $(ue 'ps -eo stat,comm | grep -c "^[^Z].*iperf3"' | tr -d '[:space:]')" \
         "upf: $(upf 'ps -eo stat,comm | grep -c "^[^Z].*iperf3"' | tr -d '[:space:]')"
    ;;
  status)
    for n in 1 2; do
      IP=$(ueip $n)
      RX=$(upf "grep -oE '[0-9.]+ [KMG]bits/sec' /tmp/dl_ue$n.log 2>/dev/null | tail -1")
      ALGO=$(upf "grep -oE 'Congestion.*' /tmp/dl_ue$n.log 2>/dev/null | tail -1")
      printf "  ue%s ip=%-12s last=%-16s %s\n" "$n" "${IP:-NONE}" "${RX:-no data}" "${ALGO:-}"
    done
    echo "  live iperf3 -- ue: $(ue 'ps -eo stat,comm | grep -c "^[^Z].*iperf3"' | tr -d '[:space:]')" \
         "upf: $(upf 'ps -eo stat,comm | grep -c "^[^Z].*iperf3"' | tr -d '[:space:]')"
    ;;
  *) echo "usage: bash scripts/traffic_2ue.sh start|stop|status [--ue N] [--cc1 bbr] [--cc2 cubic] [--cc BOTH] [--time N] [--rate R]"; exit 2 ;;
esac
