#!/usr/bin/env bash
# EdgeRIC-RT muApps in the edgeric container (pod edgeric-0): start, stop, follow, and change them live.
#
#   bash scripts/edgeric_muapp.sh start sched --scheduler maxweight
#   bash scripts/edgeric_muapp.sh start sched --scheduler fixed --weights ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4
#   bash scripts/edgeric_muapp.sh start sched --scheduler rl --model runs/ppo_4ue/best.npz
#   bash scripts/edgeric_muapp.sh start train --ues 4 --iters 100 --tag ppo_4ue
#   bash scripts/edgeric_muapp.sh ctl "scheduler rr"          live switch of the running sched muApp
#   bash scripts/edgeric_muapp.sh logs [sched|train]           follow the output
#   bash scripts/edgeric_muapp.sh stop [sched|train|all]
#   bash scripts/edgeric_muapp.sh status
#   bash scripts/edgeric_muapp.sh run <script.py> [args]       anything else under edgeric-rt/, in the foreground
#
# muApps:  sched -> muapps/sched_muapp.py   train -> muapps/train_ppo.py   eval -> muapps/eval.py
# Output goes to edgeric-rt/runs/ (bind-mounted at /edgeric/runs). One reports consumer at a time: the bridge
# sends the per-TTI reports to a single socket, so stop sched before train and vice versa.
set -u
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$(k3d kubeconfig write "${K3D_CLUSTER:-janus-cluster}" 2>/dev/null)}"
NS=ran; POD=edgeric-0; C=edgeric
usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit "${1:-1}"; }
ex() { kubectl exec -n $NS $POD -c $C -- bash -c "$1"; }
# A muApp that exited stays a zombie (the container's PID 1 is sleep, which never reaps), so a live pid is one
# that kill -0 reaches and ps does not show in state Z.
ALIVE='alive() { local p; p=$(cat /edgeric/runs/.$1.pid 2>/dev/null) || return 1; kill -0 $p 2>/dev/null && [ "$(ps -o stat= -p $p | tr -d " " | cut -c1)" != Z ]; }'

script_of() {
  case "$1" in
    sched) echo muapps/sched_muapp.py ;;
    train) echo muapps/train_ppo.py ;;
    eval)  echo muapps/eval.py ;;
    *) echo ""; return 1 ;;
  esac
}

[ $# -ge 1 ] || usage
CMD=$1; shift
timeout 15 kubectl get pod -n $NS $POD >/dev/null 2>&1 || {
  echo "ERROR: pod $NS/$POD not found. Deploy it: build edgeric:local, k3d image import, helm upgrade (README)."; exit 1; }

case "$CMD" in
  start)
    [ $# -ge 1 ] || usage
    NAME=$1; shift
    S=$(script_of "$NAME") || { echo "unknown muApp $NAME (sched train eval)"; exit 1; }
    ARGS=$(printf ' %q' "$@")
    ex "$ALIVE; mkdir -p /edgeric/runs; if alive $NAME; then
          echo '$NAME is already running (pid '\$(cat /edgeric/runs/.$NAME.pid)'): stop it first'; exit 1; fi
        cd /edgeric && setsid -f python3 -u $S $ARGS > /edgeric/runs/.$NAME.out 2>&1 < /dev/null
        for i in \$(seq 1 20); do p=\$(pgrep -n -f 'python3 -u $S'); [ -n \"\$p\" ] && break; sleep 0.2; done
        echo \$p > /edgeric/runs/.$NAME.pid; sleep 3; echo \"started $NAME (pid \$p)\"; tail -n 5 /edgeric/runs/.$NAME.out"
    ;;
  stop)
    WHAT=${1:-all}
    for n in sched train eval; do
      [ "$WHAT" = all ] || [ "$WHAT" = "$n" ] || continue
      ex "$ALIVE; f=/edgeric/runs/.$n.pid; [ -f \$f ] || exit 0; p=\$(cat \$f); if alive $n; then kill -INT \$p;
          for i in \$(seq 1 30); do alive $n || break; sleep 0.5; done; alive $n && kill -9 \$p; echo 'stopped $n'; fi; rm -f \$f"
    done
    ;;
  logs)
    ex "tail -n 40 -f /edgeric/runs/.${1:-sched}.out"
    ;;
  status)
    ex "$ALIVE"'; for n in sched train eval; do if alive $n; then
          echo "== $n (pid $(cat /edgeric/runs/.$n.pid)) =="; tail -n 3 /edgeric/runs/.$n.out; fi; done'
    ;;
  ctl)
    [ $# -ge 1 ] || usage
    ex "python3 - $(printf '%q' "$*") <<'EOF'
import os, socket, sys, tempfile
run = os.path.join(os.environ.get('EDGERIC_RT_APP_DIR', '/apps/edgeric_rt'), 'run')
me = os.path.join(tempfile.mkdtemp(), 'ctl.sock')
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); s.bind(me); s.settimeout(3)
s.sendto(sys.argv[1].encode(), os.path.join(run, 'sched_ctl.sock'))
try:
    print(s.recv(4096).decode())
except socket.timeout:
    print('no reply: is the sched muApp running?')
EOF"
    ;;
  run)
    [ $# -ge 1 ] || usage
    S=$1; shift
    ex "cd /edgeric && python3 -u $S $(printf ' %q' "$@")"
    ;;
  -h|--help) usage 0 ;;
  *) echo "unknown command $CMD"; usage ;;
esac
