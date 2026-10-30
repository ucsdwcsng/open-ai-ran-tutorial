#!/usr/bin/env bash
# The gNB console: the gNB's stdout (cell, N2, then one metrics row per UE every second) without the
# jbpf agent's log lines. setup_zmq_chan_demo.sh --ran ends here.
#
#   bash scripts/gnb_console.sh [LINES]     LINES of history first (default 25)
#
# Ctrl-C leaves the console; the gNB keeps running. The console reads the new part of /tmp/gnb.stdout once
# a second with a short kubectl exec, so it leaves nothing running in the gNB container however it ends (a
# closed window, a timeout), and it starts over when a gNB restart truncates the file.
set -u
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
N=${1:-25}
[[ "$N" =~ ^[0-9]+$ ]] || { echo "usage: bash scripts/gnb_console.sh [LINES]"; exit 1; }
F=/tmp/gnb.stdout
R=$(mktemp); B=$(mktemp); trap 'rm -f "$R" "$B"' EXIT

fetch() {  # fetch OFFSET: the file's size on one line, then its bytes from OFFSET up to that size
  timeout 10 kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- bash -c \
    "s=\$(stat -c %s $F 2>/dev/null || echo 0); echo \$s; [ \$s -gt $1 ] && tail -c +$(($1 + 1)) $F | head -c \$((s - $1)); true" \
    >"$R" 2>/dev/null
}

off=0; first=1
while :; do
  if fetch "$off" && s=$(head -n 1 "$R") && [[ "$s" =~ ^[0-9]+$ ]]; then
    if [ "$s" -lt "$off" ]; then off=0; continue; fi      # truncated: a new gNB
    tail -n +2 "$R" >"$B"
    n=$(tr -dc '\n' <"$B" | wc -c)                        # whole lines only; a partial one waits
    if [ "$n" -gt 0 ]; then
      head -n "$n" "$B" >"$R"
      off=$((off + $(wc -c <"$R")))
      if [ "$first" = 1 ]; then grep -av '\[JBPF\]' "$R" | tail -n "$N"; else grep -av '\[JBPF\]' "$R"; fi
    fi
    first=0
  fi
  sleep 1
done
