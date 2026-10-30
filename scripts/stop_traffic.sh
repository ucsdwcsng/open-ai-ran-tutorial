#!/usr/bin/env bash
# Stop ALL iperf3 traffic on the single-UE testbed (scripts/traffic.sh and
# buffer-control-experiments/traffic_A.sh flows alike) and report what is left.
#   bash scripts/stop_traffic.sh
#
# Zombies: iperf3 used to be started with "setsid ... & disown" inside the containers. Those
# orphans are re-parented to the container's PID 1 (tail / open5gs-upfd), which never reaps
# them, so each finished iperf3 stayed as a zombie. The traffic scripts now start iperf3 from a
# host-side kubectl exec instead, so no new zombies are created. EXISTING zombies cannot be
# killed (they have already exited); they disappear when the pod restarts. They use no CPU or
# memory, only PID slots (limit ~300k per container).
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0
UPF=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name 2>/dev/null | grep '^open5gs-upf-')
ue()  { kubectl exec -n "$NS" "$POD" -c durue1 -- sh -c "$1" 2>/dev/null; }
upf() { kubectl exec -n open5gs "$UPF" -c open5gs-upf -- sh -c "$1" 2>/dev/null; }

ue  'pkill -9 -x iperf3; true'
upf 'pkill -9 -x iperf3; true'
# host-side kubectl exec sessions that carried them (anchored so this script never matches itself)
pkill -f '^kubectl exec .* -- iperf3' 2>/dev/null
sleep 1
echo "traffic stopped."
echo "  live iperf3:   UE $(ue 'ps -eo stat,comm | grep -v "^Z" | grep -c iperf3')   UPF $(upf 'ps -eo stat,comm | grep -v "^Z" | grep -c iperf3')"
echo "  zombie iperf3: UE $(ue 'ps -eo stat,comm | grep -c "^Z.*iperf3"')   UPF $(upf 'ps -eo stat,comm | grep -c "^Z.*iperf3"')   (harmless leftovers from before the fix; cleared on pod restart)"
