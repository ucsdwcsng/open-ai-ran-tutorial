# Demo 1 — bufferbloat on one UE, and the buffer-size knob that fixes it

A single OAI UE runs a 2 Mbit/s "video call" and a bulk download at the same time. The
download fills the gNB's downlink RLC buffer, and the call's latency climbs to ~600 ms even
though nothing is lost. Capping that buffer from a jbpf control codelet drops the latency
back to tens of milliseconds while the download keeps most of its throughput.

You watch both effects live on Grafana.

- **Topology:** 1 UE, ZMQ direct mode — no GNU Radio broker, no network namespaces.
- **Runtime:** ~10 min from a stopped cluster, ~5 min if the cluster is already up.
- **Deeper background and measured numbers:** [`buffer-control-experiments/EXPERIMENTS.md`](../../../buffer-control-experiments/EXPERIMENTS.md)
- **Upstream command reference:** [`buffer-control-experiments/HOW_TO_RUN.md`](../../../buffer-control-experiments/HOW_TO_RUN.md)

---

## Prerequisites

- Testbed deployed once already (README sections 1–5): k3d cluster `janus-cluster`, the
  Open5GS core, the RAN pod, and the images `ocudu-gnb-jbpf:local`, `duranta-nr-ue:local`,
  `gnuradio-broker:local`.
- `docker`, `k3d`, `kubectl`, `python3`.
- Nothing else attached to the RLC control hook. If `bufctl_cli.py` is open, type `quit`.

## 0. Shell setup

Every new terminal needs this. All paths below are relative to the repo root, and the
scripts find the repo themselves.

```bash
cd <your clone of open-ai-ran-tutorial>
export REPO=$PWD
export KUBECONFIG="$(k3d kubeconfig write janus-cluster)"
P=ran/srs-gnb-du1-0
```

## 1. Start the cluster

Skip if `kubectl get pods -n ran` already works.

```bash
k3d cluster start janus-cluster
kubectl wait --for=condition=ready pod --all -n open5gs --timeout=300s
kubectl get pods -n ran
```

## 2. Fix N4 (SMF → UPF)

**Required after every cluster restart.** The UPF comes back with a new pod IP, but the SMF
learns it from static config, not signalling. Skip this and the UE attaches but passes no
data.

```bash
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
kubectl logs -n open5gs deploy/open5gs-upf --tail=5 | grep -i associat    # expect "PFCP associated"
```

Use the UPF **pod IP**, not the `open5gs-upf-pfcp` ClusterIP — the ClusterIP path drops
concurrent PFCP session requests through UDP conntrack.

## 3. Re-add the ephemeral containers

Also wiped by a cluster stop. Without them the pod reverts to the stock `srs-jbpf` image and
there is no gNB, UE or broker binary to run.

```bash
kubectl debug -n ran srs-gnb-du1-0 --image=gnuradio-broker:local --image-pull-policy=IfNotPresent -c grbroker --target=gnb -- sleep infinity
kubectl debug -n ran srs-gnb-du1-0 --image=duranta-nr-ue:local  --image-pull-policy=IfNotPresent -c durue1  --target=gnb --profile=sysadmin -- sleep infinity
kubectl patch pod -n ran srs-gnb-du1-0 --subresource ephemeralcontainers --type strategic \
  -p "$(cat $REPO/ocudu-jbpf/deploy/ephem_jbpf.json)"

# UPF tcpdumper — feeds the TCP panels via tcp_probe.py
UPFP=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name | grep '^open5gs-upf-')
kubectl debug -n open5gs "$UPFP" --image=nicolaka/netshoot --image-pull-policy=IfNotPresent -c tcpdumper --target=open5gs-upf --profile=sysadmin -- sleep infinity

sleep 15
kubectl get pod -n ran srs-gnb-du1-0 -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}={.state}{"\n"}{end}'
```

Expect `durue1`, `grbroker` and `ocudujbpf` all `running`.

## 4. Build the buffer-cap codelets

One object per cap size, all sharing codeletset id `bufcap`. Needed once per clone.

```bash
bash buffer-control-experiments/bufcap.sh build        # builds: off 16k 64k 256k 1m
bash buffer-control-experiments/bufcap.sh list
```

Build any other size later with `bufcap.sh build 96k`.

## 5. Stage runtime files into the fresh containers

Must run **after** step 4, since it copies in the objects built there.

```bash
kubectl cp "$REPO/duranta-oai-ue/oaiue_zmq.conf"   $P:/tmp/oaiue_zmq.conf   -c durue1
kubectl cp "$REPO/duranta-oai-ue/uecap_ports1.xml" $P:/tmp/uecap_ports1.xml -c durue1
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- mkdir -p /codelets
for d in upt bufcap bufctl edgeric rlc mac pdcp rrc ngap ue_contexts; do
  kubectl cp "$REPO/jrtc-apps/codelets/$d" $P:/codelets/ -c ocudujbpf 2>/dev/null
done
```

`uecap_ports1.xml` is not optional. Without it OAI cannot resolve its PDSCH MIMO layers and
rejects every downlink allocation — the UE reaches `RRC_CONNECTED`, then registration times
out and the AMF sends an Authentication reject.

## 6. Bring up the gNB and UE

```bash
bash scripts/setup_bufctl_1ue_demo.sh        # ~4-5 min
bash scripts/status.sh
```

Wait for `SETUP DONE. UE 10.45.0.x attached`. This starts a fresh `jrtc-0`, the forwarder, the
gNB, one UE, the `upt` telemetry codeletset and `tcp_probe`, and sets the RLC queue to
8192 SDU / 8 MB so a cap has room to bind.

