#!/usr/bin/env bash
# Over-the-air bring-up on the k3d jbpf OCUDU gNB: a USRP is the radio, COTS phones or modems are the UEs.
# The over-the-air counterpart of setup_zmq_chan_demo.sh. Walk-through: docs/open-ai-ran-tutorial/run_ota_cots.md
#
#   bash scripts/setup_ota.sh [options]
#
# Options:
#   --sdr b210|x310|n310   the radio file ota/ru_<sdr>.yml (default b210)
#   --sims FILE            add or update these SIMs in the core, IMSI Ki OPc per line (ota/sims.conf). SIMs
#                          programmed as 999700000000001..008 with the testbed's Ki/OPc need no file.
#   --telemetry            fresh jrtc-0, then the upt and dashboard xApps loaded before the UEs attach (Grafana,
#                          and each UE's identity from IMSI / IP in the core down to its C-RNTI)
#   --ues N                wait for N UEs to register (default 1; 0 = do not wait)
#   --sdr-nic IF           x310/n310: the host NIC cabled to the SDR. It is moved into the gNB pod (the docker
#                          group is enough, no sudo) and goes back to the host when the pod is deleted.
#   --sdr-ip CIDR          that NIC's address in the pod (default 192.168.40.1/24 x310, 192.168.20.1/24 n310)
#   --check                pre-flight only: the gNB image, the configs, and whether the SDR answers (with
#                          --sdr-nic, after moving the NIC). Stops nothing.
#
# The cell is on air as soon as the gNB starts. Keep the UEs in airplane mode until this script asks for
# them: with --telemetry the dashboard xApp has to be loaded before a UE attaches to follow its identity.
# Needs the gNB container (ocudujbpf) on ocudu-gnb-jbpf:uhd, built from ota/gnb.Dockerfile.
# Extra gNB command-line options: GNB_ARGS="ru_sdr --tx_gain 60" bash ...
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; GC=ocudujbpf
O=$HR/ota
usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit "${1:-1}"; }

SDR=b210; SIMS=""; TELEM=0; NUE=1; NIC=""; NIC_IP=""; CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sdr)       SDR="$2"; shift 2 ;;
    --sims)      SIMS="$2"; shift 2 ;;
    --telemetry) TELEM=1; shift ;;
    --ues)       NUE="$2"; shift 2 ;;
    --sdr-nic)   NIC="$2"; shift 2 ;;
    --sdr-ip)    NIC_IP="$2"; shift 2 ;;
    --check)     CHECK=1; shift ;;
    -h|--help)   usage 0 ;;
    *) echo "unknown option $1"; usage ;;
  esac
done
case "$SDR" in
  b210) [ -z "$NIC" ] || { echo "ERROR: --sdr-nic is for the network SDRs (x310, n310)"; exit 1; } ;;
  x310) : "${NIC_IP:=192.168.40.1/24}" ;;
  n310) : "${NIC_IP:=192.168.20.1/24}" ;;
  *) echo "ERROR: --sdr b210|x310|n310"; exit 1 ;;
esac
[[ "$NUE" =~ ^[0-9]+$ ]] || { echo "ERROR: --ues N"; exit 1; }
RU=$O/ru_$SDR.yml

