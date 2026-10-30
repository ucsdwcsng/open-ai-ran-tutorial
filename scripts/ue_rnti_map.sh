#!/usr/bin/env bash
# Write the ue<n> -> C-RNTI map the EdgeRIC-RT muApps use to resolve UE names (e.g. --weights ue1=0.2,...).
#
#   bash scripts/ue_rnti_map.sh [N]        (default: every UE log in the UE container)
#
# Reads the C-RNTI each OAI UE logs at RRC setup from /tmp/ues/ue<n>.log in the UE container and writes
# jrtc-apps/jrtc_apps/edgeric_rt/run/ue_map.txt (seen as /apps/edgeric_rt/run/ue_map.txt by the muApps).
# Re-run after the UEs re-attach: a UE gets a new C-RNTI on every attach.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; UC="${UE_C:-durue1}"
OUT="$HR/jrtc-apps/jrtc_apps/edgeric_rt/run/ue_map.txt"
N=${1:-0}

MAP=$(timeout 30 kubectl exec -n $NS $POD -c $UC -- bash -c '
  N='"$N"'; [ "$N" -gt 0 ] || N=$(ls /tmp/ues/ue*.log 2>/dev/null | sed -n "s#.*/ue\([0-9]*\)\.log#\1#p" | sort -n | tail -1)
  for n in $(seq 1 ${N:-0}); do
    R=$(grep -a -oiE "(c-)?rnti[ :=]*0x[0-9a-f]{4}" /tmp/ues/ue$n.log 2>/dev/null | tail -1 | grep -oiE "0x[0-9a-f]{4}")
    echo "ue$n ${R:-unknown}"
  done') || { echo "ERROR: cannot read the UE logs in $POD/$UC"; exit 1; }

mkdir -p "$(dirname "$OUT")"
{ echo "# ue name -> C-RNTI, from the UE logs ($(date -u +%FT%TZ)). Written by scripts/ue_rnti_map.sh."
  echo "$MAP" | grep -v unknown; } > "$OUT"
chmod 0666 "$OUT" 2>/dev/null
echo "$MAP"
echo "$MAP" | grep -q unknown && echo "!! some UEs have no C-RNTI in their log (not attached?); they fall back to attach order"
echo "wrote $OUT"
