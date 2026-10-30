#!/usr/bin/env bash
# 2-UE bring-up for PER-UE RLC buffer control: broker -> gNB -> upt telemetry -> 2 OAI UEs.
#
#   bash scripts/setup_bufctl_2ue_demo.sh
#
# Same order and queue config as scripts/setup_bufctl_1ue_demo.sh (8192 SDU / 8 MB so a
# per-UE limit has room to bind), but the gNB runs in BROKER mode and each UE lives in
# its own netns (ue1, ue2) so the two bearers are independent.
#
# Uses duranta-oai-ue/broker2_netns.py, not broker4_netns.py: the 4-UE adder blocks
# waiting on absent UE3/UE4 streams, so a 2-UE run never syncs with the 4-UE broker.
#
# NO application data -- that is scripts/traffic_2ue.sh.
# bufctl is deliberately NOT loaded: jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py loads it
# on start and unloads it on quit, because while it is loaded the rlc_dl_ctrl hook runs
# on every downlink packet. The upt telemetry codeletset does not touch that hook, so
# the CLI can attach while telemetry keeps running.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; GC=ocudujbpf; UC=durue1; BC=grbroker
NUE=2
kx() { timeout "${3:-30}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2"; }
jx() { timeout "${2:-60}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }
cpx() { kubectl cp "$1" "$NS/$POD:$2" -c "$3" >/dev/null 2>&1 || { echo "ERROR: cp $1 -> $3"; exit 1; }; }

# Pre-flight BEFORE stopping anything, so a bad tree cannot cost a working pipeline.
for f in "$HR/ocudu-jbpf/deploy/gnb_ocudu_jbpf.yml" "$HR/ocudu-jbpf/configs/qos.yml" \
         "$HR/duranta-oai-ue/broker2_netns.py" "$HR/duranta-oai-ue/netns_setup.sh" \
         "$HR/duranta-oai-ue/uecap_ports1.xml" "$HR/telemetry/fwd30450.py"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; echo "  run: git submodule update --init --recursive"; exit 1; }
done
for n in $(seq 1 $NUE); do
  [ -f "$HR/duranta-oai-ue/oaiue${n}_netns.conf" ] || { echo "ERROR: missing oaiue${n}_netns.conf"; exit 1; }
done

echo "== [1/7] stop everything =="
bash "$HR/scripts/stop_demo.sh" 2>&1 | grep -E "stopped|unloaded" | sed 's/^/  /'
kx $UC 'for p in $(pgrep -x nr-uesoftmodem); do kill -9 $p 2>/dev/null; done; sleep 2; echo "  ues stopped"' 40
kx $BC 'for p in $(pgrep -x python3); do kill -9 $p 2>/dev/null; done; sleep 8; echo "  broker stopped"' 40

echo "== [2/7] fresh jrtc-0 (clean app registry + jbpf IPC peer) =="
kubectl delete pod -n $NS jrtc-0 --wait=false >/dev/null 2>&1
for i in $(seq 1 40); do sleep 3; kubectl get pod -n $NS jrtc-0 2>/dev/null | grep -q '2/2 *Running' && break; done
sleep 12; echo "  jrtc-0 ready"

echo "== [3/7] stage broker, UE and gNB configs =="
D=$HR/duranta-oai-ue
cpx $D/broker2_netns.py /tmp/broker2_netns.py $BC
for n in $(seq 1 $NUE); do cpx $D/oaiue${n}_netns.conf /tmp/oaiue${n}_netns.conf $UC; done
cpx $D/uecap_ports1.xml /tmp/uecap_ports1.xml $UC
cpx $D/netns_setup.sh   /tmp/netns_setup.sh   $UC
cp $HR/ocudu-jbpf/deploy/gnb_ocudu_jbpf.yml /tmp/bcfg2.yml
sed -E 's/(queue-size:)[[:space:]]*[0-9]+/\1 8192/; s/(queue-bytes:)[[:space:]]*[0-9]+/\1 8388608/' \
  $HR/ocudu-jbpf/configs/qos.yml >> /tmp/bcfg2.yml
cpx /tmp/bcfg2.yml /tmp/bcfg2.yml $GC
echo "  staged"

echo "== [4/7] broker (2 UE) =="
kx $BC 'cd /tmp && nohup setsid python3 -u broker2_netns.py >/tmp/broker.log 2>&1 </dev/null & disown; sleep 6; echo "  broker procs=$(pgrep -c -x python3) : $(tail -1 /tmp/broker.log)"' 40