It deliberately does **not** load `bufctl`: while that dynamic controller is loaded its hook
runs on every downlink packet. This demo uses the fixed caps in step 9 instead.

## 7. Open the dashboard

```bash
python3 buffer-control-experiments/make_dashboard.py     # once per Grafana install
```

Working on the testbed host: <http://localhost:30490/d/expA-bufcap>

Working remotely, tunnel first:

```bash
ssh -N -L 3000:localhost:30490 <user>@<testbed-host>
```

then <http://localhost:3000/d/expA-bufcap> — login `admin` / `admin`.

`ssh -N` prints nothing and looks hung; that is success. Leave it open. Do not leave a
`refresh=` parameter in the URL: it overrides the dashboard's 1 s refresh and makes the panels
jump in large steps. The older per-packet dashboard is still at `/d/upt-userplane`.

## 8. Start the traffic

```bash
bash buffer-control-experiments/traffic_A.sh start
```

A paced 2 Mbit/s video call and a bulk CUBIC download, both downlink on the same UE and DRB,
sent from the UPF. Within ~20 s the call's RTT climbs to ~600 ms — that is the download
filling the buffer.

Variations:

```bash
bash buffer-control-experiments/traffic_A.sh start --cc bbr          # BBR download instead of CUBIC
bash buffer-control-experiments/traffic_A.sh start --video-rate 4M
bash buffer-control-experiments/traffic_A.sh video-only              # or: bulk-only
bash buffer-control-experiments/traffic_A.sh stop
```

## 9. Turn the buffer knob

Leave the traffic running and step through cap sizes, watching the latency and throughput
panels react within a few 134 ms buckets:

```bash
bash buffer-control-experiments/bufcap.sh load 256k    # RTT ~60 ms, download ~30 Mbit/s
bash buffer-control-experiments/bufcap.sh load 128k    # ~40 ms, download slightly lower
bash buffer-control-experiments/bufcap.sh load 64k     # below the BDP: download falls to ~15-20 Mbit/s
bash buffer-control-experiments/bufcap.sh load 16k     # far too small: download collapses
bash buffer-control-experiments/bufcap.sh status
bash buffer-control-experiments/bufcap.sh off          # no cap: the bloat returns
```

Caps swap cleanly — `load` replaces whatever is already in force, so you can flip between
sizes freely. The point of the demo is the middle of that range: latency collapses long before
throughput does.

## 10. Save and stop

```bash
python3 buffer-control-experiments/snapshot.py --minutes 5   # per-phase table + timeline.png
```

```bash
bash buffer-control-experiments/bufcap.sh off        # do this BEFORE stopping traffic
bash buffer-control-experiments/stop_traffic.sh
bash scripts/stop_demo.sh                      # stop gNB + UE
```

To shut the whole testbed down afterwards, `k3d cluster stop janus-cluster`. That is
reversible — everything survives and you resume from step 1. Do **not** use
`k3d cluster delete` unless you intend to rebuild the core from README section 2.

---

## Gotchas

**`off` and `unload` are not the same thing.** The gNB keeps the last limit written to it
after a control codelet is unloaded. `unload` removes the codelet and leaves the cap in
force. `off` loads a codelet that writes the configured limits back, waits for a downlink
packet to carry it, then unloads itself — so **`off` only works while traffic is running**.
Run it before `stop_traffic.sh`, not after.

**Step 4 before step 5.** Staging copies the `bufcap_*.o` objects into the gNB container; if
you have not built them there is nothing to copy and `load` fails to find its serdes.

**This is 1-UE direct mode.** No broker, no netns. Commands do not need `ip netns exec`, and
`broker4_netns.py` / `oaiue{1..4}_netns.conf` are not staged. For the 4-UE variant see
`scripts/bringup_4ue_demo_v2.sh`, which needs all of those plus `netns_setup.sh`.

**Cluster restarts wipe three things,** none of them persistent: the ephemeral containers,
the files staged into them, and the SMF↔UPF N4 association. Steps 2, 3 and 5 exist only to
undo that.

## Troubleshooting

**`UE did not attach`** — re-run `bash scripts/setup_bufctl_1ue_demo.sh`. ZMQ cannot reconnect mid-run, and
the script brings up a fresh gNB each time. If it fails twice, check that step 5 staged
`uecap_ports1.xml`, then read `kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- tail -20 /tmp/ue_direct.log`.

**UE attached but no data** — step 2 was skipped or the UPF restarted since. Re-run it and
confirm `PFCP associated` in the UPF log.

**Grafana panels empty** — nothing is wrong until traffic runs: the telemetry codelets fire on
downlink packets, so the series only appear once step 8 is up. If they stay empty with traffic
flowing, check that metrics are actually landing:

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- \
  curl -s 'http://victoria-metrics.ran.svc.cluster.local:8428/api/v1/label/__name__/values'
```

An empty list means the `upt` codeletset is not reporting; a populated list with blank panels
means the dashboard's time range is wrong, not the pipeline.

**Panels frozen mid-run** — check whether `jrtc-0` restarted:

```bash
kubectl get pod -n ran jrtc-0 -o jsonpath='{.status.containerStatuses[*].restartCount}{"\n"}'
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -c "Error reserving buffer" /tmp/gnb.stdout
```

A restart count above 0 together with a climbing `Error reserving buffer` count means the gNB
is holding stale jbpf IPC channels pointing at a dead controller. Reloading xApps will not fix
it — the gNB has to be restarted, which `scripts/setup_bufctl_1ue_demo.sh` does.
