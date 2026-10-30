#!/usr/bin/env bash
# Bring the single-UE testbed up. NO application data -- that is scripts/traffic.sh,
# so you can drive whatever traffic you want on top of a known-good pipeline.
#
#   bash scripts/setup_bufctl_1ue_demo.sh          # stop anything running, then bring up (~4-5 min)
#
# What it leaves you with: OCUDU gNB + one OAI UE attached + the `upt` telemetry
# codeletset + tcp_probe, and an RLC queue of 8192 SDU / 8 MB so a buffer limit
# has room to bind.
#
# bufctl is deliberately NOT loaded: jrtc_apps/bufctl/bufctl_cli.py loads it on
# start and unloads it on quit, because while it is loaded the rlc_dl_ctrl hook
# runs on every downlink packet.
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS=ran; POD=srs-gnb-du1-0
k() { timeout "${3:-30}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2" 2>/dev/null; }

# Pre-flight BEFORE stopping anything, so a bad clone cannot cost a working pipeline.
for f in "$HR/ocudu-jbpf/deploy/gnb_ocudu_jbpf.yml" "$HR/ocudu-jbpf/configs/qos.yml"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; echo "  run: git submodule update --init --recursive"; exit 1; }
done
for f in "$HR/telemetry/fwd30450.py" "$HR/telemetry/tcp_probe.py" \
         "$HR/duranta-oai-ue/oaiue_zmq.conf" "$HR/duranta-oai-ue/uecap_ports1.xml"; do
  [ -f "$f" ] || { echo "ERROR: missing repo file $f"; exit 1; }
done

bash "$HR/scripts/stop_demo.sh"

echo
echo "################ bring-up ################"
bash "$HR/telemetry/fresh_start_demo.sh" 2>&1 | tail -12

echo
echo "################ verify ################"
UEIP=$(k durue1 'ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o "10\.45\.0\.[0-9]*" | head -1' 20 | tr -d '[:space:]')
echo "  UE_IP=${UEIP:-NONE}"
echo "  codelets registered:"
k ocudujbpf 'grep -ao "Codelet created and loaded successfully: [a-z_]*" /tmp/gnb.log | sort -u | sed "s/^/    /"' 25
echo "  DRB queue config:"
k ocudujbpf 'grep -ao "RLC AM configured.*queue_size=[0-9]* queue_size_bytes=[0-9]*" /tmp/gnb.log | tail -1 | sed "s/^/    /"' 25

if [ -z "$UEIP" ]; then
  echo
  echo "  !! UE did not attach. Last lines of the UE log:"
  k durue1 'tail -8 /tmp/ue_direct.log' 20
  echo "  Re-run this script; if it fails twice the ZMQ pair needs a fresh gNB (it cannot"
  echo "  reconnect mid-run), which this script already does each time."
  exit 1
fi

echo
echo "SETUP DONE. UE $UEIP attached, telemetry flowing on the next downlink packets."
echo "  add traffic : bash scripts/traffic.sh start            # DL CUBIC, 1 h"
echo "                bash scripts/traffic.sh start --cc bbr --time 600"
echo "                bash scripts/traffic.sh --help           # all options"
echo "  control     : python3 $HR/jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py"
echo "  dashboard   : http://localhost:30490/d/upt-userplane"
echo "  status      : bash scripts/status.sh"
echo "  stop all    : bash scripts/stop_demo.sh"
