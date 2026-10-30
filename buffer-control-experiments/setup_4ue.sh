#!/usr/bin/env bash
# 4-UE bring-up for the control-hook experiments: broker -> gNB -> upt telemetry -> 4 OAI UEs.
# Same order and queue config as telemetry/fresh_start_demo.sh (8192 SDU / 8 MB), but the
# gNB uses the broker ports (gnb_ocudu_jbpf.yml unmodified) and each UE runs in netns ueN.
#   bash buffer-control-experiments/setup_4ue.sh
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; GC=ocudujbpf; UC=durue1; BC=grbroker
kx() { timeout "${3:-30}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2"; }
jx() { timeout "${2:-60}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }
cpx() { kubectl cp "$1" "$NS/$POD:$2" -c "$3" >/dev/null 2>&1 || { echo "ERROR: cp $1 -> $3"; exit 1; }; }

echo "== [1/7] stop everything =="
bash "$HR/scripts/stop_demo.sh" 2>&1 | grep -E "stopped|unloaded" | sed 's/^/  /'

echo "== [2/7] fresh jrtc-0 =="
kubectl delete pod -n $NS jrtc-0 --wait=false >/dev/null 2>&1
for i in $(seq 1 40); do sleep 3; kubectl get pod -n $NS jrtc-0 2>/dev/null | grep -q '2/2 *Running' && break; done
sleep 12; echo "  jrtc-0 ready"

echo "== [3/7] stage broker, UE and gNB configs =="
D=$HR/duranta-oai-ue
cpx $D/broker4_netns.py /tmp/broker4_netns.py $BC
for n in 1 2 3 4; do cpx $D/oaiue${n}_netns.conf /tmp/oaiue${n}_netns.conf $UC; done
cpx $D/uecap_ports1.xml /tmp/uecap_ports1.xml $UC
cpx $D/netns_setup.sh /tmp/netns_setup.sh $UC
cp $HR/ocudu-jbpf/deploy/gnb_ocudu_jbpf.yml /tmp/bcfg4.yml
sed -E 's/(queue-size:)[[:space:]]*[0-9]+/\1 8192/; s/(queue-bytes:)[[:space:]]*[0-9]+/\1 8388608/' \
  $HR/ocudu-jbpf/configs/qos.yml >> /tmp/bcfg4.yml
cpx /tmp/bcfg4.yml /tmp/bcfg4.yml $GC
echo "  staged"

echo "== [4/7] broker =="
kx $BC 'cd /tmp && nohup setsid python3 -u broker4_netns.py >/tmp/broker.log 2>&1 </dev/null & disown; sleep 5; echo "  broker procs=$(pgrep -c -x python3)"'

echo "== [5/7] gNB (broker mode) =="
kx $GC 'MYIP=$(ip -o -4 addr show|grep -oE "10\.42\.[0-9]+\.[0-9]+"|grep -v "\.255$"|head -1); sed -i "s/bind_addr: 10\.42\.[0-9.]\+/bind_addr: $MYIP/g" /tmp/bcfg4.yml; : >/tmp/gnb.log; cd /tmp && nohup setsid /usr/local/bin/gnb -c bcfg4.yml >/tmp/gnb.log 2>&1 </dev/null & disown; echo "  gnb started (bind=$MYIP)"' 20
for i in $(seq 1 24); do sleep 3; kx $GC 'grep -ciE "NG setup.*success|Connection to AMF|Available bands" /tmp/gnb.log' 15 2>/dev/null | grep -qvE '^0$|^$' && break; done
kx $GC 'echo "  gnb procs=$(pgrep -c -x gnb) ngsetup=$(grep -ciE "NG setup.*success|Connection to AMF" /tmp/gnb.log)"' 15

echo "== [6/7] forwarder + upt telemetry codeletset =="
kubectl cp "$HR/telemetry/fwd30450.py" $NS/jrtc-0:/tmp/fwd30450.py -c jrtc >/dev/null 2>&1
jx 'cd /tmp && nohup setsid python3 -u fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 3; echo "  fwd: $(head -1 /tmp/fwd30450.log 2>/dev/null)"' 20
jx 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_demo.yaml --log-level error 2>&1 | grep -iE "error|fatal" | tail -4; echo "  load_done"' 120

echo "== [7/7] netns + 4 UEs =="
kx $UC 'mkdir -p /var/run/netns && bash /tmp/netns_setup.sh >/dev/null 2>&1; cd /opt/oai-nr-ue/bin
  for n in 1 2 3 4; do
    : >/tmp/ue$n.log
    nohup setsid chrt -f 50 ip netns exec ue$n ./nr-uesoftmodem -O /tmp/oaiue${n}_netns.conf \
      --band 78 --numerology 1 -r 51 -C 3489420000 --ue-scan-carrier -E >/tmp/ue$n.log 2>&1 </dev/null & disown
    sleep 5
  done; echo "  4 UEs launched"' 60
for i in $(seq 1 24); do
  sleep 5
  N=$(kx $UC 'for n in 1 2 3 4; do ip netns exec ue$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -c 10.45; done' 20 | grep -c 1)
  [ "$N" = 4 ] && break
done
kx $UC 'for n in 1 2 3 4; do echo "  ue$n ip=$(ip netns exec ue$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o "10\.45\.0\.[0-9]*" | head -1)"; done' 20
echo "SETUP_4UE_DONE"
