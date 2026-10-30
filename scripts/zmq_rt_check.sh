#!/usr/bin/env bash
# Is the ZMQ testbed running in real time? For the N-UE bring-up (scripts/setup_zmq_chan_demo.sh).
#
#   bash scripts/zmq_rt_check.sh [seconds]      (default 10)
#
# 1. Sample rate per UE (tcp mode only) from the bytes on each UE's veth, and on the gNB link: the broker
#    paces both directions at 23.04 MS/s of complex float32, i.e. 184.32 MB/s. Real time = 100%.
#    Anything below means some element (gNB, broker, UE, channel model) cannot keep up.
# 2. Lateness the radios report: gNB "late"/underflow/overflow/real-time warnings, and OAI UE
#    lateness/resync lines -- counted since the run started.
# 3. CPU of each process, and the latest channel state of each UE.
set -u
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; UC="${UE_C:-durue1}"; GC=ocudujbpf
T="${1:-10}"
kx() { timeout "${3:-60}" kubectl exec -n $NS $POD -c "$1" -- bash -c "$2" 2>/dev/null; }

echo "== sample rate over ${T}s (real time = 23.04 MS/s = 100%) =="
kx $UC "
  UES=\$(ls /sys/class/net | grep -oE '^v[0-9]+h' | tr -dc '0-9\n' | sort -n)
  snap() { for n in \$UES; do d=/sys/class/net/v\${n}h/statistics; echo \$n \$(cat \$d/tx_bytes \$d/tx_packets \$d/rx_bytes \$d/rx_packets); done; }
  A=\$(snap); sleep $T; B=\$(snap)
  # payload = bytes - 66 per packet (eth+ip+tcp headers); 8 bytes per complex sample
  paste <(echo \"\$A\") <(echo \"\$B\") | awk -v t=$T '{
    dl=((\$7-\$2)-66*(\$8-\$3))/8/t/1e6; ul=((\$9-\$4)-66*(\$10-\$5))/8/t/1e6
    if (dl < 0.5) { ipc=1; next }
    printf \"  ue%-3s DL %6.2f MS/s (%5.1f%%)   UL %6.2f MS/s (%5.1f%%)\n\", \$1, dl, 100*dl/23.04, ul, 100*ul/23.04 }
    END { if (ipc) print \"  (broker<->UE over ipc: not visible on the veths; the gNB-side rate below covers the whole chain)\" }'
" $((T + 30))

echo "== gNB side over ${T}s (what the broker pulls from / pushes to the gNB) =="
# DL = bytes the broker received from the gNB (largest bytes_received), UL = bytes the gNB acked (largest bytes_acked)
kx $UC "s(){ ss -tinH '( dport = :2101 or sport = :2100 )' | awk '{for(i=1;i<=NF;i++){split(\$i,a,\":\"); if(a[1]==\"bytes_received\"&&a[2]>r)r=a[2]; if(a[1]==\"bytes_acked\"&&a[2]>k)k=a[2]}} END{print r+0, k+0}'; }
  A=\$(s); sleep $T; B=\$(s)
  echo \$A \$B | awk -v t=$T '{dl=(\$3-\$1)/8/t/1e6; ul=(\$4-\$2)/8/t/1e6; if (dl < 0.5) { print \"  gNB link on ipc: see the broker line below\"; exit }
    printf \"  gNB  DL %6.2f MS/s (%5.1f%%)   UL %6.2f MS/s (%5.1f%%)\\n\", dl, 100*dl/23.04, ul, 100*ul/23.04}' 
" $((T + 30))

# With the gNB on ipc there is no tcp link to count; the C++ broker logs the rate it moves every 5 s.
B=$(timeout 20 kubectl exec -n $NS $POD -c grbroker -- bash -c "grep -a '\[broker\] rate' /tmp/broker.log 2>/dev/null | tail -1" 2>/dev/null)
[ -n "$B" ] && echo "  broker (last 5 s): ${B#\[broker\] rate }"

echo "== lateness reported since start =="
kx $GC 'for f in /tmp/gnb.log /tmp/gnb.stdout; do
  c=$(grep -a -ciE "\blate\b|underflow|overflow|real-time|realtime failure" $f 2>/dev/null); echo "  gNB $(basename $f): $c"
  [ "${c:-0}" -gt 0 ] && grep -a -iE "\blate\b|underflow|overflow|real-time|realtime failure" $f | sed -E "s/^[0-9T:.-]+ //" | cut -c1-110 | sort | uniq -c | sort -rn | head -3 | sed "s/^/      /"; done'
kx $UC 'for f in $(ls /tmp/ues/ue*.log | sort -V); do
  c=$(grep -a -ciE "\blate\b|too far|resync|out of sync|lost sync|rx_rf.*(timeout|fail)|zmq.*timeout" $f)
  echo "  $(basename $f .log): $c  | $(grep -a "ZMQ chan" $f | tail -1 | sed "s/.*\[ZMQ chan\] *//")"; done'

echo "== CPU (%, of one core) =="
kx $UC 'top -b -n1 -w 200 | awk "\$8!=\"Z\" && \$12~/gnb|nr-ueso|python3/{printf \"  %-14s pid %-6s %6s%%\n\", \$12, \$1, \$9}"'
