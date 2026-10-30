#!/usr/bin/env bash
# Multi-UE OCUDU+Duranta bring-up, replicating README §6 / restart-rt.sh, but loading the
# DEMO codeletset (deployment_demo.yaml) so rlc_ctrl sweeps UE0 while UE1..3 stay at the
# gNB default (4096 SDUs). Order: jrtc -> fwd -> broker -> gNB -> codeletset -> netns + 4 UEs.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)"
P=srs-gnb-du1-0; NS=ran
k() { timeout "${3:-60}" kubectl exec -n $NS $P -c "$1" -- bash -c "$2"; }
j() { timeout "${2:-90}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }

echo "===== 1. STOP radio chain (UEs -> gNB -> broker) ====="
k durue1    'pkill -9 -x nr-uesoftmodem 2>/dev/null; sleep 2; echo ue_stopped' 40
k ocudujbpf 'pkill -9 -x gnb 2>/dev/null; sleep 2; : >/tmp/gnb.stdout; : >/tmp/gnb.log; echo gnb_stopped' 40
k gnb       'pkill -9 -x gnb 2>/dev/null; pkill -9 -x srsue 2>/dev/null; sleep 1; echo srsran_stopped' 30
k grbroker  'pkill -9 -x python3 2>/dev/null; sleep 1; echo broker_stopped' 20

echo "===== 2. FRESH jrtc-0 (clean app registry + IPC peer) ====="
kubectl delete pod -n $NS jrtc-0 --force --grace-period=0 >/dev/null 2>&1
for i in $(seq 1 40); do sleep 3; [ "$(kubectl get pod -n $NS jrtc-0 --no-headers 2>/dev/null | awk '{print $2" "$3}')" = "2/2 Running" ] && break; done
sleep 10; echo "  jrtc-0: $(kubectl get pod -n $NS jrtc-0 --no-headers 2>/dev/null | awk '{print $2, $3}')"

echo "===== 3. fwd.py in jrtc (jrtc-ctl bridge) ====="
kubectl cp "$HR/ocudu-jbpf/deploy/fwd.py" $NS/jrtc-0:/tmp/fwd.py -c jrtc >/dev/null 2>&1
j 'pkill -x python3 2>/dev/null; nohup setsid python3 -u /tmp/fwd.py >/tmp/fwd.log 2>&1 </dev/null & disown; sleep 3; grep -qi 76F2 /proc/net/tcp && echo "  fwd LISTENING" || echo "  fwd: $(head -1 /tmp/fwd.log)"' 30

echo "===== 4. BROKER (4-UE, RT prio) ====="
k grbroker 'cd /tmp && nohup setsid python3 -u broker4_netns.py >/tmp/broker.log 2>&1 </dev/null & disown; sleep 8; echo "  broker=$(pgrep -c -x python3) : $(tail -1 /tmp/broker.log)"' 30

echo "===== 5. gNB (broker mode, dynamic pod-IP NG-U bind) ====="
k ocudujbpf 'MYIP=$(ip -o -4 addr show|grep -oE "10\.42\.[0-9]+\.[0-9]+"|grep -v "\.255$"|head -1);
  sed -i "s/bind_addr: 10\.42\.[0-9.]\+/bind_addr: $MYIP/g" /tmp/gnb_ocudu_jbpf.yml;
  cd /tmp && nohup setsid /usr/local/bin/gnb -c gnb_ocudu_jbpf.yml >/tmp/gnb.stdout 2>&1 </dev/null & disown;
  echo "  gnb started (bind=$MYIP)"' 30
for i in $(seq 1 24); do sleep 3; k ocudujbpf 'grep -acE "Connected to AMF|NG setup.*success"' 15 2>/dev/null | grep -qvE '^0$|^$' && break; done
k ocudujbpf 'echo "  gnb=$(pgrep -c -x gnb) amf=$(grep -acE "Connected to AMF|NG setup.*success" /tmp/gnb.log /tmp/gnb.stdout 2>/dev/null | paste -sd+ | bc 2>/dev/null || grep -acE "Connected to AMF" /tmp/gnb.stdout)"' 15

echo "===== 6. LOAD demo codeletset (sweep on UE0) ====="
j 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_demo.yaml --log-level error 2>&1 | grep -iE "error|fatal|loaded app" | tail -4; echo "  load_done"' 150
sleep 4
k ocudujbpf 'echo "  registered codelets=$(grep -acE "Codelet created and loaded successfully|Registered codelet" /tmp/gnb.stdout /tmp/gnb.log 2>/dev/null | paste -sd+ | bc 2>/dev/null)"' 15

echo "===== 7. netns + 4 UEs (RT prio) ====="
k durue1 'mkdir -p /var/run/netns && bash /tmp/netns_setup.sh 2>&1 | tail -4' 40
k durue1 'cd /opt/oai-nr-ue/bin
  for n in 1 2 3 4; do
    nohup setsid chrt -f 50 ip netns exec ue$n ./nr-uesoftmodem -O /tmp/oaiue${n}_netns.conf \
        --band 78 --numerology 1 -r 51 -C 3489420000 --ue-scan-carrier -E >/tmp/ue$n.log 2>&1 </dev/null & disown
    sleep 5
  done; echo "  4 UEs launched"' 60

echo "===== 8. WAIT + verify attach ====="
sleep 30
k durue1 'for n in 1 2 3 4; do
    ip=$(ip netns exec ue$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o "10.45[0-9.]*" | head -1)
    to=$(grep -c "channel timeout" /tmp/ue$n.log 2>/dev/null)
    echo "  ue$n ip=${ip:-NONE} timeouts=$to"
  done' 40
echo "BRINGUP_DONE"
