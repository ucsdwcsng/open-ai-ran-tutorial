# Demo 2 — two UEs, one buffer knob each

Two OAI UEs attach to the same gNB, each running its own downlink flow with a **different
congestion control** — TCP BBR on UE1, TCP CUBIC on UE2. You then set a
*different* RLC buffer limit on each UE's bearer from a single interactive CLI, and watch the
two bearers diverge: cap one hard and its latency collapses while the other keeps bloating.

That is the point of the demo — buffer control is per-bearer, not per-cell. One UE can be
given a low-latency profile without touching the other.

- **Topology:** 2 UEs, ZMQ broker mode, each UE in its own network namespace (`ue1`, `ue2`).
- **Control:** `jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py` — interactive, per UE and per DRB.
- **Runtime:** ~12 min from a stopped cluster.
- **Single-UE version of this demo:** [`../demo1/README.md`](../demo1/README.md)
- **Background:** [`buffer-control-experiments/EXPERIMENTS.md`](../../../buffer-control-experiments/EXPERIMENTS.md)

## Scripts this demo uses

Both were written for it and live in `scripts/`:

| Script | What it does |
|---|---|
| `scripts/setup_bufctl_2ue_demo.sh` | Full 2-UE bring-up: stop → fresh `jrtc-0` → stage → broker → gNB → `upt` telemetry → 2 UEs, then verifies both attached. Uses `broker2_netns.py` and sets the RLC queue to 8192 SDU / 8 MB so per-UE limits have room to bind. Loads **no** traffic and **no** `bufctl`. |
| `scripts/traffic_2ue.sh` | Independent downlink flow per UE, with **per-UE congestion control** — BBR on UE1, CUBIC on UE2 by default (`--cc1`, `--cc2`, or `--cc` for both). Also `start`/`stop`/`status`, `--ue N`, `--time`, `--rate`. UE1 → port 5201, UE2 → 5202, so the flows never share a server. |

---

## Prerequisites

- Testbed deployed once already (README sections 1–5) and the images built.
- The codelets built: `cd jrtc-apps/codelets && for d in upt bufctl rlc mac pdcp rrc ngap ue_contexts; do ./make.sh -d "$d"; done`
- Nothing else attached to the `rlc_dl_ctrl` hook. Only **one** codelet may hold it, so if
  demo 1's `bufcap` cap is loaded, clear it first: `bash buffer-control-experiments/bufcap.sh off`.
- **TCP BBR loaded in the host kernel.** The k3d nodes share it, and the default traffic uses
  BBR on UE1. Check and load:

  ```bash
  sysctl net.ipv4.tcp_available_congestion_control     # want bbr in the list
  sudo modprobe tcp_bbr                                # if it is missing
  echo tcp_bbr | sudo tee /etc/modules-load.d/tcp_bbr.conf    # persist across reboots
  ```

  On this testbed the module is present but **not loaded by default** — a fresh boot reports
  only `reno cubic`. `traffic_2ue.sh` checks before starting and tells you the exact
  `modprobe` to run rather than letting iperf3 fail obscurely.

## 0. Shell setup

Needed in every new terminal. Note the explicit `KUBECONFIG` export — if your shell has a
stale one (e.g. `/root/.kube/config`) every script here fails with "no UPF pod found".

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
```

## 2. Fix N4 (SMF → UPF)

**Required after every cluster restart** — the UPF returns with a new pod IP and the SMF
learns it from static config, not signalling. Skip it and both UEs attach but pass no data.

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

## 3. Re-add the ephemeral containers

Also wiped by a cluster stop. The broker container matters here — demo 2 needs it, demo 1 does not.

```bash
kubectl debug -n ran srs-gnb-du1-0 --image=gnuradio-broker:local --image-pull-policy=IfNotPresent -c grbroker --target=gnb -- sleep infinity
kubectl debug -n ran srs-gnb-du1-0 --image=duranta-nr-ue:local  --image-pull-policy=IfNotPresent -c durue1  --target=gnb --profile=sysadmin -- sleep infinity
kubectl patch pod -n ran srs-gnb-du1-0 --subresource ephemeralcontainers --type strategic \
  -p "$(cat $REPO/ocudu-jbpf/deploy/ephem_jbpf.json)"
