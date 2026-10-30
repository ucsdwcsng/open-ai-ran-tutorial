#!/usr/bin/env bash
# The TCP row of the user-plane dashboard (http://localhost:30490/d/upt-userplane): runs
# telemetry/tcp_probe.py in the foreground for the UEs attached now, until Ctrl-C.
#
#   bash scripts/tcp_probe.sh
#
# tcp_probe.py runs tcpdump in a privileged tcpdumper container on the UPF pod (the UPF's own container
# cannot capture); this adds the container if the pod has none, then takes each UE's IP from the core
# (core_ue_info.sh --tcp-map). Start it after the UEs attach; restart it after they re-attach.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"

if P=$(pgrep -f "[t]cp_probe.py"); then
  echo "ERROR: tcp_probe.py runs already (pid $(echo $P)): pkill -f '[t]cp_probe.py' first"; exit 1
fi
UPF=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name 2>/dev/null | grep '^open5gs-upf-' | head -1)
[ -n "$UPF" ] || { echo "ERROR: no UPF pod in namespace open5gs"; exit 1; }
if ! kubectl get pod -n open5gs "$UPF" -o jsonpath='{.status.ephemeralContainerStatuses[*].name}' | grep -qw tcpdumper; then
  echo "adding the tcpdumper container to $UPF"
  kubectl debug -n open5gs "$UPF" --image=nicolaka/netshoot --image-pull-policy=IfNotPresent \
    -c tcpdumper --target=open5gs-upf --profile=sysadmin -- sleep infinity >/dev/null || exit 1
  for i in $(seq 1 30); do
    kubectl get pod -n open5gs "$UPF" -o jsonpath='{.status.ephemeralContainerStatuses[?(@.name=="tcpdumper")].state.running}' | grep -q . && break
    sleep 2
  done
fi
# An ephemeral container never restarts, and a pod cannot get a second one of the same name.
kubectl get pod -n open5gs "$UPF" -o jsonpath='{.status.ephemeralContainerStatuses[?(@.name=="tcpdumper")].state.running}' | grep -q . || {
  echo "ERROR: the tcpdumper container on $UPF is not running, and ephemeral containers do not restart."
  echo "  Recreate the UPF pod (kubectl delete pod -n open5gs $UPF), re-point the SMF at the UPF"
  echo "  (tutorial Part 2), then restart the run."; exit 1; }
MAP=$(bash "$HR/scripts/core_ue_info.sh" --tcp-map 2>/dev/null | tail -1)
[ -n "$MAP" ] || { echo "ERROR: no UE IPs from the core: are the UEs attached?"; exit 1; }
echo "Dashboard: http://localhost:30490/d/upt-userplane   (Ctrl-C stops the TCP row)"
exec python3 "$HR/telemetry/tcp_probe.py" --map "$MAP"