kx() { timeout "${3:-30}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2"; }
jx() { timeout "${2:-60}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }
cpx() { kubectl cp "$1" "$NS/$POD:$2" -c "$3" >/dev/null 2>&1 || { echo "ERROR: cp $1 -> $3"; exit 1; }; }

# ---- pre-flight, before stopping anything -----------------------------------------------------
for f in "$O/gnb_ota_tdd_n78_20mhz.yml" "$RU"; do [ -f "$f" ] || { echo "ERROR: missing $f"; exit 1; }; done
# uhd_find_devices arguments: type= and addr= from the radio file, so an edited address carries over
FIND=$(grep -E '^[[:space:]]*device_args:' "$RU" | grep -oE '(type|addr)=[^,[:space:]]+' | paste -sd, -)
UTYPE=$(echo "$FIND" | grep -oE 'type=[^,]+' | cut -d= -f2)
SDR_ADDR=$(echo "$FIND" | grep -oE 'addr=[^,]+' | cut -d= -f2)
if [ -n "$SIMS" ]; then
  [ -f "$SIMS" ] || { echo "ERROR: SIM file not found: $SIMS"; exit 1; }
  BAD=$(grep -vE '^[[:space:]]*(#|$)' "$SIMS" |
        grep -vE '^[[:space:]]*99970[0-9]{10}[[:space:]]+[0-9a-fA-F]{32}[[:space:]]+[0-9a-fA-F]{32}[[:space:]]*$')
  [ -z "$BAD" ] || { echo "ERROR: $SIMS: each line is <IMSI: 99970 + 10 digits> <Ki: 32 hex> <OPc: 32 hex>, no comments:"
                     echo "$BAD" | sed 's/^/  /'; exit 1; }
fi
timeout 20 kubectl get pod -n $NS $POD >/dev/null || {
  echo "ERROR: cannot reach pod $NS/$POD with KUBECONFIG=$KUBECONFIG"
  echo "  export KUBECONFIG=\"\$(k3d kubeconfig write janus-cluster)\"   (and do not run with sudo)"; exit 1; }
kx $GC 'ldd /usr/local/bin/gnb 2>/dev/null | grep -q libuhd && command -v uhd_find_devices >/dev/null' 15 2>/dev/null || {
  IMG=$(kubectl get pod -n $NS $POD -o jsonpath="{.spec.ephemeralContainers[?(@.name==\"$GC\")].image}" 2>/dev/null)
  if [ -z "$IMG" ]; then echo "ERROR: the gNB pod has no '$GC' container."
  else echo "ERROR: the gNB container '$GC' runs $IMG, which has no UHD driver."; fi
  echo "  Build and import ocudu-gnb-jbpf:uhd (ota/gnb.Dockerfile), then recreate the gNB pod with it:"
  echo "  docs/open-ai-ran-tutorial/run_ota_cots.md, steps 1 and 3."; exit 1; }
if [ "$SDR" != b210 ]; then
  RM=$(cat /proc/sys/net/core/rmem_max); WM=$(cat /proc/sys/net/core/wmem_max)
  [ "$RM" -ge 33554432 ] && [ "$WM" -ge 33554432 ] ||
    echo "  !! host net.core.rmem_max / wmem_max are $RM / $WM; UHD wants 33554432 over 10 GbE (expect overflows):
     sudo sysctl -w net.core.rmem_max=33554432 net.core.wmem_max=33554432"
fi

usb_live() {  # B210: the kernel's live /dev/bus/usb in the gNB container
  # The container's /dev is a copy of the k3d node's /dev, made when the node started. A USRP plugged in
  # later is missing from it, and so is a B210 that re-enumerates after its firmware load. devtmpfs is the
  # kernel's own /dev and always current, so bind its bus/usb over the copy. Lasts as long as the container.
  kx $GC 'if ! mountpoint -q /dev/bus/usb; then
      mkdir -p /run/hostdev /dev/bus/usb && { mountpoint -q /run/hostdev || mount -t devtmpfs none /run/hostdev; } &&
      mount --bind /run/hostdev/bus/usb /dev/bus/usb || exit 1
    fi
    E=$(lsusb 2>/dev/null | grep -iE " ID (2500|3923):"); echo "  /dev/bus/usb: live; Ettus/NI USB devices: ${E:-none}"' 20 ||
    { echo "ERROR: could not mount the live /dev/bus/usb in '$GC' (the container must be privileged)"; exit 1; }
}

nic_to_pod() {  # X310/N310: host NIC $NIC into the gNB pod, $NIC_IP, jumbo frames
  if ! kx $GC "ip link show $NIC >/dev/null 2>&1" 10; then
    ip link show "$NIC" >/dev/null 2>&1 || { echo "ERROR: no NIC $NIC on the host or in the gNB pod"; exit 1; }
    NETNS=$(kx $GC 'readlink /proc/self/ns/net' 10 | tr -d '\r')
    [[ "$NETNS" == net:* ]] || { echo "ERROR: cannot read the gNB pod's network namespace"; exit 1; }
    # A privileged helper in the host's PID and network namespaces finds a process living in the pod's
    # network namespace and moves the NIC there. A physical NIC returns to the host when that namespace ends.
    docker run --rm --privileged --net=host --pid=host --entrypoint bash ocudu-gnb-jbpf:uhd -c \
      'for p in /proc/[0-9]*; do
         [ "$(readlink $p/ns/net 2>/dev/null)" = "'"$NETNS"'" ] && exec ip link set dev '"$NIC"' netns "${p#/proc/}"
       done; echo "no process in '"$NETNS"'"; exit 1' ||
      { echo "ERROR: could not move $NIC into the gNB pod"; exit 1; }
  fi
  kx $GC "ip link set $NIC mtu 9000 up && ip addr replace $NIC_IP dev $NIC &&
          echo \"  $NIC in the gNB pod: $NIC_IP, mtu \$(cat /sys/class/net/$NIC/mtu)\"" 15 ||
    { echo "ERROR: could not configure $NIC in the gNB pod"; exit 1; }
}

radio_access() {
  if [ "$SDR" = b210 ]; then usb_live; return; fi
  [ -n "$NIC" ] && nic_to_pod
  kx $GC "ping -c 2 -W 1 $SDR_ADDR >/dev/null 2>&1" 15 || {
    echo "ERROR: the gNB pod cannot reach the $SDR at $SDR_ADDR."
    [ -z "$NIC" ] && echo "  Pass --sdr-nic <host NIC cabled to the SDR>, or fix addr= in ota/ru_$SDR.yml."
    exit 1; }
  echo "  $SDR at $SDR_ADDR answers ping"
}

sdr_probe() {
  # A B210's first open loads its firmware, after which it re-enumerates: probe twice, the second must list it.
  OUT=$(kx $GC "uhd_find_devices --args '$FIND' >/dev/null 2>&1; uhd_find_devices --args '$FIND' 2>&1" 120)
  if echo "$OUT" | grep -qE "type: $UTYPE"; then
    echo "  found: $(echo "$OUT" | grep -E '^ +(product|serial|addr|type):' | sed 's/^ *//' | paste -sd' ' -)"
  else
    echo "ERROR: no $SDR answered (uhd_find_devices --args $FIND):"; echo "$OUT" | tail -4 | sed 's/^/  /'
    [ "$SDR" = b210 ] && echo "  Plugged into a USB 3 port and powered? On the host: lsusb | grep -iE 'ettus|2500:'"
    exit 1
  fi
}

if [ "$CHECK" = 1 ]; then
  echo "== check: $SDR ($FIND) =="
  echo "  gNB image: UHD driver present"
  if kx $GC 'pgrep -x gnb >/dev/null' 10; then
    echo "  a gNB is running and holds the radio; not probing the SDR"
  else
    radio_access; sdr_probe
  fi
  echo "CHECK OK."; exit 0
fi

echo "== [1/7] stop everything =="
bash "$HR/scripts/stop_demo.sh" 2>&1 | grep -E "stopped|unloaded" | sed 's/^/  /'

echo "== [2/7] core: subscribers =="
# 999700000000001..008 cloned from the template subscriber (the testbed's Ki/OPc), as setup_zmq_chan_demo.sh
# does; then each --sims line, added or with its Ki/OPc updated. Slice, DNN and AMBR come from the template.
SIMJS=""
[ -n "$SIMS" ] && SIMJS=$(grep -vE '^[[:space:]]*(#|$)' "$SIMS" | awk '{printf "[\"%s\",\"%s\",\"%s\"],", $1, tolower($2), tolower($3)}')
MONGO=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name | grep '^open5gs-mongodb-')
timeout 60 kubectl exec -n open5gs "$MONGO" -- mongosh --quiet open5gs --eval "
  const base = db.subscribers.findOne({imsi: '999700000000001'});
  if (!base) { print('  ERROR: template subscriber 999700000000001 missing'); quit(1); }
  let added = 0, updated = 0;
  for (let i = 1; i <= 8; i++) {
    const imsi = '99970000000000'.slice(0, 15 - String(i).length) + i;
    if (db.subscribers.findOne({imsi})) continue;
    const s = Object.assign({}, base); delete s._id; s.imsi = imsi;
    db.subscribers.insertOne(s); added++;
  }
  for (const [imsi, k, opc] of [$SIMJS]) {
    if (db.subscribers.findOne({imsi})) {
      db.subscribers.updateOne({imsi}, {\$set: {'security.k': k, 'security.opc': opc, 'security.op': null}}); updated++;
    } else {
      const s = Object.assign({}, base); delete s._id; s.imsi = imsi;
      s.security = Object.assign({}, base.security, {k: k, opc: opc, op: null});
      db.subscribers.insertOne(s); added++;
    }
  }
  print('  ' + added + ' added, ' + updated + ' updated; ' + db.subscribers.countDocuments({}) + ' subscribers');" ||
  { echo "ERROR: could not provision subscribers"; exit 1; }