echo "== [5/7] gNB (broker mode, dynamic pod-IP NG-U bind) =="
kx $GC 'MYIP=$(ip -o -4 addr show|grep -oE "10\.42\.[0-9]+\.[0-9]+"|grep -v "\.255$"|head -1); sed -i "s/bind_addr: 10\.42\.[0-9.]\+/bind_addr: $MYIP/g" /tmp/bcfg2.yml; : >/tmp/gnb.log; cd /tmp && nohup setsid /usr/local/bin/gnb -c bcfg2.yml >/tmp/gnb.log 2>&1 </dev/null & disown; echo "  gnb started (bind=$MYIP)"' 20
for i in $(seq 1 24); do sleep 3; kx $GC 'grep -ciE "NG setup.*success|Connection to AMF" /tmp/gnb.log' 15 2>/dev/null | grep -qvE '^0$|^$' && break; done
kx $GC 'echo "  gnb procs=$(pgrep -c -x gnb) ngsetup=$(grep -ciE "NG setup.*success|Connection to AMF" /tmp/gnb.log)"' 15

echo "== [6/7] forwarder + upt telemetry codeletset =="
kubectl cp "$HR/telemetry/fwd30450.py" $NS/jrtc-0:/tmp/fwd30450.py -c jrtc >/dev/null 2>&1
jx 'cd /tmp && nohup setsid python3 -u fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 3; echo "  fwd: $(head -1 /tmp/fwd30450.log 2>/dev/null)"' 20
jx 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_demo.yaml --log-level error 2>&1 | grep -iE "error|fatal" | tail -4; echo "  load_done"' 120

echo "== [7/7] netns + 2 UEs =="
kx $UC 'mkdir -p /var/run/netns && bash /tmp/netns_setup.sh >/dev/null 2>&1; cd /opt/oai-nr-ue/bin
  for n in 1 2; do
    : >/tmp/ue$n.log
    nohup setsid chrt -f 50 ip netns exec ue$n ./nr-uesoftmodem -O /tmp/oaiue${n}_netns.conf \
      --band 78 --numerology 1 -r 51 -C 3489420000 --ue-scan-carrier -E >/tmp/ue$n.log 2>&1 </dev/null & disown
    sleep 5
  done; echo "  2 UEs launched"' 60

# Attach can take well over a minute; poll rather than sleeping a fixed 30s.
for i in $(seq 1 30); do
  sleep 5
  N=$(kx $UC 'c=0; for n in 1 2; do ip netns exec ue$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -q 10.45 && c=$((c+1)); done; echo $c' 20 | tr -d '[:space:]')
  [ "$N" = "$NUE" ] && break
done

echo
echo "################ verify ################"
kx $UC 'for n in 1 2; do echo "  ue$n ip=$(ip netns exec ue$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o "10\.45\.0\.[0-9]*" | head -1)"; done' 20
echo "  codelets registered:"
kx $GC 'grep -ao "Codelet created and loaded successfully: [a-z_]*" /tmp/gnb.log | sort -u | sed "s/^/    /"' 25
echo "  DRB queue config:"
kx $GC 'grep -ao "RLC AM configured.*queue_size=[0-9]* queue_size_bytes=[0-9]*" /tmp/gnb.log | tail -1 | sed "s/^/    /"' 25

MISSING=$(kx $UC 'c=0; for n in 1 2; do ip netns exec ue$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -q 10.45 || c=$((c+1)); done; echo $c' 20 | tr -d '[:space:]')
if [ "$MISSING" != "0" ]; then
  echo
  echo "  !! not all UEs attached. Last lines of each UE log:"
  kx $UC 'for n in 1 2; do echo "  --- ue$n ---"; tail -6 /tmp/ue$n.log; done' 25
  echo "  Re-run this script. ZMQ cannot reconnect mid-run, so a partial bring-up needs a"
  echo "  fresh broker+gNB, which this script does every time."
  exit 1
fi

echo
echo "SETUP 2UE DONE. Both UEs attached, telemetry flowing on the next downlink packets."
echo "  traffic     : bash scripts/traffic_2ue.sh start"
echo "  per-UE ctrl : python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py"
echo "  dashboard   : http://localhost:30490/d/upt-userplane"
echo "  stop all    : bash scripts/traffic_2ue.sh stop && bash scripts/stop_demo.sh"
