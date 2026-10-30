#!/usr/bin/env bash
# Give the dashboard xApp the core's view of each UE: IMSI, IP and NGAP UE IDs. The xApp joins it
# to the gNB's own UE contexts on the NGAP UE IDs, so each UE keeps one identity from the core
# down to its C-RNTI (Grafana's UE labels and UE identity table). The dashboard-realtime-scheduling
# xApp gets the same events and joins them on the UE number instead.
#
#   bash scripts/core_ue_info.sh             # after the UEs attach; setup_zmq_chan_demo.sh --telemetry runs it
#   bash scripts/core_ue_info.sh --tcp-map   # print only "ip=n,..." for telemetry/tcp_probe.py --map
#
# Read from the Open5GS logs of the current gNB connection (after the AMF's last "gNB-N2 accepted"):
#   AMF  RAN_UE_NGAP_ID / AMF_UE_NGAP_ID of each InitialUEMessage, then that UE's SUCI on the next
#        line (null protection scheme on this testbed, so the SUCI carries the IMSI digits)
#   SMF  "UE SUPI[imsi-...] ... IPv4[...]" for each PDU session (latest per IMSI)
# Each UE goes to the core-event input of the dashboard xApp (UDP 127.0.0.1:30502 in jrtc-0) and of the
# dashboard-realtime-scheduling xApp (:30503) as
#   {"context_type": "amf-ue", "event": "ran-ue-add", "context": {supi, suci, ran_ue, ue_ip}}
# n, the UE number on the dashboard, is the IMSI's subscriber number (...0001 -> 1).
set -u
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
SINCE="${CORE_LOG_SINCE:-24h}"
MODE=events; [ "${1:-}" = "--tcp-map" ] && MODE=map

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for nf in amf smf; do
  timeout 30 kubectl logs -n open5gs "deploy/open5gs-$nf" --since="$SINCE" 2>/dev/null \
    | sed 's/\x1b\[[0-9;]*m//g' >"$TMP/$nf.log"
done
[ -s "$TMP/amf.log" ] || { echo "ERROR: no AMF log (KUBECONFIG? do not run with sudo)"; exit 1; }

parse() {  # parse events|map|table
  python3 - "$1" "$TMP/amf.log" "$TMP/smf.log" <<'PY'
import json, re, sys, time
mode, amf_log, smf_log = sys.argv[1:4]
amf = open(amf_log).read().splitlines()
last = max((i for i, l in enumerate(amf) if "gNB-N2 accepted" in l and "master_sm" in l), default=-1)
ngap, suci, ip, pending = {}, {}, {}, None
for line in amf[last + 1:]:
    m = re.search(r"RAN_UE_NGAP_ID\[(\d+)\] AMF_UE_NGAP_ID\[(\d+)\]", line)
    if m:
        pending = (int(m.group(1)), int(m.group(2)))
        continue
    if pending is None:
        continue
    m = re.search(r"\[(suci-0-(\d{3})-(\d{2,3})-\w+-0-\d+-(\d+))\]", line)   # MCC, MNC, MSIN
    if m:
        imsi = m.group(2) + m.group(3) + m.group(4)
        ngap[imsi], suci[imsi], pending = pending, m.group(1), None
        continue
    m = re.search(r"\[imsi-(\d+)\]", line)
    if m:
        ngap[m.group(1)], pending = pending, None
for line in open(smf_log).read().splitlines():
    m = re.search(r"UE SUPI\[imsi-(\d+)\] DNN\[[^\]]*\] IPv4\[([\d.]+)\]", line)
    if m:
        ip[m.group(1)] = m.group(2)
n = lambda imsi: int(imsi[-9:])
for imsi in sorted(ngap, key=n):
    ran, amf_id = ngap[imsi]
    if mode == "events":
        print(json.dumps({"context_type": "amf-ue", "event": "ran-ue-add", "timestamp": int(time.time() * 1000),
                          "context": {"supi": f"imsi-{imsi}", "suci": suci.get(imsi),
                                      "ran_ue": {"ran_ue_ngap_id": ran, "amf_ue_ngap_id": amf_id},
                                      "ue_ip": ip.get(imsi)}}))
    elif mode == "table":
        print(f"UE {n(imsi)}  IMSI {imsi}  IP {ip.get(imsi, 'none'):<11}  RAN_UE_NGAP_ID {ran}  AMF_UE_NGAP_ID {amf_id}")
if mode == "map":
    print(",".join(f"{ip[i]}={n(i)}" for i in sorted(ngap, key=n) if i in ip))
PY
}

if [ "$MODE" = map ]; then parse map; exit 0; fi
EVENTS=$(parse events)
[ -n "$EVENTS" ] || { echo "no UE registration in the AMF log since the gNB connected"; exit 1; }
parse table
printf '%s\n' "$EVENTS" | timeout 30 kubectl exec -i -n ran jrtc-0 -c jrtc -- python3 -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
lines = [l.strip() for l in sys.stdin if l.strip()]
for l in lines:
    for port in (30502, 30503):   # the dashboard xApp, the dashboard-realtime-scheduling xApp
        s.sendto(l.encode(), ("127.0.0.1", port))
print("-> %d core events to the dashboard xApps (jrtc-0, udp 127.0.0.1:30502 dashboard, :30503 dashboard-realtime-scheduling)" % len(lines))' \
  || { echo "ERROR: cannot reach jrtc-0"; exit 1; }