if [ "$TELEM" = 1 ]; then
  echo "== [3/7] fresh jrtc-0 (clean app registry + jbpf IPC peer) =="
  kubectl delete pod -n $NS jrtc-0 --wait=false >/dev/null 2>&1
  for i in $(seq 1 40); do sleep 3; kubectl get pod -n $NS jrtc-0 2>/dev/null | grep -q '2/2 *Running' && break; done
  sleep 12; echo "  jrtc-0 ready"
else
  echo "== [3/7] telemetry: skipped (pass --telemetry to load the upt and dashboard xApps) =="
fi

echo "== [4/7] radio: $SDR ($FIND) =="
radio_access
sdr_probe

echo "== [5/7] gNB: gnb_ota_tdd_n78_20mhz.yml + ru_$SDR.yml =="
cpx "$O/gnb_ota_tdd_n78_20mhz.yml" /tmp/ota_gnb.yml $GC
cpx "$RU" /tmp/ota_ru.yml $GC
kx $GC 'MYIP=$(ip -o -4 addr show|grep -oE "10\.42\.[0-9]+\.[0-9]+"|grep -v "\.255$"|head -1); sed -i "s/bind_addr: 10\.42\.[0-9.]\+/bind_addr: $MYIP/g" /tmp/ota_gnb.yml; : >/tmp/gnb.log; cd /tmp && nohup setsid stdbuf -oL /usr/local/bin/gnb -c ota_gnb.yml -c ota_ru.yml '"${GNB_ARGS:-}"' >/tmp/gnb.stdout 2>&1 </dev/null & disown; echo "  gnb started (bind=$MYIP)"' 20
ST=starting
for i in $(seq 1 40); do
  sleep 3
  ST=$(kx $GC 'if ! pgrep -x gnb >/dev/null; then echo dead; elif grep -aq "==== gNB started ===" /tmp/gnb.stdout; then echo up; else echo starting; fi' 15)
  [ "$ST" = up ] || [ "$ST" = dead ] && break
