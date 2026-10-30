#!/usr/bin/env bash
# Stop the single-UE demo cleanly and reclaim the log.
#   bash scripts/stop_demo.sh
#
# Order matters: unload bufctl FIRST (needs a live gNB + jrtc) so the control
# hook stops running per packet and the xApp deregisters, then kill the radio.
# Applied buffer limits die with the gNB, so no RESET is needed here.
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS=ran; POD=srs-gnb-du1-0
k() { timeout "${3:-40}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2" 2>/dev/null; }

if pgrep -f bufctl_cli.py >/dev/null; then
  echo "!! bufctl_cli.py is still running (pid $(pgrep -f bufctl_cli.py | tr '\n' ' '))."
  echo "   Type 'quit' in it first so it unloads cleanly. Continuing anyway."
fi

echo "== [1/5] unload bufctl if attached =="
# the jbpf agent logs to the gNB's stdout under setup_zmq_chan_demo.sh, to gnb.log in other bring-ups
k ocudujbpf 'L=$(cat /tmp/gnb.stdout /tmp/gnb.log 2>/dev/null | grep -ac "Codelet created and loaded successfully: rlc_ctrl"); R=$(cat /tmp/gnb.stdout /tmp/gnb.log 2>/dev/null | grep -ac "Codelet rlc_ctrl removed successfully"); [ "$L" != "$R" ] && echo needs_unload || echo already_clean' 25 | grep -q needs_unload && {
  timeout 120 kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c \
    'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl unload -c /apps/bufctl/deployment.yaml --log-level error 2>&1 | tail -1' 2>/dev/null
  echo "  bufctl unloaded"; } || echo "  bufctl was not attached"

echo "== [2/5] stop traffic =="
UPF=$(timeout 15 kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name 2>/dev/null | grep "^open5gs-upf-")
[ -n "$UPF" ] && timeout 25 kubectl exec -n open5gs "$UPF" -c open5gs-upf -- bash -c 'pkill -9 -x iperf3 2>/dev/null; echo "  UPF iperf3 stopped"' 2>/dev/null
k durue1 'pkill -9 -x iperf3 2>/dev/null; sleep 1; echo "  UE iperf3 stopped"'
pkill -f "[t]cp_probe.py" 2>/dev/null && echo "  tcp_probe stopped" || echo "  tcp_probe not running"

echo "== [3/5] stop the UE =="
k durue1 'pkill -9 -x nr-uesoftmodem 2>/dev/null; sleep 3; echo "  UE stopped (live now: $(ps -eo stat,comm | grep nr-uesoftmodem | grep -v Z | grep -vc grep))"'

echo "== [4/5] stop the gNB and reclaim the log =="
k ocudujbpf 'SZ=$(du -h /tmp/gnb.log 2>/dev/null | cut -f1); for i in $(seq 1 8); do pkill -9 -x gnb 2>/dev/null; sleep 1; [ "$(pgrep -c -x gnb)" = 0 ] && break; done; : >/tmp/gnb.log; : >/tmp/gnb.stdout 2>/dev/null; echo "  gNB stopped; log truncated (was $SZ)"'

echo "== [5/5] stop the broker (multi-UE runs: Python or C++) =="
k grbroker 'pkill -9 -x python3 2>/dev/null; pkill -9 -x zmq_broker 2>/dev/null; rm -f /tmp/ran.env; sleep 1; echo "  broker stopped"' 25

echo
echo "STOPPED. Bring it back with:  bash scripts/restart_demo.sh"
