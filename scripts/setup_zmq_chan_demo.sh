#!/usr/bin/env bash
# N-UE ZMQ bring-up with a per-UE channel on the k3d jbpf OCUDU gNB:
# broker -> gNB -> N OAI (Duranta) UEs, each in its own netns with its own channel.
# The k3d counterpart of ocudu-edgeric's start_gnb_zmq.sh + start_oai_ues.sh <c> --mobility <file>.
#
#   bash scripts/setup_zmq_chan_demo.sh <N> [options] [-- broker args]
#
# Options:
#   --channel FILE   per-UE channel table (default duranta-oai-ue/ue_channel.conf: static 30 dB,
#                    CQI 15 on every UE). duranta-oai-ue/ue_channel_mobility.conf moves the UEs.
#   --clean          no channel at all: no added noise, no gNB noise floor
#   --traces FILE    channel traces: FILE (e.g. duranta-oai-ue/ue_traces_4ue.conf) says which trace each UE
#                    replays from attach, and every UE can switch traces mid-run (scripts/trace_ctl.sh).
#                    Each UE's --channel line (else static 30 dB) is its base channel, before a trace
#                    and after trace_ctl.sh stop. Needs the C++ broker. --allow-low-snr: accept traces
#                    below 0 dB SNR.
#   --telemetry      fresh jrtc-0, then the upt and dashboard xApps loaded before the UEs attach (Grafana,
#                    and each UE's identity from IMSI / IP in the core down to its C-RNTI: core_ue_info.sh)
#   --edgeric        fresh jrtc-0, then the EdgeRIC-RT per-TTI scheduling loop (edgeric_rt codelets and the
#                    rt_bridge xApp: scripts/edgeric_rt.sh load) once the gNB is up. Without --telemetry the
#                    bridge is the only Python xApp in jrtc, which keeps the loop tight.
#   --ran            the RAN half of a run, for a terminal of its own: stages 1-6 (stop everything, the
#                    subscribers in the core, the broker for N UEs, the gNB, --telemetry / --edgeric), then
#                    the gNB console (scripts/gnb_console.sh; Ctrl-C leaves it, the RAN keeps running)
#   --ues            the UE half, from another terminal: stage 7 against the broker and gNB of the last
#                    --ran, with the same N and the UE options (--channel, --traces, --no-tune). The UEs can
#                    join a RAN once; to restart them, restart the RAN.
#   --no-tune       skip scripts/zmq_tune.sh (SCHED_FIFO for gNB/broker + one NUMA node)
#   --tcp            all ZMQ links over tcp (UEs on the veths, gNB on loopback); default: ipc sockets
#   --broker cpp|py  C++ single-threaded broker (default, duranta-oai-ue/broker_cpp) or the GNU Radio one
#   -- ARGS          passed to the broker, e.g. -- --dl-gain-db 0 -6
#
# UE i (1..N): netns ue<i>, veth 10.201.<i>.1 <-> .2, IMSI 99970000000000<i> (provisioned in the
# core on demand), log /tmp/ues/ue<i>.log in the UE container.
# Every run restarts broker + gNB + UEs: ZMQ cannot reconnect mid-run, and the broker's UL sum needs
# exactly N live UEs, so changing N means re-running this script with the new N (both halves with
# --ran / --ues).
#
# Extra nr-uesoftmodem options: UE_ARGS="--num-dl-actors 8" bash ...
# Extra gNB command-line options: GNB_ARGS="expert_phy --max_request_headroom_slots 4" bash ...
# Needs the channel-capable UE (duranta-ue @ dev/channel-implementation) in the UE container
# (default durue1; override with UE_C=...). Traffic: scripts/traffic_nue.sh.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; GC=ocudujbpf; UC="${UE_C:-durue1}"; BC=grbroker
D=$HR/duranta-oai-ue
IMSI_BASE=999700000000000
usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit "${1:-1}"; }

