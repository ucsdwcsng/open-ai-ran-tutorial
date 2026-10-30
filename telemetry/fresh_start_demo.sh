#!/usr/bin/env bash
# fresh_start.sh -- clean, ordered bring-up of the OCUDU gNB + UE + jrtc 'upt' pipeline.
#
# WHY ORDER MATTERS: the gNB's jbpf agent binds to whatever jrtc service is up when
# the gNB starts. So jrtc MUST be fresh BEFORE the gNB starts, otherwise codelet
# loads fail with device 400/500 errors. Also the gNB pod gets a DYNAMIC IP, so the
# NG-U bind_addr is patched to the current pod IP at each start.
#
# Order: kill gNB+UE -> fresh jrtc -> gNB -> forwarder + codeletset load -> UE -> verify.
# Idempotent: safe to re-run any time. Usage:  bash fresh_start.sh
set -u
# Repo root, derived from this script's own location: works from any clone path,
# any user, any working directory.
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="${K3D_CLUSTER:-janus-cluster}"

# The gNB config lives in the ocudu-jbpf SUBMODULE. A clone without submodules
# would otherwise fail later with a confusing sed/cp error.
for f in "$HR/ocudu-jbpf/deploy/gnb_ocudu_jbpf.yml" "$HR/ocudu-jbpf/configs/qos.yml"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; echo "  run: git submodule update --init --recursive"; exit 1; }
done
for f in "$HR/telemetry/fwd30450.py" "$HR/telemetry/tcp_probe.py" \
         "$HR/duranta-oai-ue/oaiue_zmq.conf" "$HR/duranta-oai-ue/uecap_ports1.xml"; do
  [ -f "$f" ] || { echo "ERROR: missing repo file $f"; exit 1; }
done
NS=ran; POD=srs-gnb-du1-0; GC=ocudujbpf; UC=durue1
APP=/apps/upt/deployment_demo.yaml

