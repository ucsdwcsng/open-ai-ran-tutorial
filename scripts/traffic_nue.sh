#!/usr/bin/env bash
# Downlink TCP traffic to every attached UE, for the N-UE bring-up (scripts/setup_zmq_chan_demo.sh).
#
#   bash scripts/traffic_nue.sh start [--cc cubic] [--time 3600] [--rate R] [--ue N]
#   bash scripts/traffic_nue.sh start --udp RATE [--time N]     # UDP at RATE per UE, e.g. 40M
#   bash scripts/traffic_nue.sh start --udp RATE --plain [--ue N] # plain UDP: iperf2 sender, perl receiver
#   bash scripts/traffic_nue.sh status          # per-UE + total goodput, measured at the UE
#   bash scripts/traffic_nue.sh stop
#
# One flow per UE: iperf3 -s inside netns ue<i> (port 5200+i), iperf3 -c on the UPF, so the core is
# the sender (same arrangement as traffic_2ue.sh). The UE-side server logs every second, and
# `status` reads those receiver-side logs, not the sender's.
# --plain (with --udp): iperf2 at the UPF and scripts/udp_sink.pl in the UE netns instead of iperf3. iperf3 keeps a TCP
# control connection on the same DL queue as its data, and a UE starved for seconds (finite-load scheduling
# experiments) can lose it, which ends the flow; plain UDP has nothing to lose. The sink logs iperf3-style lines.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; UC="${UE_C:-durue1}"
CMD="${1:-status}"; shift 2>/dev/null || true
ONLY=""; CC=cubic; T=3600; RATE=""; UDP=""; PLAIN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ue)   ONLY="$2"; shift 2 ;;
    --cc)   CC="$2";   shift 2 ;;
    --time) T="$2";    shift 2 ;;
    --rate) RATE="$2"; shift 2 ;;
    --udp)  UDP="$2";  shift 2 ;;
    --plain) PLAIN=1;  shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
UPF=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name 2>/dev/null | grep '^open5gs-upf-')
[ -z "$UPF" ] && { echo "ERROR: no UPF pod (KUBECONFIG? do not run with sudo)"; exit 1; }

ue()     { kubectl exec -n $NS $POD -c $UC -- bash -c "$1" 2>/dev/null; }
upf()    { kubectl exec -n open5gs "$UPF" -c open5gs-upf -- bash -c "$1" 2>/dev/null; }
ue_bg()  { setsid nohup kubectl exec -n $NS $POD -c $UC -- "$@" >/dev/null 2>&1 </dev/null & }
upf_bg() { setsid nohup kubectl exec -n open5gs "$UPF" -c open5gs-upf -- "$@" >/dev/null 2>&1 </dev/null & }
# "<n> <ip>" for every UE netns that has an address
attached() {
  ue 'for d in $(ip netns list | grep -oE "^ue[0-9]+"); do
        IP=$(ip netns exec $d ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o "10\.45\.0\.[0-9]*" | head -1)
        [ -n "$IP" ] && echo "${d#ue} $IP"; done' | sort -n
}