done
if [ "$ST" != up ]; then
  echo "ERROR: the gNB is $ST. Last lines of /tmp/gnb.stdout:"
  kx $GC 'grep -avE "JBPF|JANUS" /tmp/gnb.stdout | tail -15' 15 | sed 's/^/  /'
  exit 1
fi
kx $GC 'grep -aE "Cell pci=|Connection to AMF.*completed" /tmp/gnb.stdout | sed "s/^/  /"' 15

if [ "$TELEM" = 1 ]; then
  echo "== [6/7] telemetry: codelets and xApps =="
  # Codelets live in the gNB process, so they load now: after this gNB starts, before any UE attaches.
  # The dashboard xApp's ue_contexts and ngap codelets only see a UE's identities as it attaches.
  kx $GC 'mkdir -p /codelets' 10
  # bufctl is only staged: jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py loads it when it starts
  for d in upt ue_contexts mac rlc pdcp rrc ngap bufctl; do cpx "$HR/jrtc-apps/codelets/$d" /codelets/ $GC; done
  kubectl cp "$HR/telemetry/fwd30450.py" $NS/jrtc-0:/tmp/fwd30450.py -c jrtc >/dev/null 2>&1
  jx 'cd /tmp && nohup setsid python3 -u fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 3; echo "  fwd: $(head -1 /tmp/fwd30450.log 2>/dev/null)"' 20
  jx 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_demo.yaml --log-level error 2>&1 | grep -iE "error|fatal" | tail -4; echo "  upt codeletset loaded"' 120
  jx 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/dashboard/deployment_ocudu.yaml --log-level error 2>&1 | grep -iE "error|fatal" | tail -4; echo "  dashboard xApp loaded"' 120
  # jrtc-ctl reports "loaded" even when an xApp dies importing a module
  sleep 3; kubectl logs -n $NS jrtc-0 -c jrtc --since=3m 2>/dev/null | grep -aE "Failed to import|Python app terminated" | tail -2 | sed 's/^/  !! /'