[[ $# -ge 1 && "$1" =~ ^[0-9]+$ && "$1" -ge 1 && "$1" -le 64 ]] || usage
NUE=$1; shift
CHAN="$D/ue_channel.conf"; TELEM=0; EDGERIC=0; TUNE=1; IPC=1; BROKER=cpp; BROKER_ARGS=(); TRACES=""; LOW=(); PHASE=all
while [ $# -gt 0 ]; do
  case "$1" in
    --channel)   CHAN="$2"; shift 2 ;;
    --clean)     CHAN=""; shift ;;
    --traces)    TRACES="$2"; shift 2 ;;
    --allow-low-snr) LOW=(--allow-low-snr); shift ;;
    --telemetry) TELEM=1; shift ;;
    --edgeric)   EDGERIC=1; shift ;;
    --ran|--ues) [ "$PHASE" = all ] || { echo "ERROR: --ran or --ues, not both"; exit 1; }; PHASE=${1#--}; shift ;;
    --no-tune)   TUNE=0; shift ;;
    --tcp)       IPC=0; shift ;;
    --broker)    BROKER="$2"; shift 2 ;;
    --) shift; BROKER_ARGS=("$@"); break ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option $1"; usage ;;
  esac
done
if [ "$PHASE" = ues ] && { [ "$TELEM" = 1 ] || [ "$EDGERIC" = 1 ] || [ "$IPC" = 0 ] || [ "$BROKER" != cpp ] || [ ${#BROKER_ARGS[@]} -gt 0 ]; }; then
  echo "ERROR: --telemetry, --edgeric, --tcp, --broker and broker args belong to the --ran half"; exit 1
fi

kx() { timeout "${3:-30}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2"; }
jx() { timeout "${2:-60}" kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c "$1"; }
cpx() { kubectl cp "$1" "$NS/$POD:$2" -c "$3" >/dev/null 2>&1 || { echo "ERROR: cp $1 -> $3"; exit 1; }; }

# ---- pre-flight, before stopping anything -----------------------------------------------------
for f in "$HR/duranta-oai-ue/gnb_zmq_tdd_n78_20mhz.yml" \
         "$D/multi_ue_broker.py" "$D/netns_setup_n.sh" "$D/oaiue1_netns.conf" "$D/uecap_ports1.xml"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; echo "  run: git submodule update --init --recursive"; exit 1; }
done
case "$BROKER" in
  cpp) [ -x "$D/broker_cpp/zmq_broker" ] || { echo "ERROR: build it first: bash duranta-oai-ue/broker_cpp/build.sh"; exit 1; } ;;
  py)  ;;
  *)   echo "ERROR: --broker cpp|py"; exit 1 ;;
esac
timeout 20 kubectl get pod -n $NS $POD >/dev/null || {
  echo "ERROR: cannot reach pod $NS/$POD with KUBECONFIG=$KUBECONFIG"
  echo "  export KUBECONFIG=\"\$(k3d kubeconfig write janus-cluster)\"   (and do not run with sudo)"; exit 1; }
kx $UC 'grep -qa "bad chan config" /usr/local/lib/liboai_zmqdevif.so' 15 || {
  echo "ERROR: the UE in container '$UC' has no ZMQ channel support (liboai_zmqdevif.so predates"
  echo "  duranta-ue dev/channel-implementation). Rebuild duranta-oai-ue/Dockerfile and re-add the UE"
  echo "  container, or set UE_C to a container that has it."; exit 1; }
if [ -n "$TRACES" ]; then
  [ -n "$CHAN" ] || { echo "ERROR: --traces needs the channel model: drop --clean"; exit 1; }
  [ "$BROKER" = cpp ] || { echo "ERROR: --traces needs --broker cpp, which starts every UE on the same gNB sample"; exit 1; }
  ug=0
  for a in "${BROKER_ARGS[@]}"; do
    case "$a" in
      --ul-gain-db) ug=1 ;;
      --ul-noise-dbfs) echo "ERROR: with --traces, set ul_noise_dbfs in $TRACES, not as a broker argument"; exit 1 ;;
      --*) ug=0 ;;
      *) [ $ug = 1 ] && awk -v g="$a" 'BEGIN {exit !(g + 0 != 0)}' && {
           echo "ERROR: --ul-gain-db $a would shift every UL SNR of the traces; leave it out with --traces"; exit 1; } ;;
    esac
  done
  kx $UC 'grep -qa "trace_ctl" /usr/local/lib/liboai_zmqdevif.so' 15 || {
    echo "ERROR: the UE in container '$UC' cannot replay traces (liboai_zmqdevif.so predates trace_ctl)."
    echo "  Rebuild duranta-oai-ue/Dockerfile, import it and recreate the pod (run_system.md steps 1 and 3)."; exit 1; }
  PLAN=$(bash "$HR/scripts/trace_ctl.sh" check "$TRACES" --ues "$NUE" "${LOW[@]}") || exit 1
  echo "  traces from attach: $PLAN"
