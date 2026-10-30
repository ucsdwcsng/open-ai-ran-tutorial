#!/usr/bin/env bash
# Real-time tuning for the running N-UE ZMQ testbed. setup_zmq_chan_demo.sh applies it by default.
#
#   bash scripts/zmq_tune.sh apply       # SCHED_FIFO for gNB + broker, and keep the testbed on one NUMA node
#   bash scripts/zmq_tune.sh show        # policy / priority / CPU of the busiest threads
#
# Why: the ZMQ chain is lock-step REQ/REP, so it runs only as fast as its slowest hop, and the hottest
# hop is the gNB's `radio` thread (ZMQ sample I/O, ~90% of a core under load). The gNB and broker
# threads otherwise run SCHED_OTHER and get preempted by the host's other load, which is spread over
# every core; the UEs already run SCHED_FIFO/RR.
# Measured at 4 UEs (static 30 dB): UDP 44.8 -> 52.6 Mbit/s, real time under load 66% -> 77%.
#
# Deliberately NOT done: narrow per-component pinning (gNB on 4 cores, broker on 4, ...). The broker
# runs ~110 threads and the gNB ~15, so small core sets make them contend; it measured 30% worse.
# Only the NUMA node is fixed (TESTBED_CPUS, default node 1 = 16-31 + siblings 48-63).
set -u
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; UC="${UE_C:-durue1}"
CPUS="${TESTBED_CPUS:-16-31,48-63}"
kx() { timeout 60 kubectl exec -n $NS $POD -c $UC -- bash -c "$1"; }

# The pod shares one PID namespace, so the UE container can see and tune every process.
APPLY='G=$(pgrep -x gnb); B=$(pgrep -f "[m]ulti_ue_broker.py|[z]mq_broker -n")
  [ -n "$G" ] || { echo "  tune: no gnb running"; exit 1; }
  # gNB: radio (ZMQ sample I/O) highest, then its PHY workers, the rest above normal
  for t in $(ls /proc/$G/task); do c=$(cat /proc/$G/task/$t/comm)
    case "$c" in radio*) p=90 ;; phy_*|lower*|low_*|ru_*|upper*) p=80 ;; *) p=60 ;; esac
    chrt -f -p $p $t 2>/dev/null; done
  for b in $B; do for t in $(ls /proc/$b/task); do chrt -f -p 70 $t 2>/dev/null; done; done
  for p in $G $B $(pgrep -x nr-uesoftmodem); do
    grep -q "^State:.*Z" /proc/$p/status 2>/dev/null && continue; taskset -a -p -c '"$CPUS"' $p >/dev/null 2>&1; done
  echo "  tune: gnb + broker threads SCHED_FIFO; gnb, broker, UEs on CPUs '"$CPUS"'"'

SHOW='for p in $(pgrep -x gnb) $(pgrep -f "[m]ulti_ue_broker.py|[z]mq_broker -n") $(pgrep -x nr-uesoftmodem); do
    grep -q "^State:.*Z" /proc/$p/status && continue
    ps -L -p $p -o comm=,tid=,cls=,rtprio=,psr=,pcpu= --sort=-pcpu | head -3 | sed "s/^/  $(cat /proc/$p/comm)\t/"; done'

case "${1:-show}" in
  apply) kx "$APPLY" ;;
  show)  kx "$SHOW" ;;
  *) sed -n '2,17p' "$0" | sed 's/^# \?//'; exit 2 ;;
esac
