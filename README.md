# Open AI-RAN tutorial — OCUDU + jbpf 5G testbed

A **k3d + Helm 5G testbed**: the **OCUDU gNB** (jbpf-instrumented, from the [`ocudu-jbpf`](ocudu-jbpf)
submodule) + [jrt-controller](https://github.com/microsoft/jrtc) + Open5GS core + **N Duranta OAI
`nr-UE`s** over ZMQ (up to 64; this guide uses 4), each with its own emulated channel, joined onto one
radio path by a ZMQ broker.

jbpf **codelets** stream live per-layer telemetry to Grafana and **actuate the RAN** at runtime — dynamic
RLC buffer sizing, DL scheduling (PRB-share), DL MCS override, L4S ECN marking, and **EdgeRIC-RT**: a
scheduler muApp in the loop every TTI, including a PPO policy trained online.

The step-by-step tutorial, with demos, is the
**[Open AI-RAN tutorial](https://edgeric.github.io/open-ai-ran-tutorial.html)** on the EdgeRIC site.

Path-independent: every command derives paths from `REPO_ROOT`, so the checkout can live anywhere.

---

## Contents

| Step | What |
|---|---|
| [Quick start](#quick-start) | One-time setup done? Four UEs with telemetry in two commands |
| [0](#0-prerequisites) | Prerequisites |
| [1](#1-clone--init-the-submodules) | **Clone & init the submodules** |
| [2](#2-create-the-cluster) | Create the cluster |
| [3](#3-deploy-the-open5gs-core--4-subscribers) | Open5GS core + 4 subscribers |
| [4](#4-deploy-the-ran-pods-and-grafana) | Deploy the RAN pods (gNB, jrt-controller, EdgeRIC) + Grafana |
| [5](#5-build-the-images-one-time) | Build images (broker / gNB / UE / EdgeRIC) + codelets |
| [6](#6-start-the-radio-broker--gnb--ues) | Start the radio — **scripted N-UE bring-up**, or by hand |
| [7](#7-traffic) | Traffic |
| [8](#8-codelets-load--unload) | **Load / unload codelets** |
| [9](#9-telemetry-and-grafana) | **Telemetry and Grafana** |
| [10](#10-control-codelets) | **Control codelets** (RLC buffer · DL sched · DL MCS · L4S) |
| [11](#11-stop--status) | Stop & status |
| [12](#12-the-buffer-control-demos) | **The buffer-control demos** — fixed caps on one UE, then a limit per UE |
| [13](#13-edgeric-rt-tti-synchronized-scheduling-and-the-rl-muapp) | **EdgeRIC-RT** — TTI-synchronized scheduling loop, scheduler muApps, PPO |
| [14](#14-per-ue-channels-and-traces) | **Per-UE channels and traces**, switched while traffic runs |
| [Reference](#reference) | Repo layout, codelets & xApps, hooks, scripts, docs |

---

## Quick start

With the one-time setup done (§0–§5, and the gNB pod's containers from §6a):

```bash
bash scripts/setup_zmq_chan_demo.sh 4 --telemetry   # broker -> gNB -> 4 UEs, telemetry loaded (~3-5 min)
bash scripts/traffic_nue.sh start                   # one DL TCP flow per UE
#   Grafana: http://localhost:30490/d/upt-userplane  (admin / admin)
bash scripts/traffic_nue.sh stop
bash scripts/stop_demo.sh
```

After a cluster restart, do the steps in [§6, after a cluster (re)start](#after-a-cluster-restart) first.

---

## 0. Prerequisites

- `docker`, `k3d` (v5+), `kubectl`, `helm` (v3+), `git`, and `python3` (the scripts run it on the host)
- Current user in the `docker` group
- ≥ 8 CPU cores — the ZMQ chain runs in real time

Tested on Ubuntu 24.04 with k3d v5.8.3 (k3s v1.31) and helm v3.18.4.

---

## 1. Clone & init the submodules

Three git submodules must be initialized (`--recursive` — `jbpf_protobuf` has its own nested submodules):
- **`ocudu-jbpf`** — the jbpf-enabled **OCUDU gNB** (the RAN, → [`ucsdwcsng/open-ai-ran-ocudu-jbpf`](https://github.com/ucsdwcsng/open-ai-ran-ocudu-jbpf))
- **`duranta-oai-ue/duranta-ue`** — the **Duranta OAI `nr-UE`** source (→ [`ucsdwcsng/open-ai-ran-duranta-ue`](https://github.com/ucsdwcsng/open-ai-ran-duranta-ue)):
  the ZMQ radio with a per-UE channel model and SNR trace replay.
  Step 5c builds the UE image from this checkout.
- **`jrtc-apps/jbpf_protobuf`** — the jbpf protobuf/serde SDK (→ `microsoft/jbpf-protobuf`)

```bash
git clone https://github.com/ucsdwcsng/open-ai-ran-tutorial.git
cd open-ai-ran-tutorial
git submodule update --init --recursive      # pulls ocudu-jbpf (RAN), duranta-ue (UE) + jbpf_protobuf (SDK)

export REPO_ROOT="$(pwd)"
export CLUSTER=janus-cluster
```

> In any **new** terminal:
> ```bash
> export REPO_ROOT=/path/to/open-ai-ran-tutorial
> export CLUSTER=janus-cluster
> export KUBECONFIG="$(k3d kubeconfig write "$CLUSTER")"
> ```

All three submodules are public and clone over HTTPS; no SSH key is needed.

To pull later submodule updates: `git submodule update --remote ocudu-jbpf duranta-oai-ue/duranta-ue`
(both follow their `main` branch).

---

## 2. Create the cluster

```bash
k3d cluster create "$CLUSTER" \
  --volume "$REPO_ROOT:$REPO_ROOT" \
  --port "30400-30500:30400-30500@loadbalancer"

export KUBECONFIG="$(k3d kubeconfig write "$CLUSTER")"

# Multus CNI (RAN pods use it)
kubectl apply -f https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/master/deployments/multus-daemonset-thick.yml
kubectl wait --for=condition=ready pod -l app=multus -n kube-system --timeout=120s
```

The `--volume` bind-mount makes built binaries persist on the host across pod restarts.

---

## 3. Deploy the Open5GS core + 4 subscribers

```bash
kubectl create namespace open5gs
helm install open5gs "$REPO_ROOT/open5gs" -n open5gs -f "$REPO_ROOT/open5gs/values-5g.yaml"
until kubectl get pod -n open5gs -l app.kubernetes.io/name=mongodb -o name | grep -q .; do sleep 2; done
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=mongodb -n open5gs --timeout=180s

# pcf/udr start before mongodb is ready — restart them once
kubectl get pods -n open5gs --no-headers | awk '/pcf|udr/{print $1}' | xargs -r kubectl -n open5gs delete pod
kubectl get pods -n open5gs -w   # Ctrl-C when all pods are 1/1
```

### Subscribers (PLMN 99970, IMSIs ...001–004)

```bash
POP=$(kubectl get pods -n open5gs --no-headers | awk '/populate/{print $1}' | head -1)
K=00112233445566778899aabbccddeeff
OPC=63bfa50ee6523365ff14c1f45f88737d
for IMSI in 999700000000001 999700000000002 999700000000003 999700000000004; do
  kubectl exec -n open5gs "$POP" -- open5gs-dbctl add "$IMSI" "$K" "$OPC"
done
kubectl exec -n open5gs "$POP" -- open5gs-dbctl showall | grep -c imsi   # expect 4
```

The scripted bring-up (§6) provisions the IMSI of every UE it starts, so more than four UEs need
nothing extra here.

---

## 4. Deploy the RAN pods and Grafana

This deploys the pod scaffolding — `srs-gnb-du1-0` (the gNB pod, with the jbpf IPC volumes and the
`srs-gnb-du1-proxy` service), `jrtc-0` (the jrt-controller) and `edgeric-0` (the EdgeRIC-RT muApp host,
§13). The OCUDU jbpf gNB, the broker and the UEs run as **ephemeral containers** in the gNB pod (§6).
`edgeric-0` needs the `edgeric:local` image (§5e): build it first, or the pod waits in
`ImagePullBackOff` until you import it.

```bash
sed "s#__REPO_ROOT__#$REPO_ROOT#g" \
  "$REPO_ROOT/jrtc-apps/containers/Helm/k3d-values.yaml" > /tmp/k3d-values.local.yaml

kubectl create namespace ran
USE_JRTC=1 helm install ran "$REPO_ROOT/jrtc-apps/containers/Helm" -n ran \
  -f "$REPO_ROOT/jrtc-apps/containers/Helm/jrtc.yaml" \
  -f /tmp/k3d-values.local.yaml

kubectl -n ran rollout status statefulset/srs-gnb-du1 --timeout=300s
kubectl get pods -n ran            # srs-gnb-du1-0, jrtc-0 and edgeric-0 Running
```

> If a UE attaches but no traffic flows, the SMF still points at an old UPF pod IP: re-point it as in
> [§6, after a cluster (re)start](#after-a-cluster-restart). The scripted bring-up binds the gNB's N3 to its
> current pod IP; for the by-hand path, see the pod-IP note in the `ocudu-jbpf/deploy/` configs.

### Grafana and VictoriaMetrics

```bash
kubectl create configmap grafana-upt-dashboard -n ran \
  --from-file=upt-dashboard.json="$REPO_ROOT/telemetry/upt-dashboard.json" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f "$REPO_ROOT/telemetry/telemetry-stack.yaml"
kubectl rollout status deployment/grafana -n ran --timeout=120s
```

xApps push metrics to VictoriaMetrics (`:30491`) in InfluxDB line protocol, and Grafana (`:30490`,
`admin` / `admin`) queries it. The dashboards are in §9 and §13.

---

## 5. Build the images (one-time)

```bash
# 5a. Broker: the container image (grbroker), and the C++ ZMQ broker that runs in it for the
#     scripted bring-up (§6; built in an ubuntu:22.04 container, the binary is gitignored)
docker build -t gnuradio-broker:local "$REPO_ROOT/broker"
k3d image import gnuradio-broker:local -c "$CLUSTER"
bash "$REPO_ROOT/duranta-oai-ue/broker_cpp/build.sh"

# 5b. OCUDU + jbpf gNB (from the submodule, with every hook used here, including the EdgeRIC-RT
#     slot hooks) -> ocudu-gnb-jbpf:local  (~15 min, clang-18)
( cd "$REPO_ROOT/ocudu-jbpf" && ./build.sh )
k3d image import ocudu-gnb-jbpf:local -c "$CLUSTER"

# 5c. Duranta OAI nr-UE (ZMQ) -> duranta-nr-ue:local  (from the duranta-ue submodule, as checked out)
docker build -t duranta-nr-ue:local "$REPO_ROOT/duranta-oai-ue"
k3d image import duranta-nr-ue:local -c "$CLUSTER"

# 5d. Codelets (built with the public jbpf SDK image; .o are gitignored)
cd "$REPO_ROOT/jrtc-apps/codelets"
# every directory that has a codeletset yaml -- a missing .o only shows up later as a
# codeletset that will not load (.o are gitignored, so a fresh clone has none)
for d in rlc mac pdcp ngap rrc ue_contexts perf edgeric edgeric_rt dashboard_realtime_scheduling \
         bufctl codel l4span upt fapi_dl_conf fapi_ul_conf fapi_ul_crc fapi_rach xran_packets; do ./make.sh -d "$d"; done
# slice_mgmt is deliberately NOT in the list: slice_mgmt.cpp does not compile against
# this SDK image (JBPF_MAX_SLICES is undeclared), so slice_mgmt.o has never existed and
# its codeletset cannot load. Add it back only once the source builds.
# bufcap is not in the list either: buffer-control-experiments/bufcap.sh builds each cap on demand (§12).
cd "$REPO_ROOT"
```

Each codelet should print `Program terminates within N instructions` (jbpf verifier passed).

```bash
# 5e. EdgeRIC-RT muApp host -> edgeric:local (pod edgeric-0, deployed by the chart in §4; muApp code is bind-mounted)
docker build -t edgeric:local "$REPO_ROOT/edgeric-rt"
k3d image import edgeric:local -c "$CLUSTER"
```

Build it before §4. On an existing release, `helm upgrade` adds `edgeric-0` but also restarts `jrtc-0`, so do that
before bringing the radio up (§13).

---

## 6. Start the radio (broker → gNB → UEs)

The gNB, broker, and UEs run as **ephemeral containers** in `srs-gnb-du1-0` (they share the pod
network namespace, so ZMQ talks over localhost, and the gNB shares `/dev/shm` + `/tmp/jbpf` with
`jrtc-0` for the jbpf IPC). Add the containers once per pod (§6a), then bring the chain up with the
[script](#scripted-bring-up-n-ues-each-with-its-own-channel) (recommended) or
[by hand](#by-hand-4-ues-on-the-gnu-radio-broker) (§6b–6d).

### After a cluster (re)start

The UPF comes back with a new pod IP, which the SMF reads from static config: skip this and the UEs
attach but pass no data. The ephemeral containers are gone too, and cannot be re-added under the same
name. So re-point the SMF, then recreate the gNB pod before §6a:

```bash
k3d cluster start "$CLUSTER"
kubectl get pods -n open5gs; kubectl get pods -n ran      # wait until all are Running

# SMF -> the UPF's current pod IP
UPFPOD=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name | grep '^open5gs-upf-')
UPFIP=$(kubectl get pod -n open5gs "$UPFPOD" -o jsonpath='{.status.podIP}'); echo "UPF=$UPFIP"
kubectl get cm -n open5gs open5gs-smf -o json > /tmp/smf.json
python3 - "$UPFIP" <<'PY'
import json,re,sys; ip=sys.argv[1]
d=json.load(open("/tmp/smf.json")); k=list(d["data"])[0]
d["data"][k]=re.sub(r"(\n      upf:\n      - address:)[^\n]*", r"\g<1> "+ip, d["data"][k], count=1)
assert ("address: "+ip) in d["data"][k]; json.dump(d,open("/tmp/smf.json","w")); print("smf upf ->",ip)
PY
kubectl replace -f /tmp/smf.json
kubectl rollout restart deploy/open5gs-smf -n open5gs && kubectl rollout status deploy/open5gs-smf -n open5gs
kubectl logs -n open5gs deploy/open5gs-upf --tail=20 | grep -i associat      # expect "PFCP associated"

# a fresh gNB pod; then add its containers (§6a)
kubectl delete pod -n ran srs-gnb-du1-0
until kubectl wait -n ran --for=condition=Ready pod/srs-gnb-du1-0 --timeout=300s 2>/dev/null; do sleep 3; done
```

### 6a. Add the ephemeral containers (one-time per pod)

```bash
# GNU Radio broker
kubectl debug -n ran srs-gnb-du1-0 --image=gnuradio-broker:local --image-pull-policy=IfNotPresent \
  -c grbroker --target=gnb -- sleep infinity

# Duranta UE runner (privileged, for netns)
kubectl debug -n ran srs-gnb-du1-0 --image=duranta-nr-ue:local --image-pull-policy=IfNotPresent \
  -c durue1 --target=gnb --profile=sysadmin -- sleep infinity

# OCUDU jbpf gNB — needs the jbpf IPC volume mounts, which `kubectl debug` can't set, so patch
# the ephemeralContainers subresource directly (image + targetContainer + dshm/varjbpf mounts):
kubectl patch pod -n ran srs-gnb-du1-0 --subresource ephemeralcontainers --type strategic \
  -p "$(cat "$REPO_ROOT/ocudu-jbpf/deploy/ephem_jbpf.json")"

kubectl get pod -n ran srs-gnb-du1-0 -o jsonpath='{.status.ephemeralContainerStatuses[*].name}'; echo
```

### Scripted bring-up: N UEs, each with its own channel

```bash
bash scripts/setup_zmq_chan_demo.sh 4 --telemetry      # ~3-5 min
```

It stops anything still running, provisions the IMSIs, stages the configs and one network namespace per
UE, then starts the C++ broker, the gNB and the UEs, in that order. It puts the gNB and broker threads on
`SCHED_FIFO` and the testbed on one NUMA node (`scripts/zmq_tune.sh`), and checks that every UE attached
(`SETUP DONE. 4 UE(s) attached.`).

| Option | Effect |
|---|---|
| `--telemetry` | fresh `jrtc-0` with the forwarder (§6b) and the `upt` and dashboard xApps, loaded before the UEs attach (§9) |
| `--edgeric` | fresh `jrtc-0` with the EdgeRIC-RT codelets and bridge (§13) |
| `--channel FILE` | a base channel per UE from a table (default `duranta-oai-ue/ue_channel.conf`: static 30 dB) (§14) |
| `--traces FILE` | per-UE SNR traces from attach, switchable at run time (§14) |
| `--clean` | no channel model and no added noise |
| `--ran`, `--ues` | one run split across two terminals: the RAN, ending in the gNB console (`scripts/gnb_console.sh`), then the UEs with the same N |
| `--no-tune`, `--tcp`, `--broker cpp\|py` | skip `zmq_tune.sh`; ZMQ over TCP instead of IPC sockets; the C++ (default) or the GNU Radio broker |

Any N up to 64 works; four UEs keep up with real time on a 2 × Xeon Gold 5218 server
(`bash scripts/zmq_rt_check.sh` measures it). ZMQ cannot reconnect a running chain, so every run restarts
the broker, gNB and UEs together; re-running the script is also how you recover from a partial attach.
Extra `nr-uesoftmodem` / gNB options go in `UE_ARGS` / `GNB_ARGS`; the script prints its full usage when
run without arguments. Walkthrough:
[`docs/run_system.md`](docs/run_system.md).

### By hand: 4 UEs on the GNU Radio broker

The same chain step by step, with the Python GNU Radio broker.

### 6b. Start the forwarder in the jrt-controller (needed for `jrtc-ctl`)

`jrtc-ctl` hard-codes `127.0.0.1:30450`; `telemetry/fwd30450.py` bridges it to the gNB proxy. It must be
running for any codelet load/unload (`--telemetry` and `--edgeric` start it). Use this file, not
`ocudu-jbpf/deploy/fwd.py`: the two differ and are not interchangeable.

```bash
kubectl cp "$REPO_ROOT/telemetry/fwd30450.py" ran/jrtc-0:/tmp/fwd30450.py -c jrtc
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'grep -qi 76F2 /proc/net/tcp && echo "forwarder already listening" ||
  { nohup setsid python3 -u /tmp/fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 2; cat /tmp/fwd30450.log; }'
```

### 6c. Stage configs + codelets into the gNB container

```bash
GDIR="$REPO_ROOT/ocudu-jbpf/deploy"; UDIR="$REPO_ROOT/duranta-oai-ue"
# gNB config (jbpf IPC to jrt_controller)
kubectl cp "$GDIR/gnb_ocudu_jbpf.yml" ran/srs-gnb-du1-0:/tmp/gnb_ocudu_jbpf.yml -c ocudujbpf
# broker + UE configs
kubectl cp "$UDIR/broker4_netns.py"   ran/srs-gnb-du1-0:/tmp/broker4_netns.py -c grbroker
for n in 1 2 3 4; do kubectl cp "$UDIR/oaiue${n}_netns.conf" ran/srs-gnb-du1-0:/tmp/oaiue${n}_netns.conf -c durue1; done
# UE capability file -- REQUIRED. Without it nr-uesoftmodem aborts with
# "Assertion (max_mimo_layers > 0) failed!" and the UE never attaches.
kubectl cp "$REPO_ROOT/duranta-oai-ue/uecap_ports1.xml" \
  ran/srs-gnb-du1-0:/tmp/uecap_ports1.xml -c durue1

# the codelets the gNB agent loads serdes from
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- mkdir -p /codelets
kubectl cp "$REPO_ROOT/jrtc-apps/codelets/edgeric"  ran/srs-gnb-du1-0:/codelets/ -c ocudujbpf
kubectl cp "$REPO_ROOT/jrtc-apps/codelets/rlc"      ran/srs-gnb-du1-0:/codelets/ -c ocudujbpf
```

### 6d. Bring it up — **order matters: broker → gNB → UEs**

```bash
# 1) broker
kubectl exec -n ran srs-gnb-du1-0 -c grbroker -- bash -c \
  'cd /tmp && nohup setsid python3 -u broker4_netns.py >/tmp/broker.log 2>&1 </dev/null & disown; sleep 3; echo broker:$(pgrep -x python3)'

# 2) OCUDU jbpf gNB — jbpf inits + registers to jrt_controller, then Connected to AMF
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- bash -c \
  'cd /tmp && nohup setsid /usr/local/bin/gnb -c gnb_ocudu_jbpf.yml >/tmp/gnb.stdout 2>&1 </dev/null & disown; sleep 12; \
   grep -aoE "Registration succeeded|Connected to AMF" /tmp/gnb.stdout /tmp/gnb.log | tail -2'

# 3) per-UE netns + 4 UEs (each UE gets its own oaitun_ue1)
kubectl cp "$UDIR/netns_setup.sh" ran/srs-gnb-du1-0:/tmp/netns_setup.sh -c durue1
kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- bash -c 'mkdir -p /var/run/netns && bash /tmp/netns_setup.sh'
kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- bash -c '
  cd /opt/oai-nr-ue/bin
  for n in 1 2 3 4; do
    nohup setsid ip netns exec ue$n ./nr-uesoftmodem -O /tmp/oaiue${n}_netns.conf \
        --band 78 --numerology 1 -r 51 -C 3489420000 --ue-scan-carrier -E >/tmp/ue$n.log 2>&1 </dev/null & disown
    sleep 4
  done'

# verify all 4 attached (distinct TUN IPs)
kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- bash -c \
  'for n in 1 2 3 4; do echo -n "ue$n:$(ip netns exec ue$n ip -o -4 addr show oaitun_ue1 2>/dev/null|awk "{print \$4}") "; done; echo'
```

> ZMQ can't reconnect mid-run — always restart **broker → gNB → UEs** together. SIGKILL'ing the gNB
> leaves a stale jbpf IPC peer in jrt-controller; retry the gNB start after jrtc reaps it.

---

## 7. Traffic

After the scripted bring-up:

```bash
bash scripts/traffic_nue.sh start      # one DL TCP flow per UE, from the UPF, CUBIC
bash scripts/traffic_nue.sh status     # per-UE and total goodput, measured at the UEs
bash scripts/traffic_nue.sh stop
```

`start` also takes `--cc bbr`, `--time S`, `--rate R`, `--ue N`, and `--udp RATE` (UDP per UE, e.g. `40M`;
add `--plain` for iperf2 with `scripts/udp_sink.pl` as the receiver, which a UE starved for seconds cannot
lose). With four UEs on clean channels the cell carries about 59 Mbit/s of TCP.

By hand: iperf servers on the UPF, clients per UE inside each netns.

```bash
UPF=$(kubectl get pods -n open5gs -o name | grep -i upf | head -1); UPF=${UPF#pod/}
kubectl exec -n open5gs "$UPF" -- bash -c 'for p in 5201 5202 5203 5204; do nohup iperf3 -s -B 10.45.0.1 -p $p >/dev/null 2>&1 & done; echo servers up'

# concurrent DL (-R) on all 4 UEs
kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- bash -c \
  'for n in 1 2 3 4; do p=$((5200+n)); ip netns exec ue$n iperf3 -c 10.45.0.1 -p $p -t 10 -R >/tmp/dl$n.txt 2>&1 & done; wait; \
   for n in 1 2 3 4; do echo "ue$n: $(grep receiver /tmp/dl$n.txt | tail -1 | grep -oE "[0-9.]+ [KMG]bits/sec")"; done'
```

For UL, drop the `-R`. (About 20 Mbit/s aggregate DL on this path, with the GNU Radio broker.)

---

## 8. Codelets: load / unload

Codelets are loaded/unloaded through the jrt-controller with `jrtc-ctl`. Run it inside `jrtc-0`
(with the forwarder from §6b running). A deployment YAML names the codeletset(s) + optional Python
xApp; `jrtc-apps/jrtc_apps/*` and `jrtc-apps/codelets/*` are bind-mounted into `jrtc-0` as `/apps`
and `/codelets`.

```bash
# helper
JRTC() { kubectl exec -n ran jrtc-0 -c jrtc -- bash -c \
  "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl $*"; }

# LOAD a codeletset + xApp
JRTC 'load -c /apps/rlc/deployment_fixed.yaml'      # RLC telemetry (decodes on jrtc-decoder)

# UNLOAD it
JRTC 'unload -c /apps/rlc/deployment_fixed.yaml'
```

Gotchas (see `docs/codelet-deployment.md`):
- **The forwarder (`fwd30450.py`, §6b) must be running** or the load fails `connection refused` on
  `127.0.0.1:30450`.
- The gNB agent reads codelet serde `.so` from `/codelets/...` — the dir must be present in the
  `ocudujbpf` container (copied in §6c; `--telemetry` copies `upt ue_contexts mac rlc pdcp rrc ngap bufctl`).
- jrtc app ids are **numeric**; clear a wedged app with
  `kubectl exec -n ran jrtc-0 -c jrtc -- curl -s -X DELETE http://127.0.0.1:3001/app/<n>`.
- Avoid rapid load/unload churn — it can wedge the app-loader / stress the jbpf LCM.

Verify a codeletset attached:
```bash
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -aoE "Registered codelet [a-z_0-9]+ to hook [a-z_0-9]+" /tmp/gnb.stdout | tail
```

---

## 9. Telemetry and Grafana

`setup_zmq_chan_demo.sh --telemetry` loads two xApps into a fresh `jrtc-0` before the UEs attach:

- **`upt_app`** (`jrtc_apps/upt`): per-packet user-plane telemetry — per-UE DL GTP arrival rate, RLC
  queuing latency and RLC buffer occupancy, in 134 ms buckets.
- **The dashboard xApp** (`jrtc_apps/dashboard`): per-layer, per-UE MAC / RLC / PDCP / RRC / NGAP
  statistics (JSON in the `jrtc-0` log), plus DL CQI, UL SNR, DL MCS and DL MAC throughput, and each UE's
  identity from IMSI and IP in the core down to its C-RNTI (`scripts/core_ue_info.sh`; re-run it after
  the UEs re-attach).

Open **<http://localhost:30490/d/upt-userplane>** (`admin` / `admin`; from a laptop, forward the port
first: `ssh -N -L 30490:localhost:30490 <user>@<testbed-host>`). The rows are UE identity, Radio / MAC,
RLC / user plane, and TCP. Panels stay empty until traffic runs: the codelets fire per packet.

The TCP row comes from `telemetry/tcp_probe.py` (tcpdump on the UPF). Start it after the UEs attach, and
again after they re-attach:

```bash
bash scripts/tcp_probe.sh          # runs in the foreground until Ctrl-C
```

The per-TTI scheduling dashboard (`/d/rt-scheduling`) is in §13.

### Loading the dashboard xApp by hand

The dashboard xApp aggregates MAC / RLC / PDCP / CRC / HARQ / RRC / NGAP telemetry into per-UE JSON.
Use the OCUDU-tuned deployment (correct hosts/ports; L2/L3 codeletsets — FAPI is omitted because
OCUDU's FAPI structs differ from the SDK codelets).

```bash
# copy the L2/L3 telemetry codelet dirs into the gNB container so the agent finds the serdes
for d in ue_contexts mac pdcp rrc ngap; do
  kubectl cp "$REPO_ROOT/jrtc-apps/codelets/$d" ran/srs-gnb-du1-0:/codelets/ -c ocudujbpf
done

JRTC() { kubectl exec -n ran jrtc-0 -c jrtc -- bash -c \
  "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl $*"; }
JRTC 'load -c /apps/dashboard/deployment_ocudu.yaml'

# generate traffic (§7), then watch the decoded per-UE telemetry:
kubectl logs -n ran jrtc-0 -c jrtc --tail=40 -f | grep -E "MAC_SCHED|RLC_|PDCP_|CRC"
```

> Load the dashboard **before** attaching UEs if you want `imsi`/UE-context populated (the
> `ue_contexts` codelet catches the RRC/NGAP attach events). Unload with the same yaml.

---

## 10. Control codelets

Writable hooks let a codelet actuate the RAN at runtime (all no-ops until a codelet drives them): the
four below, and `mac_sched_slot_ctrl`, which EdgeRIC-RT drives every TTI (§13). Load/unload exactly like
§8. (`JRTC()` helper as above.)

### 10a. RLC dynamic buffer sizing — `rlc_dl_ctrl`

Sets the DL RLC queue's byte and/or SDU-count limit per `(UE, DRB)` at run time, from an
xApp. The codelet is `bufctl`; `jrtc_apps/bufctl` is the sample xApp plus an interactive
CLI. See §12 for the guided walk-through, and [`docs/rlc-buffer-control-hook.md`](docs/rlc-buffer-control-hook.md)
for the hook API.

```bash
JRTC 'load -c /apps/bufctl/deployment.yaml'
# a command applies on the bearer's next DL packet; the gNB logs what it applied:
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  grep -ao "rlc_dl_ctrl changed .*limit\. old=[0-9]* new=[0-9]*" /tmp/gnb.log | tail -3
# over-tight requests are clamped, not rejected; drops show up as:
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -c "Dropped SDU" /tmp/gnb.log
JRTC 'unload -c /apps/bufctl/deployment.yaml'
```

> While `bufctl` is loaded the hook runs on **every** downlink packet, so unload it when
> you are not commanding. `bufctl_cli.py` does that for you on quit.

### 10b. DL scheduling (PRB-share) — `mac_sched_dl_ctrl`

EdgeRIC weight→PRB-share. Codelet `dl_sched` + xApp `dl_sched_app.py`; live policy via
`/tmp/edgeric_policy` in `jrtc-0`.

```bash
JRTC 'load -c /apps/edgeric/dl_sched_deployment.yaml'
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'echo "fixed:20,1,1,1" > /tmp/edgeric_policy'   # favor UE0
# run DL traffic (§7); the xApp log shows per-UE RB caps steer (e.g. 92/5/5/5):
kubectl logs -n ran jrtc-0 -c jrtc --tail=5 | grep edgeric_sched
# policies: maxweight (cqi*backlog) | fixed:w0,w1,w2,w3
JRTC 'unload -c /apps/edgeric/dl_sched_deployment.yaml'
```

### 10c. DL MCS override — `mac_sched_dl_mcs_ctrl`

Per-UE DL MCS override. Codelet `dl_mcs` (on the `mac_sched_dl_mcs_ctrl` hook in `grant_params_selector`)
+ xApp `dl_mcs_app.py`; live policy via `/tmp/edgeric_mcs_policy` in `jrtc-0`. Built by §5d
(`./make.sh -d edgeric` builds both `dl_sched` and `dl_mcs`) and its serde ships in the same
`edgeric/` dir copied to the gNB in §6c.

```bash
JRTC 'load -c /apps/edgeric/dl_mcs_deployment.yaml'

# force a low MCS on all UEs (stark control demo); run DL traffic (§7) to see it:
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'echo "fixed:5" > /tmp/edgeric_mcs_policy'
kubectl logs -n ran srs-gnb-du1-0 -c ocudujbpf --tail=0 2>/dev/null; \
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- bash -c \
  'grep -aoE "PDSCH: .* mod=[0-9]+QAM" /tmp/gnb.log | tail -3'    # QAM order drops with MCS
kubectl logs -n ran jrtc-0 -c jrtc --tail=3 | grep edgeric_mcs   # ue0(cqi=15,cur_mcs=27->5) ...

# policies (written to /tmp/edgeric_mcs_policy):
#   passthrough        -> no override (stock link adaptation)   [default]
#   fixed:N            -> force MCS N on all UEs
#   fixed:m0,m1,m2,m3  -> per-UE MCS
#   offset:K           -> cur_mcs + K per UE (K may be negative), clamped to [0,28]

JRTC 'unload -c /apps/edgeric/dl_mcs_deployment.yaml'
```

> Keep `dl_sched` and `dl_mcs` unloaded while EdgeRIC-RT (§13) runs: all three act on the same scheduler.

### 10d. L4S ECN marking — `pdcp_dl_sdu_segment`

In-place ECN marking of DL DRB segments (in-RAN L4S).

```bash
JRTC 'load -c /apps/l4span/deployment.yaml'
JRTC 'unload -c /apps/l4span/deployment.yaml'
```

---

## 11. Stop & status

```bash
# stop
bash scripts/traffic_nue.sh stop
bash scripts/edgeric_muapp.sh stop all       # EdgeRIC-RT muApps (§13); stop_demo.sh leaves edgeric-0 alone
bash scripts/stop_demo.sh                    # unloads bufctl if loaded, then stops iperf3, tcp_probe, the UEs,
                                             # the gNB (and truncates its log) and the broker, Python or C++

# status / logs
bash scripts/zmq_rt_check.sh                 # % of real time, lateness, CPU, each UE's channel
bash scripts/gnb_console.sh                  # one metrics row per UE every second (Ctrl-C leaves it)
kubectl get pods -A | grep -E "ran|open5gs"
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- tail -5 /tmp/gnb.log        # real gNB log
kubectl logs -n ran jrtc-0 -c jrtc --tail=30                                  # jrt-controller / xApps

# by hand: stop the radio (in the gNB pod)
kubectl exec -n ran srs-gnb-du1-0 -c durue1    -- pkill -9 -x nr-uesoftmodem
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- pkill -9 -x gnb
kubectl exec -n ran srs-gnb-du1-0 -c grbroker  -- pkill -9 -x python3

# the cluster
k3d cluster stop "$CLUSTER"                  # resume: §6, after a cluster (re)start
k3d cluster delete "$CLUSTER"                # tear it down
```

---

## 12. The buffer-control demos

The guided walk-through is [Demo 3](https://edgeric.github.io/open-ai-ran-tutorial/demo3-buffer-management.html) of the Open AI-RAN tutorial,
in two parts:

| | What it shows |
|---|---|
| Part 1 | **One UE.** A 2 Mbit/s "video call" plus a bulk download on the same bearer: the download fills the queue and the call's RTT climbs to ~600 ms. Stepping a fixed cap down (`bufcap`) fixes the latency long before it costs throughput. |
| Part 2 | **Two UEs, a limit each.** Independent downloads with different congestion control — BBR on one, CUBIC on the other — commanded per `(UE, DRB)` from `bufctl_cli.py`. Shows both that policy is per-bearer, and that the same limit affects the two transports very differently. |

Shortest path on an existing deployment (§6):

```bash
cd "$REPO_ROOT"
bash scripts/setup_zmq_chan_demo.sh 2 --telemetry     # two UEs with upt telemetry (~3-5 min)
python3 buffer-control-experiments/make_dashboard.py  # once per Grafana install: /d/demo3-bufcap
bash buffer-control-experiments/traffic_A.sh start    # part 1: video call + bulk download on UE 1
bash buffer-control-experiments/bufcap.sh load 64k    # a fixed cap; `off` restores the configured buffer
bash buffer-control-experiments/bufcap.sh off
bash buffer-control-experiments/traffic_A.sh stop
bash scripts/traffic_2ue.sh start                     # part 2: UE 1 BBR, UE 2 CUBIC
python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py      # a limit per (UE, DRB)
```

Control-hook reference (all four hooks, their contexts and conflicts):
[`docs/control-codelets.md`](docs/control-codelets.md).

---

## 13. EdgeRIC-RT: TTI-synchronized scheduling and the RL muApp

EdgeRIC's (NSDI'24) real-time loop over jbpf. Every TTI the gNB reports per-UE state tagged with its slot counter
(RANtime) through the `mac_sched_slot_report` hook. A muApp in the `edgeric` container answers with per-UE weights
for the next DL slot, and the `mac_sched_slot_ctrl` hook applies them only in exactly that TTI, splitting the slot's
PRBs by weight; every other slot runs the stock scheduler (Lazy RIC). The muApp follows the paper's RICtime rules
(Lazy RAN: wait for a newer report; Lazy RIC: act on the newest). Full guide: [docs/edgeric-rt.md](docs/edgeric-rt.md).

It needs the gNB image from §5b (it carries the two slot hooks), `edgeric-0` (§5e, §4), and the `edgeric_rt`
and `dashboard_realtime_scheduling` codelets (§5d).

```bash
bash scripts/setup_zmq_chan_demo.sh 4 --edgeric --traces duranta-oai-ue/ue_traces_cqi_4ue.conf   # CQI 4..15 channels
bash scripts/ue_rnti_map.sh 4                    # ue1..ue4 -> C-RNTI; re-run after the UEs re-attach
bash scripts/traffic_nue.sh start --udp 100M     # full buffer

# schedulers: one reports consumer at a time, so stop one muApp before starting another
bash scripts/edgeric_muapp.sh start sched --scheduler maxweight      # or rr | pf | maxcqi | bestcqi | stock |
                                                                     #    fixed --weights ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4
                                                                     # one UE per slot; --alloc shared splits RBs
bash scripts/edgeric_muapp.sh logs sched                             # every 5 s: Mbit/s per UE, DL HIT %, latency
bash scripts/edgeric_muapp.sh ctl "scheduler rr"                     # switch live
bash scripts/edgeric_muapp.sh ctl "scheduler rl models/ppo_walk4/last.npz"   # a shipped PPO policy
bash scripts/edgeric_muapp.sh stop sched

# train PPO online, or compare schedulers on the same channel (60 s each)
bash scripts/edgeric_muapp.sh start train --ues 4 --iters 150 --steps 5000 --tag my_ppo
bash scripts/edgeric_eval.sh rl:models/ppo_walk4/last.npz bestcqi maxweight pf rr stock

bash scripts/dashboard_realtime_scheduling.sh load   # Grafana "EdgeRIC real-time scheduling" (/d/rt-scheduling):
                                                     #   UE map, CQI, backlog, throughput received (TCP: tcpdump on
                                                     #   the UPF, UDP: UE receivers) and gNB throughput per UE
```

Trained policies ship in [`edgeric-rt/models/`](edgeric-rt/models/README.md): `ppo_walk4` (4 UEs, full
buffer), `ppo_walk2` (2 UEs) and `ppo_load8844` (finite loads of 8/8/4/4 Mbit/s over plain UDP). Training
writes to `edgeric-rt/runs/<tag>/` (gitignored). `bash scripts/edgeric_rt.sh status` shows the bridge's
counters; `edgeric_rt.sh unload` removes the loop.

---

## 14. Per-UE channels and traces

Every UE has its own emulated channel (Duranta's channel model in the UE): a **base channel** from a
table, and optionally an **SNR trace** that you can switch per UE while traffic runs.

```bash
# base channels (default duranta-oai-ue/ue_channel.conf: static 30 dB, CQI 15 on every UE)
bash scripts/setup_zmq_chan_demo.sh 4 --channel duranta-oai-ue/ue_channel_mobility.conf    # UEs that move

# traces: each UE replays the trace its table names, from attach (needs the C++ broker, the default)
bash scripts/setup_zmq_chan_demo.sh 4 --telemetry --traces duranta-oai-ue/ue_traces_4ue.conf

bash scripts/trace_ctl.sh list                     # the trace library
bash scripts/trace_ctl.sh status                   # each UE's channel now
bash scripts/trace_ctl.sh set 1,3 veh_30kmh        # UEs 1 and 3 switch at the same air time, 3 s from now
bash scripts/trace_ctl.sh apply duranta-oai-ue/ue_traces_cqi_4ue.conf   # a whole table
bash scripts/trace_ctl.sh stop all                 # back to the base channels
```

The library is `duranta-oai-ue/traces/lib/<name>.csv`: DL and UL SNR every 10 ms, generated from the
recipes in `duranta-oai-ue/traces/scenarios.conf` by `python3 duranta-oai-ue/traces/gen_traces.py --all`
(deterministic, so any file can be rebuilt from its line). The recipes cover random walks, constant
channels, the UE's built-in mobility model, and **CQI 4..15** processes mapped to DL SNR through
`traces/cqi_calib.csv`, including EdgeRIC's car / drone / robot / turntable traces (`traces/src_cqi/`).

| Table (`duranta-oai-ue/`) | UEs and traces |
|---|---|
| `ue_traces_4ue.conf` | 4 UEs on SNR random walks |
| `ue_traces_cqi_4ue.conf` | 4 UEs on CQI 4..15 walks (the EdgeRIC-RT and PPO scenario, §13) |
| `ue_traces_cqi_4ue_mob.conf` | 2 cars + 2 drones (CQI) |
| `ue_traces_cqi_s1.conf`, `_s2`, `_s3` | 2 UEs: walks; good vs bad; iid (CQI) |
| `ue_traces_cal.conf` | calibration: UE 1 sweeps 0 → 24 dB ([docs/edgeric-rt.md](docs/edgeric-rt.md#channel-traces-reported-cqi-415)) |

Walkthrough: [`docs/run_system.md`](docs/run_system.md) (§4).

---

## Reference

### Repo layout

| Path | What |
|---|---|
| `ocudu-jbpf/` (submodule) | jbpf-enabled OCUDU gNB source, with the control and EdgeRIC-RT hooks, + `build.sh` + `deploy/` (gNB configs, ephemeral-container patch) |
| `duranta-oai-ue/` | Duranta OAI `nr-UE` image + ZMQ configs + netns + brokers (C++ `broker_cpp/` and GNU Radio), channel and trace tables (`ue_channel*.conf`, `ue_traces*.conf`), `traces/` (trace library, generator, CQI calibration) |
| `duranta-oai-ue/duranta-ue/` (submodule) | Duranta OAI `nr-UE` source: ZMQ radio with a per-UE channel model and trace replay |
| `broker/` | broker container image |
| `open5gs/` | Open5GS 5G core Helm chart |
| `jrtc-apps/` | jrt-controller, codelets (`codelets/`), xApps (`jrtc_apps/`), the RAN Helm chart (`containers/Helm/`), `jbpf_protobuf/` (submodule) |
| `edgeric-rt/` | EdgeRIC-RT muApp host image (pod `edgeric-0`), muApps (`muapps/`), library, trained PPO models (`models/`), tools; runs go to `runs/` (gitignored) |
| `telemetry/` | VictoriaMetrics + Grafana stack, dashboard JSON, the `jrtc-ctl` forwarder (`fwd30450.py`), TCP and UDP probes |
| `buffer-control-experiments/` | buffer-cap scripts, traffic and dashboards (§12) |
| `scripts/` | bring-up, traffic, channels, EdgeRIC-RT, dashboards and the demos (below) |
| `docs/` | guides (below) |

### Codelets & xApps (`jrtc-apps/`)

Organized like [`microsoft/jrtc-apps`](https://github.com/microsoft/jrtc-apps) — **all upstream codelets**
plus this testbed's additions:

- **Upstream telemetry** — `mac`, `rlc`, `pdcp`, `rrc`, `ngap`, `ue_contexts`, `slice_mgmt`,
  `fapi_dl_conf`/`fapi_ul_conf`/`fapi_ul_crc`/`fapi_rach`, `xran_packets`, `perf`.
- **Per-packet telemetry** — `upt` (GTP arrival / RLC queuing latency / RLC buffer occupancy,
  bucketed for the user-plane dashboard) + `jrtc_apps/upt` and the **dashboard** xApp
  `jrtc_apps/dashboard` (per-layer telemetry **+ TCP state** via `telemetry/tcp_probe.py` on the UPF).
- **Control hooks** (writable, actuate the RAN — see §10):
  - `edgeric` — DL **scheduling** (`dl_sched`, `mac_sched_dl_ctrl`, PRB-share) + DL **MCS**
    (`dl_mcs`, `mac_sched_dl_mcs_ctrl`).
  - `bufctl` — dynamic **RLC buffer sizing** (`rlc_dl_ctrl`), with the `jrtc_apps/bufctl` xApp + CLI;
    `bufcap` — fixed caps on the same hook (§12).
  - `l4span` — **L4S** ECN marking (`pdcp_dl_sdu_segment`).
  - `codel` — CoDel AQM.
- **EdgeRIC-RT** (§13) — `edgeric_rt`: `rt_report` on `mac_sched_slot_report` sends each TTI's state out,
  and `rt_ctrl` on `mac_sched_slot_ctrl` applies the muApp's weights in the TTI they target. The bridge
  xApp `jrtc_apps/edgeric_rt` (`rt_bridge_app.py`) relays both to the muApps in `edgeric-0`.
- **Real-time scheduling dashboard** — `dashboard_realtime_scheduling`: `rts_stats` on
  `mac_sched_slot_report` buckets every slot per UE, and the xApp of the same name writes it to
  VictoriaMetrics.
- **Example** — `dl_sdu_count`, telemetry on `rlc_dl_new_sdu` with the `jrtc_apps/dl_sdu_count` xApp.
  The website's [Hooks, Codelets and muApps](https://edgeric.github.io/open-ai-ran-tutorial/hooks-and-codelets.html)
  page walks through it, the `bufcap` control codelet and writing a scheduler muApp.

The control hooks, including EdgeRIC-RT's two slot hooks, are added to the OCUDU RAN (`ocudu-jbpf`
submodule); the telemetry hooks are the upstream jbpf set, likewise ported.

### Hooks used here

| Hook | Layer | Type | Used by |
|---|---|---|---|
| `report_stats` | periodic | monitor | `mac_stats_collect`, `rlc_collect` |
| `mac_sched_crc_indication`, `_ul_bsr_`, `_ul_phr_`, `_uci_`, `_harq_dl`, `_harq_ul` | MAC | monitor | `mac_stats` collectors |
| `pdcp_dl_new_sdu` | PDCP | monitor | `gtp_arrival` (`upt`) |
| `rlc_dl_new_sdu`, `rlc_dl_sdu_send_started`, `rlc_dl_sdu_send_completed` | RLC | monitor | `upt` codelets |
| `mac_sched_slot_report` | MAC | monitor | `rt_report`, `rts_stats` (§13) |
| `mac_sched_slot_ctrl` | MAC | control | `rt_ctrl` (§13) |
| `rlc_dl_ctrl` | RLC | control | `bufctl`, `bufcap` (§10a, §12) |
| `mac_sched_dl_ctrl` | MAC | control | `dl_sched` (§10b) |
| `mac_sched_dl_mcs_ctrl` | MAC | control | `dl_mcs` (§10c) |
| `pdcp_dl_sdu_segment` | PDCP | control | `l4span_mark` (§10d) |

Every hook's definition: `ocudu-jbpf/srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h`.

### Scripts (`scripts/`)

Run them from the repo root; each derives its own paths.

| Script | What |
|---|---|
| **Bring-up and checks** | |
| `setup_zmq_chan_demo.sh N [options]` | N-UE bring-up, broker → gNB → UEs (§6) |
| `setup_ota.sh [options]` | over-the-air bring-up: a USRP and COTS UEs ([`run_ota_cots.md`](docs/run_ota_cots.md)) |
| `gnb_console.sh [LINES]` | the gNB console: one metrics row per UE every second, without the jbpf agent's lines |
| `zmq_rt_check.sh [seconds]` | is the chain in real time: % of real time, lateness, CPU, each UE's channel |
| `zmq_tune.sh apply\|show` | SCHED_FIFO for the gNB and broker, one NUMA node (the bring-up applies it) |
| `core_ue_info.sh [--tcp-map]` | each UE's IMSI, IP and NGAP IDs from the core, for the dashboard xApps |
| `stop_demo.sh` | stop traffic, probes, UEs, gNB and broker (§11) |
| **Traffic** | |
| `traffic_nue.sh start\|status\|stop` | DL traffic per UE for the N-UE bring-up (§7) |
| `udp_sink.pl` | the plain-UDP receiver behind `traffic_nue.sh --plain` |
| **Channels** | |
| `trace_ctl.sh list\|status\|set\|stop\|apply\|check` | switch the UEs' traces while they run (§14) |
| **Telemetry** | |
| `tcp_probe.sh` | the TCP row of the user-plane dashboard (§9) |
| `dashboard_realtime_scheduling.sh load\|unload\|status\|grafana\|build` | the per-TTI scheduling dashboard (§13) |
| **EdgeRIC-RT** (§13) | |
| `edgeric_rt.sh load\|unload\|status\|build` | the per-TTI loop: `edgeric_rt` codelets + `rt_bridge` xApp |
| `edgeric_muapp.sh start\|ctl\|logs\|stop\|status\|run` | the muApps in `edgeric-0` |
| `edgeric_eval.sh SCHED ...` | compare schedulers on the same channel |
| `ue_rnti_map.sh [N]` | the `ue<n>` → C-RNTI map the muApps use |
| **Buffer control** (§12; the rest is in `buffer-control-experiments/`) | |
| `traffic_2ue.sh start\|status\|stop` | one DL flow per UE, each with its own congestion control |

### Docs

| Doc | What |
|---|---|
| [Open AI-RAN tutorial](https://edgeric.github.io/open-ai-ran-tutorial.html) | the step-by-step tutorial, with demos |
| [`docs/run_system.md`](docs/run_system.md) | the N-UE system with a channel per UE, step by step |
| [`docs/run_ota_cots.md`](docs/run_ota_cots.md) | the same gNB over the air: a USRP and COTS phones or modems |
| [`docs/edgeric-rt.md`](docs/edgeric-rt.md) | EdgeRIC-RT: the loop, muApps, PPO training, dashboard, results |
| [`docs/control-codelets.md`](docs/control-codelets.md) | the control hooks: contexts and conflicts |
| [`docs/codelet-deployment.md`](docs/codelet-deployment.md) | why the `jrtc-ctl` forwarder exists; debugging load/unload failures |
| [`docs/rlc-buffer-control-hook.md`](docs/rlc-buffer-control-hook.md) | RLC DL buffer-size control hook (`rlc_dl_ctrl`): API spec + xApp developer guide |

---

## License

This repository's own code and docs are under the [MIT License](LICENSE). Bundled and
submodule code keeps its own license: `jrtc-apps/` (MIT, Microsoft) and the `srsRAN_Project`
(AGPL-3.0) and `mbedtls` trees inside it, the Helm charts under `open5gs/`, and the submodules
[`open-ai-ran-ocudu-jbpf`](https://github.com/ucsdwcsng/open-ai-ran-ocudu-jbpf) (OCUDU),
[`open-ai-ran-duranta-ue`](https://github.com/ucsdwcsng/open-ai-ran-duranta-ue) (OpenAirInterface)
and [`jbpf-protobuf`](https://github.com/microsoft/jbpf-protobuf) (MIT). See the license file in each.