fi

# Channel table -> one --zmq.[0].chan spec per UE (same parsing and checks as ocudu-edgeric).
declare -A MOB=(); UL_NOISE=""
if [ -n "$CHAN" ]; then
  [ -f "$CHAN" ] || { echo "ERROR: channel file not found: $CHAN"; exit 1; }
  UL_NOISE=$(awk '$1=="ul_noise_dbfs"{print $2}' "$CHAN" | tail -1)
  while read -r ue dist speed minm maxm dlsnr ulsnr jitter dlmin ulmin; do
    [[ "$ue" =~ ^[0-9]+$ ]] || continue
    (( ue > NUE )) && continue
    for v in "$dist" "$speed" "$minm" "$maxm" "$dlsnr" "$ulsnr"; do
      [[ "$v" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || { echo "ERROR: $CHAN: UE $ue: non-numeric value '$v'"; exit 1; }
    done
    awk -v a="$maxm" -v b="$minm" 'BEGIN{exit !(a<=b)}' && { echo "ERROR: $CHAN: UE $ue: max_m ($maxm) must exceed min_m ($minm)"; exit 1; }
    awk -v d="$dist" 'BEGIN{exit !(d<=0)}' && { echo "ERROR: $CHAN: UE $ue: dist_m must be > 0"; exit 1; }
    spec="distance=$dist,speed=$speed,min_distance=$minm,max_distance=$maxm,dl_snr_ref=$dlsnr,ul_snr_ref=$ulsnr,seed=$ue"
    [ -n "$jitter" ] && spec+=",jitter_db=$jitter"
    [ -n "$dlmin" ] && spec+=",dl_snr_min=$dlmin"
    [ -n "$ulmin" ] && spec+=",ul_snr_min=$ulmin"
    MOB[$ue]="$spec"
  done < <(grep -vE '^[[:space:]]*(#|$)' "$CHAN")
fi
# With traces every UE gets a spec and the control file it is switched through, and the broker and
# all UEs one noise floor: the traces table's, else the channel table's, else -65 dBFS.
if [ -n "$TRACES" ]; then
  TN=$(awk '$1=="ul_noise_dbfs"{print $2}' "$TRACES" | tail -1)
  UL_NOISE=${TN:-${UL_NOISE:--65}}
  [[ "$UL_NOISE" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || { echo "ERROR: ul_noise_dbfs '$UL_NOISE' is not a number"; exit 1; }
fi
[ -n "$UL_NOISE" ] && BROKER_ARGS=(--ul-noise-dbfs "$UL_NOISE" "${BROKER_ARGS[@]}")
declare -A SPEC=()
for n in $(seq 1 "$NUE"); do
  sp="${MOB[$n]:-}"
  [ -n "$TRACES" ] && sp="${sp:-dl_snr_ref=30,ul_snr_ref=30,seed=$n},trace_ctl=/tmp/traces/ue$n.ctl"
  [ -n "$sp" ] && [ -n "$UL_NOISE" ] && sp+=",ul_noise_dbfs=$UL_NOISE"
  if [ -n "$sp" ]; then
    (( ${#sp} <= 256 )) || { echo "ERROR: ue$n channel spec is ${#sp} characters, the UE takes 256: $sp"; exit 1; }
    [[ "$sp" =~ ^[A-Za-z0-9_.,=/+-]+$ ]] || { echo "ERROR: ue$n channel spec has stray characters (a comment on a data line?): $sp"; exit 1; }
  fi
  SPEC[$n]="$sp"
done

if [ "$PHASE" = ues ]; then
  echo "== [1-6/7] the RAN of the last --ran =="
  BPID=$(kx $BC 'pgrep -f "[m]ulti_ue_broker.py|[z]mq_broker -n" | head -1' 10 | tr -dc '0-9')
  RAN=$(kx $BC 'cat /tmp/ran.env 2>/dev/null || true' 10)
  [ -n "$BPID" ] && [ -n "$RAN" ] || { echo "ERROR: no broker from a --ran run: start the RAN first (--ran)"; exit 1; }
  eval "$(grep -E '^RAN_[A-Z_]+=[A-Za-z0-9.+-]*$' <<<"$RAN")"
  kx $GC 'pgrep -x gnb >/dev/null && grep -qiE "NG setup.*success|Connection to AMF.*completed" /tmp/gnb.stdout' 15 2>/dev/null || {
    echo "ERROR: no gNB connected to the AMF: restart the RAN (--ran)"; exit 1; }
  kx $UC '[ ! -e /tmp/ues ]' 10 2>/dev/null || {
    echo "ERROR: UEs already joined this RAN, and ZMQ cannot reconnect: restart the RAN (--ran), then --ues"; exit 1; }
  [ "${RAN_NUE:-}" = "$NUE" ] || {
    echo "ERROR: the broker serves ${RAN_NUE:-?} UE(s): run --ues with N=${RAN_NUE:-?}, or the RAN with N=$NUE"; exit 1; }
  [ "${RAN_UL_NOISE:-}" = "$UL_NOISE" ] || awk -v a="${RAN_UL_NOISE:-}" -v b="$UL_NOISE" 'BEGIN {exit !(a != "" && b != "" && a + 0 == b + 0)}' || {
    echo "ERROR: the broker's UL noise floor is ${RAN_UL_NOISE:-off} dBFS, this channel's ${UL_NOISE:-off}: give --ran the same --channel / --traces"; exit 1; }
  [ -n "$TRACES" ] && [ "${RAN_BROKER:-}" != cpp ] && { echo "ERROR: --traces needs a RAN with the C++ broker"; exit 1; }
  IPC=${RAN_IPC:-1}; TELEM=${RAN_TELEM:-0}
  echo "  broker pid $BPID for $NUE UE(s), UL noise ${UL_NOISE:-off}; gNB up; telemetry $([ "$TELEM" = 1 ] && echo on || echo off)"
else
  echo "== [1/7] stop everything =="
  bash "$HR/scripts/stop_demo.sh" 2>&1 | grep -E "stopped|unloaded" | sed 's/^/  /'
  kx $UC 'for p in $(pgrep -x nr-uesoftmodem); do kill -9 $p 2>/dev/null; done; for p in $(pgrep -x iperf3); do kill -9 $p 2>/dev/null; done; sleep 2; rm -rf /tmp/ues /tmp/traces; mkdir -p /tmp/traces; echo "  ues stopped"' 40
  kx $BC 'for p in $(pgrep -x python3) $(pgrep -x zmq_broker); do kill -9 $p 2>/dev/null; done; sleep 3; rm -f /tmp/ran.env; echo "  broker stopped"' 40

  echo "== [2/7] core: IMSIs $((IMSI_BASE + 1))..$((IMSI_BASE + NUE)) =="
  MONGO=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name | grep '^open5gs-mongodb-')
  timeout 60 kubectl exec -n open5gs "$MONGO" -- mongosh --quiet open5gs --eval "
    const base = db.subscribers.findOne({imsi: '999700000000001'});
    if (!base) { print('  ERROR: template subscriber 999700000000001 missing'); quit(1); }
    let added = 0;
    for (let i = 1; i <= $NUE; i++) {
      const imsi = '99970000000000'.slice(0, 15 - String(i).length) + i;
      if (db.subscribers.findOne({imsi})) continue;
      const s = Object.assign({}, base); delete s._id; s.imsi = imsi;
      db.subscribers.insertOne(s); added++;
    }
    print('  provisioned (' + added + ' added)');" || { echo "ERROR: could not provision subscribers"; exit 1; }

  if [ "$TELEM" = 1 ] || [ "$EDGERIC" = 1 ]; then
    echo "== [3/7] fresh jrtc-0 (clean app registry + jbpf IPC peer) =="
    kubectl delete pod -n $NS jrtc-0 --wait=false >/dev/null 2>&1
    for i in $(seq 1 40); do sleep 3; kubectl get pod -n $NS jrtc-0 2>/dev/null | grep -q '2/2 *Running' && break; done
    sleep 12; echo "  jrtc-0 ready"
  else
    echo "== [3/7] telemetry: skipped (pass --telemetry to load the upt and dashboard xApps) =="
  fi

  echo "== [4/7] stage broker, UE and gNB configs; netns for $NUE UE(s) =="
  cpx $D/multi_ue_broker.py /tmp/multi_ue_broker.py $BC
  [ "$BROKER" = cpp ] && cpx $D/broker_cpp/zmq_broker /tmp/zmq_broker $BC
  cpx $D/oaiue1_netns.conf  /tmp/oaiue_tmpl.conf  $UC
  cpx $D/uecap_ports1.xml   /tmp/uecap_ports1.xml $UC
  cpx $D/netns_setup_n.sh   /tmp/netns_setup_n.sh $UC
  # gNB: ocudu-edgeric's n78 20 MHz ZMQ config, k3d-adapted (duranta-oai-ue/gnb_zmq_tdd_n78_20mhz.yml).
  cp $HR/duranta-oai-ue/gnb_zmq_tdd_n78_20mhz.yml /tmp/zcfg.yml
  cpx /tmp/zcfg.yml /tmp/zcfg.yml $GC
  kx $UC "bash /tmp/netns_setup_n.sh $NUE | sed 's/^/  /'" 40 || { echo "ERROR: netns setup failed"; exit 1; }

  echo "== [5/7] broker ($NUE UE) =="
  # ipc: every ZMQ socket lives in the broker container's /tmp/zmq. The UE and gNB containers reach
  # them through /proc/<broker pid>/root (the pod shares one PID namespace; the broker container may
  # not look into the others), and path-based unix sockets work across the UEs' network namespaces.
  # Cheaper per round trip than tcp, which matters in a lock-step REQ/REP chain. The gNB side needs
  # the ocudu-jbpf build that accepts ipc:// in ru_sdr device_args (lib/radio/zmq validator).
  BARGS="-n $NUE ${BROKER_ARGS[*]}"
  GNB_IPC=0
  if [ "$IPC" = 1 ]; then
    BARGS="$BARGS --ue-tx ipc:///tmp/zmq/ue{i}_tx --ue-rx ipc:///tmp/zmq/ue{i}_rx"
    if [ "${GNB_LINK:-ipc}" = ipc ] && kx $GC 'grep -aq "valid TCP or IPC socket" /usr/local/bin/gnb' 15; then
      GNB_IPC=1; BARGS="$BARGS --gnb-tx ipc:///tmp/zmq/gnb_tx --gnb-rx ipc:///tmp/zmq/gnb_rx"
    else
      echo "  (this gNB build only takes tcp:// -- gNB <-> broker stays on tcp loopback)"
    fi
  fi
  BCMD="python3 -u multi_ue_broker.py"; [ "$BROKER" = cpp ] && BCMD="./zmq_broker"
  kx $BC 'rm -rf /tmp/zmq; mkdir -p /tmp/zmq' 10
  kx $BC "cd /tmp && nohup setsid $BCMD $BARGS >/tmp/broker.log 2>&1 </dev/null & disown; sleep 3; sed 's/^/  /' /tmp/broker.log" 40
  BPID=$(kx $BC 'pgrep -f "[m]ulti_ue_broker.py|[z]mq_broker -n" | head -1' 10 | tr -dc '0-9')
  [ -n "$BPID" ] || { echo "ERROR: broker exited"; kx $BC 'tail -20 /tmp/broker.log'; exit 1; }
  # what --ues needs to join this broker
  kx $BC "printf 'RAN_NUE=%s\nRAN_UL_NOISE=%s\nRAN_IPC=%s\nRAN_TELEM=%s\nRAN_BROKER=%s\n' $NUE '$UL_NOISE' $IPC $TELEM $BROKER >/tmp/ran.env" 10

  echo "== [6/7] gNB (broker mode, dynamic pod-IP NG-U bind) =="
  [ "$GNB_IPC" = 1 ] && kx $GC "sed -i 's#tx_port=tcp://127.0.0.1:2101,rx_port=tcp://127.0.0.1:2100#tx_port=ipc:///proc/$BPID/root/tmp/zmq/gnb_tx,rx_port=ipc:///proc/$BPID/root/tmp/zmq/gnb_rx#' /tmp/zcfg.yml; grep -o 'tx_port=[^,]*' /tmp/zcfg.yml | sed 's/^/  gnb /'" 15
  kx $GC 'MYIP=$(ip -o -4 addr show|grep -oE "10\.42\.[0-9]+\.[0-9]+"|grep -v "\.255$"|head -1); sed -i "s/bind_addr: 10\.42\.[0-9.]\+/bind_addr: $MYIP/g" /tmp/zcfg.yml; : >/tmp/gnb.log; cd /tmp && nohup setsid stdbuf -oL /usr/local/bin/gnb -c zcfg.yml '"${GNB_ARGS:-}"' >/tmp/gnb.stdout 2>&1 </dev/null & disown; echo "  gnb started (bind=$MYIP)"' 20
  for i in $(seq 1 24); do sleep 3; kx $GC 'grep -ciE "NG setup.*success|Connection to AMF.*completed" /tmp/gnb.stdout' 15 2>/dev/null | grep -qvE '^0$|^$' && break; done
  kx $GC 'echo "  gnb procs=$(pgrep -c -x gnb) ngsetup=$(grep -ciE "NG setup.*success|Connection to AMF.*completed" /tmp/gnb.stdout)"' 15

  if [ "$TELEM" = 1 ]; then
    # Codelets live in the gNB process, so they load now: after this gNB starts, before any UE attaches.
    # The dashboard xApp's ue_contexts and ngap codelets only see a UE's identities as it attaches.
    kx $GC 'mkdir -p /codelets' 10
    # bufctl is only staged: jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py loads it when it starts (Demo 3)
    for d in upt ue_contexts mac rlc pdcp rrc ngap bufctl; do cpx "$HR/jrtc-apps/codelets/$d" /codelets/ $GC; done
    kubectl cp "$HR/telemetry/fwd30450.py" $NS/jrtc-0:/tmp/fwd30450.py -c jrtc >/dev/null 2>&1
    jx 'cd /tmp && nohup setsid python3 -u fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 3; echo "  fwd: $(head -1 /tmp/fwd30450.log 2>/dev/null)"' 20
    jx 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_demo.yaml --log-level error 2>&1 | grep -iE "error|fatal" | tail -4; echo "  upt codeletset loaded"' 120
    jx 'export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl load -c /apps/dashboard/deployment_ocudu.yaml --log-level error 2>&1 | grep -iE "error|fatal" | tail -4; echo "  dashboard xApp loaded"' 120
    # jrtc-ctl reports "loaded" even when an xApp dies importing a module
    sleep 3; kubectl logs -n $NS jrtc-0 -c jrtc --since=3m 2>/dev/null | grep -aE "Failed to import|Python app terminated" | tail -2 | sed 's/^/  !! /'
  fi
  if [ "$EDGERIC" = 1 ]; then
    bash "$HR/scripts/edgeric_rt.sh" load | sed 's/^/  /'
  fi
fi

if [ "$PHASE" = ran ]; then
  echo
  echo "RAN UP. Broker for $NUE UE(s); gNB connected to the AMF."
  echo "  UEs     : bash scripts/setup_zmq_chan_demo.sh $NUE --ues [--channel FILE | --traces FILE]   (another terminal)"
  echo "  console : bash scripts/gnb_console.sh"
  [ -t 0 ] && [ -t 1 ] || exit 0
  echo
  exec bash "$HR/scripts/gnb_console.sh"
fi

echo "== [7/7] $NUE UE(s) =="
ue_ep() {  # ue_ep <i> tx|rx
  if [ "$IPC" = 1 ]; then echo "ipc:///proc/$BPID/root/tmp/zmq/ue${1}_$2"
  elif [ "$2" = tx ]; then echo "tcp://10.201.$1.2:2001"; else echo "tcp://10.201.$1.1:2000"; fi
}
for n in $(seq 1 "$NUE"); do
  IMSI=$(printf "%015d" $((IMSI_BASE + n)))
  CH=""; [ -n "${SPEC[$n]}" ] && CH="'--zmq.[0].chan' ${SPEC[$n]}"
  echo "  ue$n: IMSI $IMSI  chan: ${SPEC[$n]:-clean}"
  # Own working dir per UE: the softmodem writes nrL1_UE_stats-0.log to its cwd.
  kx $UC "mkdir -p /tmp/ues/ue$n && cd /tmp/ues/ue$n && : >/tmp/ues/ue$n.log
    nohup setsid chrt -f 50 ip netns exec ue$n /opt/oai-nr-ue/bin/nr-uesoftmodem -O /tmp/oaiue_tmpl.conf \
      --uicc0.imsi $IMSI '--zmq.[0].tx_channels' $(ue_ep $n tx) '--zmq.[0].rx_channels' $(ue_ep $n rx) \
      --band 78 --numerology 1 -r 51 -C 3489420000 --ue-scan-carrier -E $CH ${UE_ARGS:-} \
      >/tmp/ues/ue$n.log 2>&1 </dev/null & disown" 20
done

# Attach takes well over a minute with several UEs; poll rather than sleep.
count_attached() {
  kx $UC "c=0; for n in \$(seq 1 $NUE); do ip netns exec ue\$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -q 10.45 && c=\$((c+1)); done; echo \$c" 20 | tr -d '[:space:]'
}
for i in $(seq 1 $((30 + 6 * NUE))); do
  sleep 5
  A=$(count_attached); [ "$A" = "$NUE" ] && break
  kx $UC '[ "$(pgrep -c -x nr-uesoftmodem)" -gt 0 ]' 10 || { echo "ERROR: all UEs exited"; break; }
done

[ "$TUNE" = 1 ] && bash "$HR/scripts/zmq_tune.sh" apply

echo
echo "################ verify ################"
kx $UC "for n in \$(seq 1 $NUE); do IP=\$(ip netns exec ue\$n ip -o -4 addr show oaitun_ue1 2>/dev/null | grep -o '10\.45\.0\.[0-9]*' | head -1)
  [ -n \"\$IP\" ] && ip netns exec ue\$n ip route replace default dev oaitun_ue1
  echo \"  ue\$n ip=\${IP:-NONE}  \$(grep -a 'ZMQ chan' /tmp/ues/ue\$n.log | tail -1 | sed 's/.*\[ZMQ chan\] *//')\"; done" 30
A=$(count_attached)
if [ "$A" != "$NUE" ]; then
  echo
  echo "  !! $A/$NUE UEs attached. Last lines of each UE log:"
  kx $UC "for n in \$(seq 1 $NUE); do echo \"  --- ue\$n ---\"; tail -5 /tmp/ues/ue\$n.log; done" 25
  if [ "$PHASE" = ues ]; then
    echo "  Restart the RAN (--ran), then run --ues again: a partial bring-up needs a fresh broker + gNB."
  else
    echo "  Re-run this script: a partial bring-up needs a fresh broker + gNB, which it does every time."
  fi
  exit 1
fi
# the core's IMSI / IP / NGAP IDs per UE, which the dashboard xApp joins to its gNB-side UE contexts
[ "$TELEM" = 1 ] && bash "$HR/scripts/core_ue_info.sh" 2>&1 | sed 's/^/  /'
if [ -n "$TRACES" ]; then
  echo
  echo "== traces: $TRACES =="
  bash "$HR/scripts/trace_ctl.sh" apply "$TRACES" "${LOW[@]}" || echo "  !! traces not applied; the UEs stay on their base channel (retry: bash scripts/trace_ctl.sh apply $TRACES)"
fi

echo
echo "SETUP DONE. $NUE UE(s) attached."
echo "  traffic : bash scripts/traffic_nue.sh start     (DL iperf3, UPF -> every UE)"
echo "  rates   : bash scripts/traffic_nue.sh status    (per-UE + total, measured at the UE)"
echo "  channel : kubectl exec -n ran $POD -c $UC -- grep 'ZMQ chan' /tmp/ues/ue1.log | tail"
[ -n "$TRACES" ] && echo "  traces  : bash scripts/trace_ctl.sh status | set <ues> <trace> | stop <ues> | list"
echo "  stop    : bash scripts/traffic_nue.sh stop && bash scripts/stop_demo.sh"
