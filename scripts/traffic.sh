#!/usr/bin/env bash
# Application data for the single-UE testbed. Bring-up is separate:
# scripts/setup_bufctl_1ue_demo.sh must have run first.
#
#   bash scripts/traffic.sh start [options]
#   bash scripts/traffic.sh stop
#   bash scripts/traffic.sh status
#
# Options (all optional; defaults reproduce the original DL CUBIC 1-hour flow):
#   --dir dl|ul     direction                            (default dl)
#   --cc NAME       sender congestion control: cubic, bbr, reno, ...  (default cubic)
#   --time N        seconds                              (default 3600)
#   --rate R        target bitrate, e.g. 20M             (default unlimited; 100M for --udp)
#   --streams N     parallel streams (iperf3 -P)         (default 1)
#   --udp           UDP instead of TCP                   (default TCP)
#   --port N        port                                 (default 5201)
#
# WHERE THE SENDER RUNS, AND WHY IT MATTERS:
#   iperf3 -C sets congestion control on the process that SENDS. For a downlink
#   flow the sender is the core side, so this script runs `iperf3 -s` inside the
#   UE container and the client on the UPF with -C. The obvious alternative --
#   a UE-side client with `-R` -- makes the SERVER the sender, so -C is silently
#   ignored and you always get the node default. Uplink is the mirror image.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0
GW=10.45.0.1                 # UPF gateway inside the UE subnet

DIR=dl; CC=cubic; T=3600; RATE=""; STREAMS=1; PROTO=tcp; PORT=5201
CMD="${1:-start}"; shift 2>/dev/null || true
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)     DIR="${2:-dl}"; shift 2 ;;
    --cc)      CC="${2:-cubic}"; shift 2 ;;
    --time)    T="${2:-3600}"; shift 2 ;;
    --rate)    RATE="${2:-}"; shift 2 ;;
    --streams) STREAMS="${2:-1}"; shift 2 ;;
    --udp)     PROTO=udp; shift ;;
    --port)    PORT="${2:-5201}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done
[ "$PROTO" = udp ] && [ -z "$RATE" ] && RATE=100M   # iperf3 UDP defaults to 1 Mbit otherwise

UPF=$(kubectl get pods -n open5gs --no-headers 2>/dev/null | grep -oE 'open5gs-upf-[a-z0-9-]+' | head -1)
UEIP=$(kubectl exec -n "$NS" "$POD" -c durue1 -- \
        ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o '10\.45\.0\.[0-9]*' | head -1)
ue() { kubectl exec -n "$NS" "$POD" -c durue1 -- bash -c "$1" 2>/dev/null; }
upf() { kubectl exec -n open5gs "$UPF" -c open5gs-upf -- bash -c "$1" 2>/dev/null; }
# Long-running iperf3 is started by a BACKGROUNDED HOST-SIDE kubectl exec, not with
# "setsid ... & disown" inside the container: an orphan inside the container is re-parented
# to the container's PID 1 (tail / open5gs-upfd), which never reaps it, so every finished
# iperf3 used to stay behind as a zombie. An exec'd process is reaped by the runtime instead.
ue_bg()  { setsid nohup kubectl exec -n "$NS" "$POD" -c durue1 -- "$@" >/dev/null 2>&1 </dev/null & }
upf_bg() { setsid nohup kubectl exec -n open5gs "$UPF" -c open5gs-upf -- "$@" >/dev/null 2>&1 </dev/null & }

OPTS="-p $PORT -t $T -P $STREAMS"
[ -n "$RATE" ] && OPTS="$OPTS -b $RATE"
[ "$PROTO" = udp ] && OPTS="$OPTS -u" || OPTS="$OPTS -C $CC"

case "$CMD" in
  start)
    [ -z "$UEIP" ] && { echo "UE not attached (no oaitun_ue1). Run: bash scripts/setup_bufctl_1ue_demo.sh"; exit 1; }
    [ -z "$UPF" ]  && { echo "UPF pod not found in namespace open5gs."; exit 1; }
    ue  'pkill -9 -x iperf3 2>/dev/null; true'
    upf 'pkill -9 -x iperf3 2>/dev/null; true'
    sleep 1
    if [ "$DIR" = dl ]; then
      # server in the UE, sender (client) on the UPF -> -C applies to the DL sender
      ue_bg  iperf3 -s -B "$UEIP" -p "$PORT"
      sleep 1
      upf_bg iperf3 -c "$UEIP" $OPTS --logfile /tmp/traffic.log
      echo "DL $PROTO started: UPF -> UE $UEIP  (cc=$CC streams=$STREAMS rate=${RATE:-unlimited} ${T}s)"
      echo "  sender log: UPF container /tmp/traffic.log"
    else
      ue_bg  iperf3 -s -B "$UEIP" -p "$PORT"     # harmless if unused
      upf_bg iperf3 -s -B "$GW" -p "$PORT"
      sleep 1
      ue_bg  iperf3 -c "$GW" $OPTS --logfile /tmp/traffic.log
      echo "UL $PROTO started: UE $UEIP -> UPF $GW  (cc=$CC streams=$STREAMS rate=${RATE:-unlimited} ${T}s)"
      echo "  sender log: durue1 container /tmp/traffic.log"
    fi
    echo "  watch: http://localhost:30490/d/upt-userplane   ·   bash scripts/status.sh"
    ;;
  stop)
    ue  'pkill -9 -x iperf3 2>/dev/null; true'
    upf 'pkill -9 -x iperf3 2>/dev/null; true'
    echo "traffic stopped."
    ;;
  status)
    echo -n "  UE  iperf3 live: "; ue  'ps -eo stat,comm | grep iperf3 | grep -v Z | grep -vc grep'
    echo -n "  UPF iperf3 live: "; upf 'ps -eo stat,comm | grep iperf3 | grep -v Z | grep -vc grep'
    echo "  --- last sender line (DL: UPF, UL: UE):"
    upf 'tail -2 /tmp/traffic.log 2>/dev/null' | sed 's/^/    UPF: /'
    ue  'tail -2 /tmp/traffic.log 2>/dev/null' | sed 's/^/    UE:  /'
    ;;
  *) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