case "$CMD" in
  start)
    LIST=$(attached); [ -n "$ONLY" ] && LIST=$(echo "$LIST" | awk -v u="$ONLY" '$1==u')
    [ -z "$LIST" ] && { echo "no attached UE. Run: bash scripts/setup_zmq_chan_demo.sh <N>"; exit 1; }
    AVAIL=$(upf 'cat /proc/sys/net/ipv4/tcp_available_congestion_control')
    case " $AVAIL " in *" $CC "*) ;; *) echo "ERROR: '$CC' not available on the node ($AVAIL); sudo modprobe tcp_$CC"; exit 1 ;; esac
    [ "$PLAIN" = 1 ] && { kubectl cp "$HR/scripts/udp_sink.pl" "$NS/$POD:/tmp/udp_sink.pl" -c "$UC" >/dev/null 2>&1 || { echo "ERROR: copy udp_sink.pl"; exit 1; }; }
    while read -r n IP; do
      # [x]yz patterns: pgrep -f must not match the bash -c running this very line
      ue "for p in \$(pgrep -f '[n]etns exec ue$n iperf3') \$(pgrep -f '[u]dp_sink.pl $IP '); do kill -9 \$p; done; rm -f /tmp/ues/dl_ue$n.log; true"
      upf "for p in \$(pgrep -f '[i]perf3 -c $IP ') \$(pgrep -f '[i]perf -c $IP '); do kill -9 \$p; done; true"
    done <<< "$LIST"
    sleep 1
    while read -r n IP; do
      PORT=$((5200 + n))
      if [ -n "$UDP" ] && [ "$PLAIN" = 1 ]; then
        ue_bg ip netns exec ue$n perl /tmp/udp_sink.pl "$IP" "$PORT" /tmp/ues/dl_ue$n.log
        sleep 1
        upf_bg iperf -c "$IP" -p "$PORT" -u -b "$UDP" -l 1400 -t "$T" -i 10
        echo "ue$n: DL started  UPF -> $IP:$PORT  (plain udp $UDP, ${T}s)"
        continue
      elif [ -n "$UDP" ]; then
        OPTS="-p $PORT -u -b $UDP -l 1400 -t $T -i 10"; WHAT="udp $UDP"
      else
        OPTS="-p $PORT -C $CC -t $T -i 10"; [ -n "$RATE" ] && OPTS="$OPTS -b $RATE"; WHAT="cc=$CC rate=${RATE:-unlimited}"
      fi
      ue_bg ip netns exec ue$n iperf3 -s -B "$IP" -p "$PORT" -1 -i 1 --forceflush --logfile /tmp/ues/dl_ue$n.log
      sleep 1
      upf_bg iperf3 -c "$IP" $OPTS --logfile /tmp/dl_ue$n.log
      echo "ue$n: DL started  UPF -> $IP:$PORT  ($WHAT, ${T}s)"
    done <<< "$LIST"
    echo "Give TCP ~10 s to ramp, then: bash scripts/traffic_nue.sh status"
    ;;
  status)
    # Receiver-side: mean of the last 10 one-second intervals in each UE's iperf3 server log.
    ue 'tot=0
        for f in $(ls /tmp/ues/dl_ue*.log 2>/dev/null | sort -V); do
          n=$(basename $f .log); n=${n#dl_ue}
          r=$(grep -E "sec .*bits/sec" $f | grep -v -E "receiver|sender" | tail -10 | awk "{for(i=2;i<=NF;i++) if(\$i~/bits\/sec\$/){v=\$(i-1); u=\$i}; m=(u~/^G/)?1000:(u~/^K/)?0.001:1; s+=v*m; c++} END{if(c) printf \"%.1f\", s/c}")
          l=$(grep -E "sec .*bits/sec" $f | grep -v -E "receiver|sender" | tail -10 | grep -oE "[0-9]+/[0-9]+ \(" | tr -d "(" | awk -F/ "{a+=\$1; b+=\$2} END{if(b) printf \"  loss %.1f%%\", 100*a/b}")
          printf "  ue%-3s %8s Mbit/s%s  (mean of last 10 s at the UE)\n" "$n" "${r:-none}" "$l"
          [ -n "$r" ] && tot=$(awk "BEGIN{print $tot + $r}")
        done
        printf "  total %8.1f Mbit/s\n" "$tot"'
    ;;
  stop)
    ue  'for p in $(pgrep -x iperf3) $(pgrep -f udp_sink.pl); do kill -9 $p; done; true'
    upf 'for p in $(pgrep -x iperf3) $(pgrep -x iperf); do kill -9 $p; done; true'
    echo "traffic stopped"
    ;;
  *) echo "usage: bash scripts/traffic_nue.sh start|status|stop [--cc NAME] [--time N] [--rate R] [--ue N]"; exit 2 ;;
esac
