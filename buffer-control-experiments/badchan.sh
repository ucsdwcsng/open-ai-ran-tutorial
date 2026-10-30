#!/usr/bin/env bash
# badchan.sh - "bad radio" emulation: cap the downlink MCS (experiment B).
#
#   bash buffer-control-experiments/badchan.sh build [NAME ...]   # default: mcs5 mcs10 mcs16
#   bash buffer-control-experiments/badchan.sh load NAME          # channel goes bad
#
# NAME:  mcsN                  sustained: DL MCS <= N from now on
#        fadeXofY[_mcsN]       fading: DL MCS <= N (default 10) for X ms out of every Y ms
#                              e.g. fade100of400 = a 100 ms dip every 400 ms
#        trace_NAME            replay buffer-control-experiments/traces/NAME.csv (per-second MCS), looping
#   bash buffer-control-experiments/badchan.sh unload             # channel recovers on the next grant
#   bash buffer-control-experiments/badchan.sh status
#   bash buffer-control-experiments/badchan.sh list
#
# Lower N = worse channel = lower downlink rate. The codelet only ever LOWERS the MCS that
# link adaptation picked. It sits on mac_sched_dl_mcs_ctrl, a different hook from bufcap
# (rlc_dl_ctrl), so both can be loaded at the same time.
#
# Unlike bufcap, unloading fully undoes it: the MCS override is per grant, and the gNB
# falls back to link adaptation as soon as no codelet sets it.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CDIR=$HR/jrtc-apps/codelets/badchan         # jrtc-0 sees this as /codelets/badchan
ADIR=$HR/jrtc-apps/jrtc_apps/badchan        # jrtc-0 sees this as /apps/badchan
STATE=$HR/buffer-control-experiments/.badchan_state
NS=ran; POD=srs-gnb-du1-0
SDK=ghcr.io/microsoft/jrtc-apps/srs-jbpf-sdk:latest

