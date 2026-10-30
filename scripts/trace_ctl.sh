#!/usr/bin/env bash
# Channel traces for the running UEs: switch any UE to any trace mid-run, back to its base channel,
# or show what each UE plays. The UEs must run with trace control (setup_zmq_chan_demo.sh --traces).
#
#   bash scripts/trace_ctl.sh set <ues> <trace> [--lead S] [--once]   switch UEs to a trace
#   bash scripts/trace_ctl.sh stop <ues> [--lead S]                    back to the base channel
#   bash scripts/trace_ctl.sh apply FILE [--lead S]                    a whole assignment table
#   bash scripts/trace_ctl.sh status                                   each UE's channel now
#   bash scripts/trace_ctl.sh list                                     the trace library
#   bash scripts/trace_ctl.sh check FILE [--ues N]                     validate a table only
#
# <ues>    1 | 1,3 | 2-4 | all (every UE running with trace control)
# <trace>  a library name (duranta-oai-ue/traces/lib/<name>.csv, generated from scenarios.conf if
#          missing) or the path of a CSV file (format: duranta-oai-ue/traces/gen_traces.py)
# FILE     assignment table: "ue trace [once]" lines, several UEs may share a trace; other lines
#          (ul_noise_dbfs, comments) are for the setup script. Example: duranta-oai-ue/ue_traces_4ue.conf
#
# Every UE of one call switches at the same air time T = now + lead (default 3 s), on the same gNB
# sample in DL and UL; until T it keeps its current channel, so there is no gap. Traces loop unless
# --once (the last row then holds). --allow-low-snr accepts traces below 0 dB SNR, --no-wait skips
# waiting for T. Files in the UE container: /tmp/traces/<trace>.csv (copied under its file name, so
# two different files of the same name replace each other) and /tmp/traces/ue<n>.ctl.
set -u
HR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=srs-gnb-du1-0; UC="${UE_C:-durue1}"
TR=$HR/duranta-oai-ue/traces; GEN=$TR/gen_traces.py
usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit "${1:-1}"; }
die() { echo "ERROR: $*" >&2; exit 1; }

CMD="${1:-}"; [ -n "$CMD" ] || usage; shift
LEAD=3; LOOP=1; LOW=(); WAIT=1; MAXUE=""; POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --lead)          LEAD="${2:-}"; shift 2 ;;
    --once)          LOOP=0; shift ;;
    --allow-low-snr) LOW=(--allow-low-snr); shift ;;
    --no-wait)       WAIT=0; shift ;;
    --ues)           MAXUE="${2:-}"; shift 2 ;;
    -h|--help)       usage 0 ;;
    -*)              die "unknown option $1" ;;
    *)               POS+=("$1"); shift ;;
  esac