sleep 15
kubectl get pod -n ran srs-gnb-du1-0 -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}={.state}{"\n"}{end}'
```

## 4. Stage the codelets

`bufctl` must be present or the CLI cannot load its codelet.

```bash
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- mkdir -p /codelets
for d in upt bufctl rlc mac pdcp rrc ngap ue_contexts; do
  kubectl cp "$REPO/jrtc-apps/codelets/$d" $P:/codelets/ -c ocudujbpf
done
```

`setup_bufctl_2ue_demo.sh` stages the UE configs, `uecap_ports1.xml`, `netns_setup.sh`, the broker and
the gNB config itself, so nothing else is needed by hand.

## 5. Bring up the two UEs

```bash
bash scripts/setup_bufctl_2ue_demo.sh        # ~5 min
```

Wait for `SETUP 2UE DONE`. It prints each UE's IP, the registered codelets and the DRB queue
config. If one UE fails to attach it dumps both UE logs and exits non-zero — just re-run it.

## 6. Open the dashboard

```bash
python3 buffer-control-experiments/make_dashboard.py     # once per Grafana install
```

On the testbed host: <http://localhost:30490/d/upt-userplane>

Remotely, tunnel first:

```bash
ssh -N -L 3000:localhost:30490 <user>@<testbed-host>
```

then <http://localhost:3000/d/upt-userplane> (`admin`/`admin`).

Use `upt-userplane` rather than `expA-bufcap` for this demo: its series are labelled per UE and
per bearer (`UE {{ue}} {{bearer}}`), which is exactly the comparison you want. Set the `$bearer`
template to `DRB1` to drop the signalling bearers from the plot.

## 7. Start a flow on each UE

```bash
bash scripts/traffic_2ue.sh start        # UE1 = BBR, UE2 = CUBIC (the default)
bash scripts/traffic_2ue.sh status
```

Each UE gets a bulk download, but with a different congestion control: **BBR on UE1, CUBIC on
UE2**. `iperf3 -C` sets the algorithm on whichever side *sends*, and for downlink that is the
UPF — so both `iperf3 -c` processes run there, each with its own `-C`.

Within ~20 s both bearers' buffers fill, but not identically: CUBIC is loss-based and will fill
whatever buffer it is handed, so UE2's occupancy and latency climb until something drops. BBR
paces to its own bandwidth-delay estimate and tends to sit lower. That asymmetry is the setup
for step 8 — the *same* cap affects the two very differently.

Variants:

```bash
bash scripts/traffic_2ue.sh start --cc cubic            # both CUBIC, as a baseline
bash scripts/traffic_2ue.sh start --cc1 cubic --cc2 bbr # swap which UE runs which
bash scripts/traffic_2ue.sh start --ue 1 --rate 2M      # UE1 paced at 2 Mbit/s, like a call
bash scripts/traffic_2ue.sh start --ue 2                # restart UE2's flow only
```

## 8. Per-UE buffer control

Run the CLI **on the host**, not inside a container, and from this same clone:

```bash
python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py
```

It loads the `bufctl` codelet + xApp on start. At the prompt:

```
scan                    # ask UE 0..N / DRB 1..M to report; discovers the live bearers
list                    # the table: UE, DRB, current limit, configured limit, last event
set 0 1 byte 200000     # cap UE 0's DRB1 at 200 kB
set 1 1 byte 2000000    # leave UE 1 loose at 2 MB
list                    # confirm both
reset 0 1               # restore UE 0's configured size
quit                    # unloads the codelet and exits
```

`set <ue> <drb> sdu <n>` sets an SDU count instead of a byte limit.

**The demo, two ways:**

1. *Different policy per user.* With both flows running, cap one UE hard and leave the other
   alone. On `upt-userplane` the capped bearer's `upt_rlc_buffer_bytes_avg` and latency drop
   within a few 134 ms buckets while the other stays bloated. Then swap which one is capped.
   Same cell, same channel, different buffer policy per user.

2. *Same policy, different transport.* Give **both** bearers the identical limit
   (`set 0 1 byte 200000` and `set 1 1 byte 200000`) and compare. The CUBIC flow is the one
   that was relying on the deep buffer, so it loses the most throughput and gains the most
   latency; BBR, already pacing to its own estimate, barely notices. This is the argument for
   why a single cell-wide buffer setting cannot be right for every user.

## 9. Stop

```bash
# in the CLI, before quitting:
#   reset 0 1
#   reset 1 1
#   quit
bash scripts/traffic_2ue.sh stop
bash scripts/stop_demo.sh
```

`k3d cluster stop janus-cluster` to shut the testbed down — reversible, resume from step 1.

---

## Gotchas

**The gNB's UE index is not the netns number.** `bufctl` addresses bearers by the gNB's
0-based UE index (`ue 0`, `ue 1`); the namespaces are `ue1` and `ue2`. Which maps to which
depends on attach order, and the `list` table shows no IP to correlate with. The reliable way
to identify them: set a distinctive limit on UE 0, then see which flow reacts in Grafana or in
`traffic_2ue.sh status`. Do that once at the start of a session and note it.

**Limits survive unloading the codelet.** The gNB keeps the last limit it was given, so
`quit` (which unloads) leaves your caps in force. `reset <ue> <drb>` each bearer *before*
quitting if you want the configured sizes back. Same trap as demo 1's `unload` versus `off`.

**Only one codelet may hold the `rlc_dl_ctrl` hook.** `bufctl` (this demo) and `bufcap`
(demo 1) both want it, so they cannot be loaded together. If a `bufcap` cap is loaded, run
`bash buffer-control-experiments/bufcap.sh off` before starting the CLI.

**The CLI must run from the tree the Helm release points at.** It talks to the xApp by
appending to `commands.jsonl` and reading `state.json` in `jrtc-apps/jrtc_apps/bufctl/`, which
the jrtc pod sees as `/apps/bufctl` via a hostPath pinned to the `REPO_ROOT` used at install
time. Run it from a different clone and the xApp never sees your commands; the CLI detects
this and tells you.

**`scan` defaults to UE 0..3 / DRB 1..2.** Harmless with 2 UEs. Narrow it with
`BUFCTL_SCAN_UES=2` if the table is noisy.

**Use `broker2_netns.py`, not `broker4_netns.py`.** The 4-UE broker's adder blocks waiting on
absent UE3/UE4 streams, so a 2-UE run never syncs. `setup_bufctl_2ue_demo.sh` already picks the right
one — this matters only if you bring things up by hand.

## Troubleshooting

**`no UPF pod found`** — almost always a stale `KUBECONFIG`. Re-run the export in step 0.

**One UE attached, the other did not** — re-run `bash scripts/setup_bufctl_2ue_demo.sh`. ZMQ cannot
reconnect mid-run, so a half-attached state needs a fresh broker and gNB, which the script
does every time. If it fails twice, confirm step 4 staged the codelets and check
`kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- tail -20 /tmp/ue2.log`.

**Both attached, no throughput** — step 2 was skipped, or the UPF has restarted since. Re-run
it and look for `PFCP associated`.

**`bufctl` set commands do nothing** — check the xApp is actually loaded
(`kubectl exec -n ran jrtc-0 -c jrtc -- curl -s http://127.0.0.1:3001/app`) and that the bearer
appeared in `scan`/`list`. A bearer that has never carried a downlink packet will not report,
so start traffic before scanning.

**Dashboard shows one UE only** — the second bearer has not carried a downlink packet yet.
Telemetry is packet-driven; start the flow on that UE and it appears.

**Panels frozen mid-run** — check whether `jrtc-0` restarted and the gNB is holding stale jbpf
IPC channels:

```bash
kubectl get pod -n ran jrtc-0 -o jsonpath='{.status.containerStatuses[*].restartCount}{"\n"}'
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -c "Error reserving buffer" /tmp/gnb.log
```

A non-zero restart count with a climbing error count means reloading xApps will not help — the
gNB has to restart, which `setup_bufctl_2ue_demo.sh` does.