gnb() { timeout "${2:-30}" kubectl exec -n $NS $POD -c ocudujbpf -- bash -c "$1" 2>/dev/null; }
jctl() {  # jctl load|unload NAME
  timeout 150 kubectl exec -n $NS jrtc-0 -c jrtc -- bash -c \
    "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl $1 -c /apps/badchan/deployment_$2.yaml --log-level error 2>&1 | grep -iE 'error|fail' | tail -2"
}
parse() {  # NAME -> "MCS ON_MS PERIOD_MS TRACE"   (wall-clock ms)
  local n=$1 m=10 on=0 per=0
  if [[ $n =~ ^trace_([a-z0-9]+)$ ]]; then
    [ -f "$HR/buffer-control-experiments/traces/${BASH_REMATCH[1]}.csv" ] || { echo "no trace traces/${BASH_REMATCH[1]}.csv" >&2; return 1; }
    echo "0 0 0 1"; return 0
  elif [[ $n =~ ^mcs([0-9]{1,2})$ ]]; then
    m=${BASH_REMATCH[1]}
  elif [[ $n =~ ^fade([0-9]+)of([0-9]+)(_mcs([0-9]{1,2}))?$ ]]; then
    on=${BASH_REMATCH[1]}; per=${BASH_REMATCH[2]}
    [ -n "${BASH_REMATCH[4]}" ] && m=${BASH_REMATCH[4]}
    [ "$on" -gt 0 ] && [ "$on" -lt "$per" ] || { echo "bad fade '$n': need 0 < X < Y" >&2; return 1; }
  else
    echo "bad name '$n' (use mcsN, or fadeXofY[_mcsN] with X, Y in ms)" >&2; return 1
  fi
  [ "$m" -le 28 ] || { echo "MCS must be 0..28" >&2; return 1; }
  echo "$m $on $per 0"
}
mcs_of() { set -- $(parse "$1") || return 1; echo "$1"; }
describe() {  # human text for NAME
  local n=$1
  set -- $(parse "$1") || return 1
  [ "$4" -eq 1 ] && { echo "trace ${n#trace_}: replaying traces/${n#trace_}.csv ($(( $(wc -l < "$HR/buffer-control-experiments/traces/${n#trace_}.csv") - 1 )) s, looping)"; return 0; }
  [ "$3" -eq 0 ] && echo "bad channel: MCS <= $1" || echo "fading channel: MCS <= $1 for $2 ms every $3 ms"
}
counts() {
  gnb "echo \$(grep -aoE 'loaded successfully: [a-z_0-9]+' /tmp/gnb.log | grep -cx 'loaded successfully: $1') \$(grep -ac 'Codelet $1 removed successfully' /tmp/gnb.log)"
}
attached() { set -- $(counts "$1"); [ "${1:-0}" -gt "${2:-0}" ]; }
current() { cat "$STATE" 2>/dev/null || echo none; }
mark() {  # mark MCS_CAP(or 28 = no cap) TEXT
  local now_ns; now_ns=$(date +%s%N)
  curl -s -m 5 -XPOST http://localhost:30491/write --data-binary "badchan mcs_cap=$1 $now_ns" >/dev/null
  curl -s -m 5 -u admin:admin -H 'Content-Type: application/json' -XPOST http://localhost:30490/api/annotations \
    -d "{\"time\":$(( now_ns / 1000000 )),\"tags\":[\"badchan\"],\"text\":\"$2\"}" >/dev/null
  echo "{\"t\": $(( now_ns / 1000000 ))e-3, \"mcs_cap\": $1, \"text\": \"$2\"}" >> "$HR/buffer-control-experiments/.badchan_events.jsonl"
}

build_one() {
  local n=$1 p
  p=$(parse "$n") || return 1
  set -- $p; local m=$1 on=$2 per=$3 tr=$4
  mkdir -p "$CDIR" "$ADIR"
  if [ "$tr" -eq 1 ]; then
    python3 "$HR/buffer-control-experiments/gen_trace_h.py" "$HR/buffer-control-experiments/traces/${n#trace_}.csv" "$CDIR/trace_active.h" | sed 's/^/  /'
  else
    printf '#define TRACE_LEN (1)\nstatic inline uint32_t trace_mcs(uint32_t s) { return 28; }\n' > "$CDIR/trace_active.h"
  fi
  docker run --rm --user "$(id -u):$(id -g)" -v "$HR/jrtc-apps/codelets:/codelet" \
    --entrypoint /usr/bin/make "$SDK" -C /codelet/badchan one NAME="$n" MCS="$m" ON="$on" PERIOD="$per" TRACE="$tr" 2>&1 \
    | grep -E "^---|rror|verifier" | sed 's/^/  /'
  [ -f "$CDIR/badchan_$n.o" ] || { echo "  !! build failed for $n"; return 1; }
  cat > "$CDIR/badchan_$n.yaml" <<EOF
# generated by buffer-control-experiments/badchan.sh -- $(describe "$n")
codeletset_id: badchan
codelet_descriptor:
  - codelet_name: badchan
    codelet_path: \${JBPF_CODELETS}/badchan/badchan_$n.o
    hook_name: mac_sched_dl_mcs_ctrl
    priority: 1
EOF
  cat > "$ADIR/deployment_$n.yaml" <<EOF
# generated by buffer-control-experiments/badchan.sh
name: badchan
jbpf:
  device:
    - id: 1
      host: srs-gnb-du1-proxy.ran.svc.cluster.local
      port: 30450
  codelet_set:
    - device: 1
      config: \${JBPF_CODELETS}/badchan/badchan_$n.yaml
EOF
  echo "  built badchan_$n ($(describe "$n"))"
}

stage() {  # the gNB agent loads the .o from ITS OWN /codelets, not jrtc-0's; always re-copy (rebuilds)
  gnb "mkdir -p /codelets/badchan"
  kubectl cp "$CDIR/badchan_$1.o" "$NS/$POD:/codelets/badchan/badchan_$1.o" -c ocudujbpf >/dev/null 2>&1
}

do_unload() {
  attached badchan || { echo "  no badchan attached"; rm -f "$STATE"; return 0; }
  local any; any=$(ls "$ADIR"/deployment_*.yaml 2>/dev/null | head -1 | sed 's#.*/deployment_##; s#\.yaml$##')
  jctl unload "$any"
  attached badchan && { echo "  !! unload failed"; return 1; }
  echo "  unloaded badchan_$(current): channel back to normal"
  rm -f "$STATE"
  mark 28 "channel normal"
}

do_load() {
  local n=$1
  parse "$n" >/dev/null || return 1
  [ -f "$CDIR/badchan_$n.o" ] || build_one "$n" || return 1
  if attached dl_mcs; then
    echo "  !! the EdgeRIC dl_mcs codelet holds mac_sched_dl_mcs_ctrl; unload it first"; return 1
  fi
  attached badchan && { do_unload >/dev/null || return 1; }
  stage "$n" || { echo "  !! could not copy badchan_$n.o into the gNB container"; return 1; }
  set -- $(counts badchan); local before=${1:-0}
  jctl load "$n"
  set -- $(counts badchan)
  [ "${1:-0}" -gt "$before" ] || { echo "  !! load of badchan_$n failed (see /tmp/gnb.log)"; return 1; }
  echo "$n" > "$STATE"
  mark "$(mcs_of "$n")" "$(describe "$n")"
  echo "  loaded badchan_$n: $(describe "$n")"
}

CMD="${1:-status}"; shift 2>/dev/null || true
case "$CMD" in
  build)
    [ $# -eq 0 ] && set -- mcs5 mcs10 mcs16
    for n in "$@"; do build_one "$n"; done ;;
  load)
    [ $# -eq 1 ] || { echo "usage: badchan.sh load NAME   (mcsN | fadeXofY[_mcsN])"; exit 2; }
    do_load "$1" ;;
  unload|off)
    do_unload ;;
  status)
    set -- $(counts badchan)
    echo "  badchan: loads=${1:-0} removes=${2:-0} -> $(attached badchan && echo "ATTACHED ($(current))" || echo "not attached")" ;;
  list)
    ls "$CDIR"/badchan_*.o 2>/dev/null | sed 's#.*/badchan_##; s#\.o$##' | tr '\n' ' '; echo ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