else
  echo "== [6/7] telemetry: skipped =="
fi

echo "== [7/7] UEs =="
echo
echo "  >>> The cell is on air. Take the UE(s) out of airplane mode now. <<<"
echo
registered() {  # UEs registered with a PDU session since this gNB connected, as "ip=n,..." from the core
  bash "$HR/scripts/core_ue_info.sh" --tcp-map 2>/dev/null | tr ',' '\n' | grep -c '='
}
if [ "$NUE" -gt 0 ]; then
  WAIT=${UE_WAIT:-300}; A=0; LAST=-1
  for i in $(seq 1 $((WAIT / 5))); do
    sleep 5
    kx $GC 'pgrep -x gnb >/dev/null' 10 || { echo "ERROR: the gNB exited:"; kx $GC 'grep -avE "JBPF|JANUS" /tmp/gnb.stdout | tail -10' 15 | sed 's/^/  /'; exit 1; }
    A=$(registered); [ "$A" != "$LAST" ] && echo "  $A/$NUE registered ($((i * 5)) s)"; LAST=$A
    [ "$A" -ge "$NUE" ] && break
  done
  if [ "$A" -lt "$NUE" ]; then
    echo
    echo "  !! $A/$NUE UEs registered after ${WAIT}s. The cell stays up: toggle airplane mode on a UE that"
    echo "     did not attach, and see run_ota_cots.md, Troubleshooting. Then: bash scripts/core_ue_info.sh"
  fi
fi

echo
echo "################ UEs ################"
if [ "$TELEM" = 1 ]; then
  # the core's IMSI / IP / NGAP IDs per UE, which the dashboard xApp joins to its gNB-side UE contexts
  bash "$HR/scripts/core_ue_info.sh" 2>&1 | sed 's/^/  /'
else
  echo "  $(bash "$HR/scripts/core_ue_info.sh" --tcp-map 2>/dev/null | tr ',' ' ')   (UE IP=UE number)"
fi
echo
echo "SETUP DONE. gNB on air with the $SDR."
echo "  console : bash scripts/gnb_console.sh       (one row per UE every second: CQI, MCS, BLER, rate)"
echo "  traffic : run_ota_cots.md step 8            (iperf3 server on the UPF, iperf3 -R on the UE)"
echo "  Grafana : http://localhost:30490/d/upt-userplane"
echo "  stop    : bash scripts/stop_demo.sh"