# kx/jx: exec into a container with a hard timeout so a backgrounded daemon start
# (nohup setsid ... & disown) can't wedge the exec -- the daemon survives the timeout.
kx() { timeout "${3:-30}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2"; }
jx() { timeout "${2:-60}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }

echo "== [1/6] kill gNB + UE =="
kx $GC 'for i in $(seq 1 8); do pkill -9 -x gnb 2>/dev/null; sleep 1; [ "$(ss -uan 2>/dev/null|grep -c ":2152 ")" = 0 ]&&break; done; echo "  gnb killed, :2152=$(ss -uan 2>/dev/null|grep -c ":2152 ")"' 30
kx $UC 'pkill -9 -x nr-uesoftmodem 2>/dev/null; sleep 1; echo "  ue killed"' 20

echo "== [2/6] fresh jrtc-0 (so the gNB agent binds a live jrtc) =="
kubectl delete pod -n $NS jrtc-0 --wait=false >/dev/null 2>&1
for i in $(seq 1 40); do sleep 3; kubectl get pod -n $NS jrtc-0 2>/dev/null | grep -q '2/2 *Running' && break; done
sleep 12   # let the service + decoder settle
echo "  jrtc-0 ready"

echo "== [3/6] start gNB (dynamic pod-IP NG-U bind) =="
sed 's#tx_port=tcp://127.0.0.1:2101,rx_port=tcp://127.0.0.1:2100#tx_port=tcp://127.0.0.1:4556,rx_port=tcp://127.0.0.1:4557#' \
  $HR/ocudu-jbpf/deploy/gnb_ocudu_jbpf.yml > /tmp/bcfg.yml
# RLC queue must exceed the largest byte-cap we sweep (4096 SDU x 1500 = 6.144 MB) so the
# CAP -- not the queue -- is the binding limit. 8192 SDU / 8 MB gives headroom for all caps.
# PDCP discard timer for 5QI 9 (default traffic). Default 1500 ms (shipped value); -1 = infinity.
# 1500 ms drops anything queued > 1.5 s; the buffer-control-experiments trace runs set -1 (run_expD.sh).
# Valid values: 10..1500 (3GPP set) or -1. Override: PDCP_DISCARD_MS=-1 bash fresh_start_demo.sh
PDCP_DISCARD_MS=${PDCP_DISCARD_MS:-1500}
sed -E 's/(queue-size:)[[:space:]]*[0-9]+/\1 8192/; s/(queue-bytes:)[[:space:]]*[0-9]+/\1 8388608/' \
  $HR/ocudu-jbpf/configs/qos.yml \
  | sed -E "/five_qi: 9$/,/five_qi:/ s/(discard_timer:)[[:space:]]*-?[0-9]+/\1 $PDCP_DISCARD_MS/" >> /tmp/bcfg.yml
kubectl cp /tmp/bcfg.yml $NS/$POD:/tmp/bcfg.yml -c $GC >/dev/null 2>&1
kx $GC 'MYIP=$(ip -o -4 addr show|grep -oE "10\.42\.[0-9]+\.[0-9]+"|grep -v "\.255$"|head -1); sed -i "s/bind_addr: 10\.42\.[0-9.]\+/bind_addr: $MYIP/g" /tmp/bcfg.yml; : >/tmp/gnb.log; cd /tmp && nohup setsid /usr/local/bin/gnb -c bcfg.yml >/tmp/gnb.log 2>&1 </dev/null & disown; echo "  gnb started (bind=$MYIP)"' 20
for i in $(seq 1 24); do sleep 3; kx $GC 'grep -ciE "NG setup.*success|Connection to AMF|Available bands"' 15 2>/dev/null | grep -qvE '^0$|^$' && break; done
kx $GC 'echo "  gnb: bind_err=$(grep -c "Cannot assign\|Unable to allocate" /tmp/gnb.log) ngsetup=$(grep -ciE "NG setup.*success|Connection to AMF") hooks=$(grep -c "rlc_dl_ctrl" /tmp/gnb.log)"' 15

echo "== [4/6] forwarder + load upt codeletset =="
kubectl cp "$HR/telemetry/fwd30450.py" $NS/jrtc-0:/tmp/fwd30450.py -c jrtc \
  || { echo "ERROR: could not stage fwd30450.py into jrtc-0"; exit 1; }
jx 'cd /tmp && nohup setsid python3 -u fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 3; echo "  fwd: $(head -1 /tmp/fwd30450.log 2>/dev/null)"' 20
jx 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_demo.yaml --log-level error 2>&1 | grep -iE "error|fatal|loaded app" | tail -4; echo "  load_done"' 120

echo "== [5/6] start UE + wait for attach =="
# Stage the UE config + capability file. The UE container is ephemeral, so a pod
# recreate wipes /tmp; a missing uecap file makes nr-uesoftmodem abort with
# "Assertion (max_mimo_layers > 0) failed!".
for f in oaiue_zmq.conf uecap_ports1.xml; do
  kubectl cp "$HR/duranta-oai-ue/$f" $NS/$POD:/tmp/$f -c $UC \
    || { echo "ERROR: could not stage $f into $UC"; exit 1; }
done
echo "  staged UE config + uecap file"
kx $UC 'cd /opt/oai-nr-ue/bin; : >/tmp/ue_direct.log; nohup setsid chrt -f 50 ./nr-uesoftmodem -O /tmp/oaiue_zmq.conf --band 78 --numerology 1 -r 51 -C 3489420000 --ue-scan-carrier -E >/tmp/ue_direct.log 2>&1 </dev/null & disown; echo "  ue started"' 20
ip=""
for i in $(seq 1 24); do sleep 5; ip=$(kx $UC 'ip -o -4 addr show oaitun_ue1 2>/dev/null|grep -o "10.45[0-9.]*"|head -1' 15 2>/dev/null | tr -d "[:space:]"); [ -n "$ip" ]&&break; done
echo "  UE_IP=${ip:-NONE}"

echo "== [6/7] tcp_probe (TCP app-level metrics -> bottom dashboard row) =="
# tcp_probe.py is a HOST-side script: it runs tcpdump inside a privileged 'tcpdumper'
# ephemeral container in the UPF pod (ogstun) and writes tcp_* to VM. Ensure that
# container exists (wiped by any pod/cluster restart), then launch the probe for this UE.
if [ -n "$ip" ]; then
  export PATH="$HOME/.local/bin:$PATH"; export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "$CLUSTER" 2>/dev/null)}"
  UPFP=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name 2>/dev/null | grep '^open5gs-upf-')
  kubectl get pod -n open5gs "$UPFP" -o jsonpath='{.status.ephemeralContainerStatuses[*].name}' 2>/dev/null | grep -q tcpdumper \
    || { kubectl debug -n open5gs "$UPFP" --image=nicolaka/netshoot --image-pull-policy=IfNotPresent \
           -c tcpdumper --target=open5gs-upf --profile=sysadmin -- sleep infinity >/dev/null 2>&1; sleep 12; }
  pkill -f "[t]cp_probe.py" 2>/dev/null
  setsid python3 $HR/telemetry/tcp_probe.py --map ${ip}=0 --container tcpdumper >/tmp/tcp_probe.log 2>&1 </dev/null & disown
  sleep 3; echo "  tcp_probe: $(grep -m1 -iE 'writing to|error|no ' /tmp/tcp_probe.log 2>/dev/null || echo started)"
else
  echo "  (UE not up -- skipping tcp_probe)"
fi

echo "== [7/7] result =="
[ -n "$ip" ] && echo "PIPELINE UP -- gNB(new binary+hooks) + upt codeletset + UE $ip + tcp_probe" \
             || echo "gNB+codelets up, UE not attached yet -- check: kubectl exec -n ran $POD -c $UC -- tail /tmp/ue_direct.log"