done
[[ "$LEAD" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "--lead takes seconds >= 0"
[ -z "$MAXUE" ] || [[ "$MAXUE" =~ ^[0-9]+$ ]] || die "--ues takes a number"

kx() { timeout "${2:-30}" kubectl exec -n $NS $POD -c $UC -- bash -c "$1"; }
names() { local s="ue$*"; echo "${s// /, ue}"; }  # 1 2 3 -> ue1, ue2, ue3
kxs() { timeout 30 kubectl exec -i -n $NS $POD -c $UC -- bash -s -- "$@"; }  # script on stdin, with args

# A trace argument -> SRC (host file) and NAME (/tmp/traces/<NAME>.csv in the UE container)
resolve() {
  if [[ "$1" == */* || "$1" == *.csv ]]; then
    SRC="$1"
  else
    SRC="$TR/lib/$1.csv"
    if [ ! -f "$SRC" ] && python3 "$GEN" --list | awk -v n="$1" '$1 == n {f = 1} END {exit !f}'; then
      python3 "$GEN" --only "$1" | sed 's/^/  generated /'
    fi
  fi
  [ -f "$SRC" ] || die "no trace '$1': not a file, and not in $TR/lib or scenarios.conf (trace_ctl.sh list)"
  NAME=$(basename "$SRC" .csv)
  [[ "$NAME" =~ ^[A-Za-z0-9_.+-]+$ ]] || die "trace file name '$NAME': letters, digits and _.+- only"
  python3 "$GEN" --check "$SRC" "${LOW[@]}" || die "$SRC is not a usable trace (above)"
}

# <ues> -> the UE numbers
ues_of() {
  local out=() part a
  if [ "$1" = all ]; then
    out=($(kx 'grep -la "traces switched through" /tmp/ues/ue*.log 2>/dev/null | grep -o "ue[0-9]*\.log" | tr -dc "0-9\n" | sort -n' 15))
    [ ${#out[@]} -gt 0 ] || die "no UE runs with trace control (bring them up with setup_zmq_chan_demo.sh --traces)"
  else
    IFS=, read -ra parts <<< "$1"
    for part in "${parts[@]}"; do
      if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
        for ((a = BASH_REMATCH[1]; a <= BASH_REMATCH[2]; a++)); do out+=("$a"); done
      elif [[ "$part" =~ ^[0-9]+$ ]]; then
        out+=("$part")
      else
        die "bad UE list '$1' (1 | 1,3 | 2-4 | all)"
      fi
    done
  fi
  echo "${out[@]}"
}

# Assignment table -> PAIRS ("ue:name:loop") and SRCS ("src name" pairs), UEs above MAXUE skipped
read_table() {
  [ -f "$1" ] || die "no table $1"
  PAIRS=(); SRCS=()
  local -A seen=()
  local ue tr opt l
  while read -r ue tr opt _; do
    [[ "$ue" =~ ^[0-9]+$ ]] || continue
    [ -n "$tr" ] || die "$1: ue $ue has no trace"
    [ -z "${seen[$ue]:-}" ] || die "$1: ue $ue appears twice"
    seen[$ue]=1
    case "${opt:-loop}" in loop) l=1 ;; once) l=0 ;; *) die "$1: ue $ue: third column is once or loop, got '$opt'" ;; esac
    [ -n "$MAXUE" ] && (( ue > MAXUE )) && continue
    resolve "$tr"
    PAIRS+=("$ue:$NAME:$l"); SRCS+=("$SRC" "$NAME")
  done < <(sed 's/#.*//' "$1")
  [ ${#PAIRS[@]} -gt 0 ] || die "$1 assigns no trace to UEs 1..${MAXUE:-}"
}

# Copies "src name" pairs to /tmp/traces/<name>.csv in the UE container, unless an identical copy is there
push() {
  local have src name sum
  have=$(kx 'mkdir -p /tmp/traces && cd /tmp/traces && for f in *.csv; do [ -f "$f" ] && md5sum -- "$f"; done; true' 15)
  while [ $# -gt 0 ]; do
    src=$1 name=$2; shift 2
    sum=$(md5sum < "$src" | cut -d' ' -f1)
    grep -qx "$sum  $name.csv" <<< "$have" && continue
    kubectl cp "$src" "$NS/$POD:/tmp/traces/$name.csv" -c "$UC" >/dev/null 2>&1 || die "copy $src -> $UC:/tmp/traces/$name.csv"
    have+=$'\n'"$sum  $name.csv"
    echo "  copied $name.csv"
  done
}

# Writes the control files of "ue:name:loop" (name none = base channel), all from T = now + LEAD
switch() {
  local out T now
  out=$(kxs "$LEAD" "$@" <<'EOF'
lead=$1; shift
now=0
for a in "$@"; do
  IFS=: read -r n name loop <<< "$a"
  f=/tmp/ues/ue$n.log
  [ -f $f ] || { echo "ERR ue$n is not running (no $f)"; exit 1; }
  grep -qa "traces switched through /tmp/traces/ue$n.ctl" $f || {
    echo "ERR ue$n runs without trace control: bring it up with setup_zmq_chan_demo.sh --traces"; exit 1; }
  [ "$name" = none ] || [ -f /tmp/traces/$name.csv ] || { echo "ERR /tmp/traces/$name.csv missing"; exit 1; }
  t=$(grep -a '\[ZMQ chan\] t [0-9]' $f | tail -1 | sed -E 's/.*\[ZMQ chan\] t ([0-9.]+) s.*/\1/')
  [ -n "$t" ] || { echo "ERR ue$n has logged no [ZMQ chan] line yet"; exit 1; }
  now=$(awk -v a=$now -v b=$t 'BEGIN {print (b > a ? b : a)}')
done
T=$(awk -v t=$now -v l=$lead 'BEGIN {printf "%.1f", t + l}')
for a in "$@"; do
  IFS=: read -r n name loop <<< "$a"
  p=none; [ "$name" = none ] || p=/tmp/traces/$name.csv
  wc -l < /tmp/ues/ue$n.log > /tmp/traces/.ue$n.mark   # where to look for the UE's answer
  printf 'trace=%s\nstart=%s\nloop=%s\n' $p $T $loop > /tmp/traces/.ue$n.ctl
  mv -f /tmp/traces/.ue$n.ctl /tmp/traces/ue$n.ctl
done
echo "OK $now $T"
EOF
  ) || true
  grep -q '^OK ' <<< "$out" || die "${out:+${out#ERR }}${out:-cannot reach $UC in $NS/$POD}"
  read -r _ now T <<< "$out"
  local ues=() a
  for a in "$@"; do ues+=("${a%%:*}"); done
  echo "  at t $now s: $(names "${ues[@]}") switch at t $T s"

  # each UE answers within ~0.1 s of the file landing: the switch it scheduled, or why not
  local got i
  for i in 1 2 3 4 5 6 7 8; do
    sleep 0.5
    got=$(kxs "${ues[@]}" <<'EOF'
for n in "$@"; do
  m=$(cat /tmp/traces/.ue$n.mark 2>/dev/null || echo 0)
  l=$(tail -n +$((m + 1)) /tmp/ues/ue$n.log | grep -a '\[ZMQ chan\]' | grep -av '\[ZMQ chan\] t [0-9]' | head -1)
  echo "ue$n|${l##*\[ZMQ chan\] }"
done
EOF
    )
    grep -q '|$' <<< "$got" || break
  done
  sed 's/^\(ue[0-9]*\)|$/\1|no answer: is the UE still running? (trace_ctl.sh status)/; s/^\(ue[0-9]*\)|/    \1: /' <<< "$got"
  grep -q '|$\|channel unchanged' <<< "$got" && return 1
  [ "$WAIT" = 1 ] || return 0

  # wait for T, then show each UE's first status line from after it (the UL runs a few ms ahead of the
  # DL, so a line logged right at T can show the UL switched and the DL not yet)
  local deadline=$((SECONDS + 20 + 3 * ${LEAD%.*}))
  while [ $SECONDS -lt $deadline ]; do
    got=$(kxs "$T" "${ues[@]}" <<'EOF'
T=$1; shift
for n in "$@"; do
  l=$(grep -a '\[ZMQ chan\] t [0-9]' /tmp/ues/ue$n.log | tail -1)
  t=$(sed -E 's/.*\[ZMQ chan\] t ([0-9.]+) s.*/\1/' <<< "$l")
  awk -v t=$t -v T=$T 'BEGIN {exit !(t > T)}' || { echo WAIT; exit 0; }
  echo "ue$n: ${l##*\[ZMQ chan\] }"
done
EOF
    )
    grep -qx WAIT <<< "$got" || { echo "  now:"; sed 's/^/    /' <<< "$got"; return 0; }
    sleep 1
  done
  echo "  !! air time has not reached t $T s: is the ZMQ chain running? (trace_ctl.sh status)"
  return 1
}

case "$CMD" in
  set)
    [ ${#POS[@]} -eq 2 ] || usage
    UES=($(ues_of "${POS[0]}")) || exit 1
    resolve "${POS[1]}"
    push "$SRC" "$NAME"
    PAIRS=(); for n in "${UES[@]}"; do PAIRS+=("$n:$NAME:$LOOP"); done
    echo "$(names "${UES[@]}") -> $NAME ($([ "$LOOP" = 1 ] && echo looping || echo once))"
    switch "${PAIRS[@]}"
    ;;
  stop)
    [ ${#POS[@]} -eq 1 ] || usage
    UES=($(ues_of "${POS[0]}")) || exit 1
    PAIRS=(); for n in "${UES[@]}"; do PAIRS+=("$n:none:1"); done
    echo "$(names "${UES[@]}") -> base channel"
    switch "${PAIRS[@]}"
    ;;
  apply)
    [ ${#POS[@]} -eq 1 ] || usage
    # only the UEs running with trace control; table lines for other UEs are skipped
    RUNNING=" $(ues_of all) " || exit 1
    read_table "${POS[0]}"
    KEEP=(); KEEPSRC=()
    for i in "${!PAIRS[@]}"; do
      n=${PAIRS[$i]%%:*}
      if [[ "$RUNNING" == *" $n "* ]]; then
        KEEP+=("${PAIRS[$i]}"); KEEPSRC+=("${SRCS[@]:$((2 * i)):2}")
      else
        echo "  ue$n: not running with trace control, skipped"
      fi
    done
    [ ${#KEEP[@]} -gt 0 ] || die "no UE of ${POS[0]} is running with trace control"
    push "${KEEPSRC[@]}"
    for p in "${KEEP[@]}"; do IFS=: read -r n name l <<< "$p"; echo "ue$n -> $name"; done
    switch "${KEEP[@]}"
    ;;
  check)
    [ ${#POS[@]} -eq 1 ] || usage
    read_table "${POS[0]}"
    for p in "${PAIRS[@]}"; do IFS=: read -r n name l <<< "$p"; echo -n "ue$n $name$([ "$l" = 0 ] && echo " (once)")  "; done
    echo
    ;;
  status)
    kx 'for f in $(ls /tmp/ues/ue*.log 2>/dev/null | sort -V); do
      s=$(grep -a "\[ZMQ chan\] t [0-9]" $f | tail -1); w=$(grep -a "\[ZMQ chan\]" $f | grep -av "\[ZMQ chan\] t [0-9]" | tail -1)
      echo "$(basename $f .log)  ${s##*\[ZMQ chan\] }"
      [ -n "$w" ] && echo "     last switch: ${w##*\[ZMQ chan\] }"
    done' 20
    ;;
  list)
    ls "$TR"/lib/*.csv >/dev/null 2>&1 || python3 "$GEN" --all
    python3 - "$TR/lib" <<'EOF'
import csv, glob, os, statistics, sys
print(f"{'trace':<14} {'rows':>6} {'loop s':>7}   {'DL SNR dB min/med/max':<22} {'UL SNR dB min/med/max':<22}")
for f in sorted(glob.glob(os.path.join(sys.argv[1], "*.csv"))):
    rows = [r for r in csv.reader(open(f)) if r][1:]
    t, dl, ul = ([float(r[i]) for r in rows] for i in range(3))
    loop = t[-1] + (t[-1] - t[-2]) if len(t) > 1 else 0
    span = lambda v: f"{min(v):5.1f} {statistics.median(v):5.1f} {max(v):5.1f}"
    print(f"{os.path.basename(f)[:-4]:<14} {len(t):>6} {loop:>7.0f}   {span(dl):<22} {span(ul):<22}")
EOF
    echo "Recipes: duranta-oai-ue/traces/scenarios.conf (python3 duranta-oai-ue/traces/gen_traces.py --all)"
    ;;
  *) usage ;;
esac
