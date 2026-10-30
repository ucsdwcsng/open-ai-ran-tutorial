# Run the 4-UE system: a channel per UE, changed on the fly

Four OAI (Duranta) UEs on one OCUDU jbpf gNB over ZMQ, each in its own network namespace with its
**own channel**. While the system runs, you can change any UE's channel, one UE at a time or
several at once, with no restart and no re-attach:

- to an SNR trace: a random walk, a vehicle, a constant;
- to any SNR you choose;
- back to a clean 30 dB link.

Grafana follows every UE from the core (IMSI, IP) down to its C-RNTI, with per-UE radio, MAC
throughput, RLC and TCP metrics.

- **Topology:** C++ ZMQ broker → gNB → 4 UEs (netns `ue1`..`ue4`), every ZMQ link on `ipc://`.
- **Channels:** each UE starts on a **base channel** (the built-in channel model, step 4). It then
  replays a **trace** (DL and UL SNR over time) that can be switched per UE at any time (step 8).
- **Telemetry (step 7):** a Grafana dashboard with each UE's identity chain, DL CQI, UL SNR and DL
  MAC throughput, RLC queuing and TCP. The dashboard xApp also writes per-layer stats as JSON.
- **Runtime:** ~10 min from a stopped cluster. Steps 1–3 only restore what a cluster stop/start
  wipes; on a running cluster go straight to step 5 (~3–5 min).
- **Channel model and trace replay:** [`duranta-oai-ue/duranta-ue/docs/zmq-channel.md`](../../duranta-oai-ue/duranta-ue/docs/zmq-channel.md)
- **Design notes and measurements:** [`duranta-oai-ue/README.md`](../../duranta-oai-ue/README.md),
  section "N UEs with a per-UE channel"

## Quick start

With the cluster, images and gNB pod in place (steps 1–3), the whole run is:

```bash
export PATH="$HOME/.local/bin:$PATH"; export KUBECONFIG="$(k3d kubeconfig write janus-cluster)"

bash scripts/setup_zmq_chan_demo.sh 4 --traces duranta-oai-ue/ue_traces_4ue.conf --telemetry   # step 5
bash scripts/traffic_nue.sh start                                                             # step 6
MAP=$(bash scripts/core_ue_info.sh --tcp-map)                                                  # step 7b, TCP row
setsid nohup python3 telemetry/tcp_probe.py --map "$MAP" >/tmp/tcp_probe.log 2>&1 </dev/null &
#   Grafana: http://localhost:30490/d/upt-userplane  (admin / admin)

bash scripts/trace_ctl.sh status                 # step 8: what each UE plays now
bash scripts/trace_ctl.sh set 1 trace_2          #   ue1 onto another trace, on the fly
bash scripts/trace_ctl.sh set 3 static_16db      #   ue3 to a constant 16 dB
bash scripts/trace_ctl.sh stop all               #   every UE back to a clean 30 dB channel

bash scripts/traffic_nue.sh stop && bash scripts/stop_demo.sh                                 # step 9
```

Run the bring-up and the probe as separate commands, as here: `stop_demo.sh`, which the setup
calls, kills any process whose command line mentions `tcp_probe.py`, including a shell that runs
both.

## What it uses

| File | Role |
|---|---|
| `scripts/setup_zmq_chan_demo.sh` | N-UE bring-up: stop → provision IMSIs → netns → broker → gNB → UEs, each UE with its own `--zmq.[0].chan`; then checks every UE attached and starts the traces |
| `duranta-oai-ue/ue_channel.conf` | default base channel table: static 30 dB on every UE |
| `duranta-oai-ue/ue_channel_4ue.conf` | a base channel table with four different channels |
| `duranta-oai-ue/ue_traces_4ue.conf` | which trace each UE replays from attach (`--traces`) |
| `duranta-oai-ue/traces/` | the trace library `lib/*.csv`, its generator `gen_traces.py`, the recipes `scenarios.conf`, and `check_parity.py` |
| `scripts/trace_ctl.sh` | changes UEs' channels on the fly: `set`, `stop`, `apply`, `status`, `list` |
| `scripts/traffic_nue.sh` | one downlink iperf3 flow per attached UE; `status` reads the UE-side logs |
| `scripts/zmq_rt_check.sh` | how close to real time the ZMQ chain runs, lateness counts, CPU, each UE's channel |
| `scripts/zmq_tune.sh` | SCHED_FIFO for the gNB and broker, testbed on one NUMA node; the setup applies it |
| `scripts/core_ue_info.sh` | each UE's IMSI, IP and NGAP UE IDs from the core (AMF / SMF logs) → the dashboard xApp; `--tcp-map` prints `tcp_probe.py`'s map. Step 5 runs it |
| `telemetry/fwd30450.py` | forwarder run inside `jrtc-0`; every `jrtc-ctl` load and unload needs it |
| `jrtc-apps/jrtc_apps/upt/` | the `upt` codeletset + xApp: per-UE GTP / RLC metrics → VictoriaMetrics → Grafana |
| `jrtc-apps/jrtc_apps/dashboard/` | the dashboard xApp: per-layer, per-UE stats as JSON in `jrtc-0`'s log, plus DL CQI / UL SNR / DL MAC throughput for Grafana's top row |
| `telemetry/tcp_probe.py` | host-side tcpdump on the UPF: per-UE TCP throughput / RTT for Grafana's bottom row |
| `telemetry/upt-dashboard.json` | the Grafana dashboard (`upt-userplane`), deployed through the `grafana-upt-dashboard` ConfigMap (7e) |

## Prerequisites

- **Host setup.** README sections 1–5 done once on this host (cluster, core, RAN Helm release,
  images).
- **UE image.** `duranta-nr-ue:local` is built from the `duranta-ue` submodule's working tree
  (README §5c, tens of minutes). It must include the channel model and trace replay; the setup
  refuses an image without them:

  ```bash
  docker build -t duranta-nr-ue:local duranta-oai-ue
  ```

- **C++ broker.** The binary is gitignored, so a fresh clone has none:

  ```bash
  bash duranta-oai-ue/broker_cpp/build.sh
  ```

- **gNB image.** `ocudu-gnb-jbpf:ipc` is the gNB built from an `ocudu-jbpf` tree carrying the
  `ipc://` patch to `lib/radio/zmq/radio_config_zmq_validator.cpp`. `ocudu-jbpf/build.sh` always
  tags `:local`, so build this tag directly:

  ```bash
  docker build -t ocudu-gnb-jbpf:ipc -f ocudu-jbpf/docker/Dockerfile ocudu-jbpf
  ```

  Optional: with the stock `:local` gNB everything still runs, but the gNB ↔ broker link falls
  back to tcp (97–99% of real time at 4 UEs instead of 100%).
- **User.** Run everything as your own user from the repo root, **not** with sudo.

## 0. Shell setup

Every new terminal:

```bash
cd <your clone>
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="$(k3d kubeconfig write janus-cluster)"
```

## 1. Start the cluster and load the images

Skip the `cluster start` if `kubectl get pods -n ran` already works.

```bash
k3d cluster start janus-cluster
kubectl get pods -n open5gs; kubectl get pods -n ran      # wait until all are Running
k3d image import duranta-nr-ue:local ocudu-gnb-jbpf:ipc -c janus-cluster
```

Re-run the import whenever you rebuild either image, then redo step 3.

## 2. Fix N4 (SMF → UPF)

Required after every cluster restart: the UPF comes back with a new pod IP, and the SMF reads it
from static config. Skip this and the UEs attach but pass no data.

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
kubectl logs -n open5gs deploy/open5gs-upf --tail=20 | grep -i associat      # expect "PFCP associated"
```

## 3. Fresh gNB pod with the broker, UE and gNB containers

`grbroker`, `durue1` and `ocudujbpf` are ephemeral containers. A cluster restart stops them, and
they cannot be re-added under the same name, so recreate the pod first. The gNB container gets
the `:ipc` image instead of the `:local` one that `ephem_jbpf.json` names.

```bash
kubectl delete pod -n ran srs-gnb-du1-0
until kubectl wait -n ran --for=condition=Ready pod/srs-gnb-du1-0 --timeout=300s 2>/dev/null; do sleep 3; done
kubectl debug -n ran srs-gnb-du1-0 --image=gnuradio-broker:local --image-pull-policy=IfNotPresent -c grbroker --target=gnb -- sleep infinity
kubectl debug -n ran srs-gnb-du1-0 --image=duranta-nr-ue:local --image-pull-policy=IfNotPresent -c durue1 --target=gnb --profile=sysadmin -- sleep infinity
kubectl patch pod -n ran srs-gnb-du1-0 --subresource ephemeralcontainers --type strategic \
  -p "$(sed 's/ocudu-gnb-jbpf:local/ocudu-gnb-jbpf:ipc/' ocudu-jbpf/deploy/ephem_jbpf.json)"
sleep 15
kubectl get pod -n ran srs-gnb-du1-0 -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}={.state}{"\n"}{end}'   # all 3 running
```

## 4. Choose each UE's channel

Two tables decide the channels. Step 5 reads both on every run.

### Base channel: `--channel FILE`

The built-in channel model, one line per UE. Every UE attaches on it, and returns to it with
`trace_ctl.sh stop` (step 8). The default `duranta-oai-ue/ue_channel.conf` puts every UE at a static
30 dB: CQI 15, the best this cell does, so effectively a clean channel.
`duranta-oai-ue/ue_channel_4ue.conf` gives each UE a different one:

```
# ue   dist_m   speed_kmh   min_m   max_m   dl_snr_ref   ul_snr_ref   jitter_db
  1    50       0           40      60      30           30           0
  2    50       0           40      60      16           16           0
  3    50       0           40      60      12           12           0
  4    50       30          40      140     25           25           4
ul_noise_dbfs -65
```

| UE | channel | expected CQI |
|---|---|---|
| ue1 | static, 30 dB | 15 |
| ue2 | static, 16 dB | 13 |
| ue3 | static, 12 dB | 12 |
| ue4 | bounces 40–140 m at 30 km/h: DL 28.6 → 8.2 dB, 4 dB shadowing | 15 ↔ 9 |

Columns:

- starting distance (m);
- speed (km/h, 0 = static);
- bounce range (m);
- DL and UL SNR at the starting distance (dB);
- shadowing (rms dB).

SNR falls by `37.6·log10(d / dist_m)` as the UE moves and is floored at 8 dB DL / 5 dB UL.
`ul_noise_dbfs` is the gNB noise floor, one value for the whole cell. The expected CQIs come from
the measured table in `zmq-channel.md`.

### Traces: `--traces FILE`

`--traces FILE` says which SNR trace each UE replays once all UEs are attached. It also turns on
changing channels on the fly (step 8); without it, each UE keeps its base channel for the whole
run. `duranta-oai-ue/ue_traces_4ue.conf`:

```
# ue   trace
  1    trace_1
  2    trace_1
  3    trace_5
  4    trace_6
ul_noise_dbfs -65
```

A line is `ue trace [once]`:

- The trace is a name from the library below, or the path of a CSV file.
- `once` plays the trace once and then holds its last row; without it the trace loops.
- Several UEs can share a trace. ue1 and ue2 above then see the same SNR at the same instant.
- A UE without a line stays on its base channel until you switch it.

The library, in `duranta-oai-ue/traces/lib/` (`bash scripts/trace_ctl.sh list`):

| traces | what | DL SNR |
|---|---|---|
| `trace_1` … `trace_8` | random walks in a 30–120 m ring around the gNB at 3, 10, 20, 30, 50, 60, 80 and 100 km/h, 4 dB log-normal shadowing | median 15–20 dB, floor 8 dB |
| `static_30db`, `static_16db`, `static_12db` | a constant (one row) | 30 / 16 / 12 dB |
| `ped_3kmh`, `veh_30kmh`, `veh_104kmh` | the model's mobility scenarios as traces (`ue_channel_mobility.conf` ue2 and ue1, `ue_channel_4ue.conf` ue4) | as with `--channel` |

Trace format, how to make new ones, and how the UE replays them:
[Channel traces: reference](#channel-traces-reference).

## 5. Bring up broker → gNB → 4 UEs

```bash
bash scripts/setup_zmq_chan_demo.sh 4 --traces duranta-oai-ue/ue_traces_4ue.conf --telemetry     # ~3–5 min
```

- **`--traces`** gives every UE trace control and starts the table's traces once all UEs are
  attached.
- **`--telemetry`** starts a fresh `jrtc-0` and loads both telemetry xApps (step 7). It does this
  after the gNB is up and before the UEs start, which is what lets the dashboard xApp follow each
  UE from the core down to its C-RNTI.
- **`--channel duranta-oai-ue/ue_channel_4ue.conf`** (optional) gives the UEs four different base
  channels instead of the default 30 dB.

It ends with `SETUP DONE. 4 UE(s) attached.` Before that it prints:

- one line per UE with its IP and its channel at attach (the base channel);
- what the core knows about each UE, which goes to the dashboard xApp;
- the traces starting, on every UE at one instant.

```
################ verify ################
  ue1 ip=10.45.0.37  t 6.0 s, d 50.0 m, DL SNR 30.0 dB (ref -27.3 dBFS), UL SNR 30.0 dB, UL gain 8.4 dB
  ue2 ip=10.45.0.39  t 6.0 s, d 50.0 m, DL SNR 30.0 dB (ref -27.3 dBFS), UL SNR 30.0 dB, UL gain 8.4 dB
  ue3 ip=10.45.0.36  t 6.0 s, d 50.0 m, DL SNR 30.0 dB (ref -27.3 dBFS), UL SNR 30.0 dB, UL gain 8.4 dB
  ue4 ip=10.45.0.38  t 6.0 s, d 50.0 m, DL SNR 30.0 dB (ref -27.3 dBFS), UL SNR 30.0 dB, UL gain 8.4 dB
  UE 1  IMSI 999700000000001  IP 10.45.0.37   RAN_UE_NGAP_ID 0  AMF_UE_NGAP_ID 35
  UE 2  IMSI 999700000000002  IP 10.45.0.39   RAN_UE_NGAP_ID 2  AMF_UE_NGAP_ID 37
  UE 3  IMSI 999700000000003  IP 10.45.0.36   RAN_UE_NGAP_ID 1  AMF_UE_NGAP_ID 36
  UE 4  IMSI 999700000000004  IP 10.45.0.38   RAN_UE_NGAP_ID 3  AMF_UE_NGAP_ID 38
  -> 4 core events to the dashboard xApp (jrtc-0, udp 127.0.0.1:30502)

== traces: duranta-oai-ue/ue_traces_4ue.conf ==
  copied trace_1.csv
  copied trace_5.csv
  copied trace_6.csv
ue1 -> trace_1
ue2 -> trace_1
ue3 -> trace_5
ue4 -> trace_6
  at t 7.0 s: ue1, ue2, ue3, ue4 switch at t 10.0 s
    ue1: trace trace_1 (30000 rows, looping) from t 10.0 s (set at t 8.0 s)
    ue2: trace trace_1 (30000 rows, looping) from t 10.0 s (set at t 8.0 s)
    ue3: trace trace_5 (30000 rows, looping) from t 10.0 s (set at t 8.1 s)
    ue4: trace trace_6 (30000 rows, looping) from t 10.0 s (set at t 8.1 s)
  now:
    ue1: t 11.0 s, trace trace_1 row 99 (T 10.0 s), DL SNR 25.8 dB (ref -27.3 dBFS), UL SNR 25.8 dB, UL gain 4.2 dB
    ue2: t 11.0 s, trace trace_1 row 99 (T 10.0 s), DL SNR 25.8 dB (ref -27.3 dBFS), UL SNR 25.8 dB, UL gain 4.2 dB
    ue3: t 11.0 s, trace trace_5 row 99 (T 10.0 s), DL SNR 19.9 dB (ref -27.3 dBFS), UL SNR 19.9 dB, UL gain -1.7 dB
    ue4: t 11.0 s, trace trace_6 row 99 (T 10.0 s), DL SNR 17.9 dB (ref -27.3 dBFS), UL SNR 17.8 dB, UL gain -3.8 dB

SETUP DONE. 4 UE(s) attached.
```

- **`t`** is the UEs' air time: seconds of samples since the stream started, the same for every UE.
- **IPs and NGAP IDs** follow attach order, so they differ between runs.
- **Re-running.** Every run restarts the broker, gNB and UEs together (ZMQ cannot reconnect
  mid-run). Re-running this step is therefore also how you recover from a partial attach or apply
  an edited table. It clears the previous run's trace control files, and with `--telemetry` it
  starts a fresh `jrtc-0` too, so nothing needs unloading first.

## 6. Traffic and checks

```bash
bash scripts/traffic_nue.sh start      # DL TCP CUBIC, UPF -> every UE, 1 h
bash scripts/traffic_nue.sh status     # per-UE + total goodput at the UE, mean of the last 10 s
bash scripts/trace_ctl.sh status       # each UE's channel now: trace and row, or base; DL / UL SNR
bash scripts/zmq_rt_check.sh           # % of real time, lateness, CPU, each UE's current channel
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- tail -12 /tmp/gnb.stdout      # per-UE CQI / MCS / BLER / DL bit rate
```

`traffic_nue.sh start` also takes `--cc bbr`, `--udp 40M`, `--ue N`, `--time S` and `--rate R`.

What it looked like on 2026-10-06 with CUBIC to all four UEs:

| channel | UE | CQI | DL MCS | goodput at the UE |
|---|---|---|---|---|
| clean: every UE on its base channel, 30 dB (`trace_ctl.sh stop all`) | each | 15 | 27 | 14.6–15.4 Mbit/s, 59.2 total |
| `--channel duranta-oai-ue/ue_channel_4ue.conf`, no traces | ue1, static 30 dB | 15 | 27 | 11.5 Mbit/s |
| | ue2, static 16 dB | 13 | 19 | 9.5 Mbit/s |
| | ue3, static 12 dB | 12 | 12 | 6.4 Mbit/s |
| | ue4, 40–140 m at 30 km/h | 12–14 | 10–13 | 7.2 Mbit/s, 34.6 total |

On the clean channel, Grafana's DL throughput (MAC) read 15.8–15.9 Mbit/s per UE and 63.3 Mbit/s
in total, matching the gNB's own `dl_brate` (16M per UE). The broker logged 23.04 MS/s in both
directions (100% of real time) under this load, and no UE was dropped.

## 7. Telemetry: Grafana and the dashboard xApp

Two independent xApps feed the telemetry; step 5's `--telemetry` loads both into `jrtc-0`:

- **`upt_app`** (`jrtc_apps/upt`): this repo's per-packet user-plane telemetry. Per-UE downlink
  GTP arrival rate, RLC queuing latency and RLC buffer occupancy in ~134 ms buckets, written to
  VictoriaMetrics for Grafana.
- **The dashboard xApp** (`jrtc_apps/dashboard`, the sample from Microsoft's jrtc-apps): per-layer,
  per-UE MAC / RLC / PDCP / RRC / NGAP stats about once a second, as JSON lines in `jrtc-0`'s log.
  It also writes per-UE DL CQI, UL SNR, DL MCS and DL MAC throughput, and each UE's identities
  (7b), to VictoriaMetrics for Grafana.

The Grafana dashboard (`upt-userplane`) has four rows: the UE identity table and Radio / MAC from
the dashboard xApp, RLC / user plane from `upt_app`, and TCP from `tcp_probe.py` (7b). A row stays
empty until its source runs.

### 7a. Forwarder, codelets and xApps by hand

Step 5's `--telemetry` does all of this. By hand, for example to load an edited xApp:

```bash
# jrtc-ctl always connects to 127.0.0.1:30450; the forwarder bridges that to the gNB's jbpf proxy
kubectl cp telemetry/fwd30450.py ran/jrtc-0:/tmp/fwd30450.py -c jrtc
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'grep -qi 76F2 /proc/net/tcp && echo "forwarder already listening" ||
  { nohup setsid python3 -u /tmp/fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown; sleep 2; cat /tmp/fwd30450.log; }'

# the gNB agent loads codelet objects from its own /codelets: upt for upt_app, the other six for the dashboard xApp
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- mkdir -p /codelets
for d in upt ue_contexts mac rlc pdcp rrc ngap; do
  kubectl cp jrtc-apps/codelets/$d ran/srs-gnb-du1-0:/codelets/ -c ocudujbpf
done

# in every terminal that loads or unloads
JRTC() { kubectl exec -n ran jrtc-0 -c jrtc -- bash -c "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl $*"; }
JRTC 'load -c /apps/upt/deployment_demo.yaml'
JRTC 'load -c /apps/dashboard/deployment_ocudu.yaml'
```

`jrtc-0` mounts `jrtc-apps/jrtc_apps` from this repo, so a load always runs the xApp code in your
tree. A dashboard xApp loaded after the UEs attached still reports their stats, but cannot follow
their identities (7b): the gNB reports those only while a UE attaches. To load an edited dashboard
xApp with its identities, re-run step 5 with `--telemetry`.

### 7b. Grafana

```bash
bash scripts/traffic_nue.sh start      # the codelets fire per packet, so no traffic = empty panels
curl -s http://localhost:30491/api/v1/label/__name__/values     # after ~10 s: upt_*, mac_*, ue_ids
```

Open <http://localhost:30490/d/upt-userplane> (`admin` / `admin`). From a laptop, forward the port
first: add 30490 in VS Code's Ports panel, or run `ssh -N -L 30490:localhost:30490 <user>@<testbed-host>`.

Every graph draws one line per UE: the average. The other statistics (p95 / p99 latency,
per-bucket max, min, the buffer-limit setpoint) are rows in the table under each graph, with their
mean and max. DL throughput (MAC) adds a dashed grey line for the cell total. The `bearer`
selector defaults to `DRB1`.

- **UE identity** (table) and **Radio / MAC** (DL CQI, UL SNR, DL throughput) need the dashboard
  xApp.
- **RLC / user plane** (GTP arrival rate, RLC queuing latency, RLC buffer occupancy) needs `upt_app`.
- **TCP** (throughput, RTT, bytes in flight) needs `tcp_probe.py`, below.

**One identity per UE, from the core to its C-RNTI.** "UE n" is the IMSI's subscriber number.
IMSI ...0001 is UE 1, which with the setup's IMSIs is also netns `ue1`, `trace_ctl.sh` UE `1`,
and line 1 of the channel and trace tables. Every panel uses it, with one colour per UE: UE 1
blue, UE 2 orange, UE 3 aqua, UE 4 yellow (then magenta, green, violet, red). The table at the top
shows each UE's whole chain, for example:

| UE | IMSI | IP | AMF UE NGAP ID | RAN UE NGAP ID | CU-CP | CU-UP | DU | PCI | C-RNTI |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 999700000000001 | 10.45.0.18 | 19 | 2 | 3 | 0 | 3 | 1 | 0x4604 |
| 2 | 999700000000002 | 10.45.0.19 | 20 | 3 | 2 | 1 | 2 | 1 | 0x4603 |
| 3 | 999700000000003 | 10.45.0.20 | 17 | 0 | 0 | 3 | 0 | 1 | 0x4601 |
| 4 | 999700000000004 | 10.45.0.21 | 18 | 1 | 1 | 2 | 1 | 1 | 0x4602 |

Each layer numbers UEs on its own, in a different order, so the chain is built from exact ID
matches, with no UE-side data:

- **Core:** the AMF logs each registration's NGAP UE IDs with the UE's IMSI (in the SUCI, null
  scheme here), and the SMF logs IMSI to IP. `scripts/core_ue_info.sh`, run by step 5, reads both
  and sends each UE to the dashboard xApp.
- **gNB:** as each UE attaches, the `ue_contexts` and `ngap` codelets report:
  - the DU index with the C-RNTI;
  - the CU-CP index, joined to the DU on (PCI, C-RNTI), the key the CU-CP itself looks UEs up by;
  - the CU-UP index, joined at E1AP bearer setup;
  - the NGAP UE IDs, at PDU session setup.
- **Join:** the dashboard xApp joins core and gNB on the NGAP UE IDs, which both sides assign
  together, and publishes `ue_ids{n, imsi, ip, amf_ue_ngap_id, ran_ue_ngap_id, cucp, cuup, du,
  pci, rnti}`.

Each panel joins its series to `ue_ids` on the key the series carries: the DU index for MAC and
RLC, the CU-UP index for GTP (PDCP runs in the CU-UP), and the IP for TCP (captured at the UPF).
Without the chain, series keep their native key: `UE du2`, `UE cu3`, `UE 10.45.0.18`.

To read the chain outside Grafana, for example to address a UE in `bufctl_cli.py`, which takes
the DU index:

```bash
curl -s --data-urlencode 'query=last_over_time(ue_ids[10s])' http://localhost:30491/api/v1/query
```

The TCP row needs `tcp_probe.py`. It runs on the host and captures with tcpdump on the UPF's
`ogstun` through a privileged `tcpdumper` container. Its map of UE IPs comes from the core:

```bash
UPFP=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name | grep '^open5gs-upf-')
kubectl get pod -n open5gs "$UPFP" -o jsonpath='{.status.ephemeralContainerStatuses[*].name}' | grep -q tcpdumper ||
  kubectl debug -n open5gs "$UPFP" --image=nicolaka/netshoot --image-pull-policy=IfNotPresent -c tcpdumper --target=open5gs-upf --profile=sysadmin -- sleep infinity
MAP=$(bash scripts/core_ue_info.sh --tcp-map); echo "$MAP"      # 10.45.0.37=1,10.45.0.39=2,...
setsid nohup python3 telemetry/tcp_probe.py --map "$MAP" >/tmp/tcp_probe.log 2>&1 </dev/null &
tail -4 /tmp/tcp_probe.log      # UE map, "writing to http://localhost:30491/write", "capturing on open5gs-upf-..."
```

`--map` is required: on its own, `tcp_probe.py` only looks for srsUE interfaces in `ue1`/`ue2`. The
UE IPs change with every run, so start the probe again after each step 5 (`stop_demo.sh` stops it).

On 2026-10-06, with `ue_channel_4ue.conf` and CUBIC to all four UEs, the TCP row read 11.7 / 9.7
/ 7.3 / 7.1 Mbit/s for UE 1–4 (2-minute averages). The dashboard xApp reported DL CQI 15 / 13 /
12 and DL MCS 26.6 / 18.9 / 12.0 for UE 1–3, with UE 4's rising and falling as it moved. RLC
queuing latency on DRB1 sat at 1.0–1.25 s for every UE: CUBIC fills the deep RLC queue, the
bufferbloat the buffer-control demos address ([demo1](demo1/README.md)).

### 7c. The dashboard xApp

```bash
# jrtc-ctl prints "loaded app" even when the Python app dies on import, so check both ends:
kubectl exec -n ran jrtc-0 -c jrtc -- curl -s http://127.0.0.1:3001/app | grep -oE '"id":[0-9]+'
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -aoE "Registered codelet [a-z_0-9]+ to hook [a-z_0-9]+" /tmp/gnb.stdout | tail
kubectl logs -n ran jrtc-0 -c jrtc --since=5m | grep -E "Failed to import|Python app terminated|\[dashboard\] error"   # must print nothing

# watch it, with traffic running
kubectl logs -n ran jrtc-0 -c jrtc --tail=40 -f | grep -E "MAC_SCHED|RLC_|PDCP_|CRC"
```

Each line is one JSON report (`MAC_SCHED_CRC_STATS`, `RLC_DL_STATS`, `PDCP_DL_STATS`, ...); with
the chain in place each carries the UE's `ue_ctx`, IMSI included. The xApp also sends them to
Azure Log Analytics, but only when `LA_WORKSPACE_ID` and `LA_PRIMARY_KEY` are set.

For Grafana's Radio / MAC row it writes per-UE metrics to VictoriaMetrics:

- `mac_dl_cqi_*` and `mac_ul_snr_*_db` (avg / min / max);
- `mac_dl_tput_mbps`;
- `mac_dl_mcs_*`, which no panel shows.

`vm_url` in `dashboard_params.py` sets the target; an empty string turns it off.

DL CQI comes from the DL HARQ stats, which record the UE's reported CQI as the scheduler used it
for each new transmission. The UCI stats would carry it directly, but they report no CSI on this
gNB.

DL throughput comes from the same stats: the bytes of new DL transport blocks, less those dropped
after the last HARQ retransmission, over the ~1.07 s between two reports. The panel's Total is
`sum(mac_dl_tput_mbps)`.

The UE identities live in the xApp's memory and the gNB reports them only at attach, so reloading
the xApp or restarting `jrtc-0` loses them until the next step 5 with `--telemetry`.

### 7d. Unload

Step 5 with `--telemetry` starts a fresh `jrtc-0` every run, so nothing needs unloading. If you
loaded the xApps by hand (7a), unload them before re-running step 5 without `--telemetry`.
Otherwise the setup restarts the gNB, which drops the codelets, while `jrtc-0` keeps the apps
registered, and a second load fails with `400 Bad Request`.

```bash
JRTC 'unload -c /apps/upt/deployment_demo.yaml'
JRTC 'unload -c /apps/dashboard/deployment_ocudu.yaml'
```

### 7e. Update the dashboard in Grafana

Grafana loads `upt-userplane` from the `upt-dashboard.json` key of the `grafana-upt-dashboard`
ConfigMap. After editing `telemetry/upt-dashboard.json`, replace that key alone:

```bash
python3 -c 'import json; print(json.dumps({"data": {"upt-dashboard.json": open("telemetry/upt-dashboard.json").read()}}))' > /tmp/dash_patch.json
kubectl patch configmap grafana-upt-dashboard -n ran --type merge --patch-file /tmp/dash_patch.json
```

Grafana picks it up within 1–2 min. The ConfigMap holds other files too (campaign results and
scripts), so do not rebuild it with `kubectl create configmap ... | kubectl replace`, which would
delete them.

## 8. Change a UE's channel on the fly

With `--traces` in step 5, `trace_ctl.sh` changes any UE's channel while traffic runs, with no
restart and no re-attach. It takes the UEs (`1`, `1,3`, `2-4` or `all`) and what to play:

```bash
bash scripts/trace_ctl.sh status                          # what every UE plays now
bash scripts/trace_ctl.sh set 2 trace_3                   # ue2 onto the 20 km/h random walk
bash scripts/trace_ctl.sh set 1,3 veh_30kmh               # ue1 and ue3 onto one trace, same instant
bash scripts/trace_ctl.sh set all static_16db             # every UE at a constant 16 dB
bash scripts/trace_ctl.sh stop 4                          # ue4 back to its base channel (clean 30 dB by default)
bash scripts/trace_ctl.sh stop all                        # every UE back to its base channel
bash scripts/trace_ctl.sh apply duranta-oai-ue/ue_traces_4ue.conf   # the step 5 assignment again
```

**Any SNR for one UE.** A CSV with one row is a constant; DL and UL can differ:

```bash
printf 't_s,dl_snr_db,ul_snr_db\n0,20,12\n' > /tmp/dl20_ul12.csv
bash scripts/trace_ctl.sh set 3 /tmp/dl20_ul12.csv        # ue3: DL 20 dB, UL 12 dB, until changed
```

**A profile of your own.** Each row holds until the next, and the trace loops after its last row
plus one step:

```bash
printf 't_s,dl_snr_db,ul_snr_db\n0,25,25\n10,12,10\n20,25,25\n' > /tmp/fade.csv
bash scripts/trace_ctl.sh set 1 /tmp/fade.csv             # ue1: 25 dB, then a 10 s fade to DL 12 / UL 10 dB, every 30 s
```

**A new random walk.** Add a recipe and generate it (all keys: `scenarios.conf`):

```bash
echo "trace_9  walk  seed=9  speed_kmh=40  start_m=70  min_m=30  max_m=120" >> duranta-oai-ue/traces/scenarios.conf
python3 duranta-oai-ue/traces/gen_traces.py --only trace_9
bash scripts/trace_ctl.sh set 2 trace_9
```

Options:

- `--lead S` switches S seconds of air time after the command (default 3).
- `--once` plays the trace once, then holds its last row (default: loop).
- `--no-wait` returns as soon as the UEs answer, without waiting for the switch.
- `--allow-low-snr` accepts traces below 0 dB SNR ([Limits](#limits)).

A switch, as printed:

```
$ bash scripts/trace_ctl.sh set 1,3 veh_30kmh
  copied veh_30kmh.csv
ue1, ue3 -> veh_30kmh (looping)
  at t 331.0 s: ue1, ue3 switch at t 334.0 s
    ue1: trace veh_30kmh (30000 rows, looping) from t 334.0 s (set at t 331.3 s)
    ue3: trace veh_30kmh (30000 rows, looping) from t 334.0 s (set at t 331.3 s)
  now:
    ue1: t 335.0 s, trace veh_30kmh row 99 (T 334.0 s), DL SNR 21.5 dB (ref -27.3 dBFS), UL SNR 21.6 dB, UL gain -0.0 dB
    ue3: t 335.0 s, trace veh_30kmh row 99 (T 334.0 s), DL SNR 21.5 dB (ref -27.3 dBFS), UL SNR 21.6 dB, UL gain -0.1 dB
```

- **Timing.** The switch lands at T, the UEs' air time when you ran the command plus the lead. It
  happens on the same sample in the DL and the UL, and on every UE of that command. Until T each
  UE keeps its current channel, so there is no gap.
- **Answers.** Each UE answers at once, with the switch it scheduled or why it refused one. A UE
  that refuses keeps its channel.
- **Order.** The latest command wins, even over a switch still waiting for its T.
- **Start.** A trace always starts from its first row, so `set` again restarts it.
- **Status lines at a switch.** A status line logged right at T can show the UL switched and the DL
  not yet, because the UE processes its UL a few ms ahead of its DL.
- **Grafana.** The Radio / MAC row (DL CQI, UL SNR, DL throughput) and the TCP row follow within
  a second or two.

## 9. Stop

```bash
bash scripts/traffic_nue.sh stop
bash scripts/stop_demo.sh                                # also stops tcp_probe.py
kubectl exec -n ran srs-gnb-du1-0 -c grbroker -- pkill -9 -x zmq_broker    # stop_demo.sh only kills a python3 broker
```

The xApps stay loaded in `jrtc-0`; the next step 5 with `--telemetry` replaces `jrtc-0`.
`k3d cluster stop janus-cluster` shuts the testbed down. To resume, start again from step 1 and redo
steps 1–3.

## Channel traces: reference

### Trace files

A trace is CSV with the header `t_s,dl_snr_db,ul_snr_db`. Further columns are ignored; the library
files add the distance `d_m`.

- `t_s` starts at 0 and strictly increases.
- Each row holds until the next, with no interpolation.
- The trace loops with a period of its last `t_s` plus its last step; a one-row trace is a
  constant.
- The SNRs apply as given, with no floors.

The library files have one row per 10 ms over 300 s, so a random walk jumps back to its first SNR
every 5 minutes.

`gen_traces.py` writes the library from the recipes in `scenarios.conf`, with fixed seeds, so it
always writes the same files:

- **`walk`** recipes are 2D random walks around the gNB with path loss and log-normal shadowing.
- **`bounce`** recipes run the UE's own channel model. `check_parity.py` checks that copy against
  the UE's C++ code.

```bash
bash scripts/trace_ctl.sh list                       # rows, loop length, DL / UL SNR min, median, max
python3 duranta-oai-ue/traces/gen_traces.py --all    # rebuild the library (--only NAME for one)
python3 duranta-oai-ue/traces/gen_traces.py --check FILE.csv     # what trace_ctl.sh checks before a switch
python3 duranta-oai-ue/traces/check_parity.py        # the generator's copy of the model vs the UE's C++
```

### How the UE replays them

With `--traces`, each UE's `--zmq.[0].chan` carries `trace_ctl=/tmp/traces/ue<n>.ctl`, a control
file in the `durue1` container. `trace_ctl.sh` does the following:

- validates the trace on the host;
- copies it to `durue1:/tmp/traces/<name>.csv` unless it is already there (the file name is the
  trace's name, so two different files of the same name replace each other);
- writes each UE's control file: `trace=`, `start=` (the air time T), `loop=`.

The UE polls its control file every 100 ms on a normal-priority thread, loads the trace, and
switches at T on the same sample in both directions. The UE side:
[`zmq-channel.md`](../../duranta-oai-ue/duranta-ue/docs/zmq-channel.md), "Trace replay".

### What it looked like

On 2026-10-06, with TCP CUBIC to all four UEs:

- **Replay.** Every UE status line matched its CSV row: 639 lines over 4 traces and 5 switches. The
  DL matched to the logged 0.1 dB, and the row was the one `t − T` selects. The UL played the same
  row or the next one.
- **Constant traces** (30 / 16 / 12 / 8 dB): DL CQI 15.0 / 12.9 / 11.6 / 8.7 and DL MCS 26.7 /
  18.8 / 11.2 / 7.0, as in the CQI table in `zmq-channel.md` (15 / 13 / 12 / 9). Grafana's UL SNR
  read 33.0 / 19.9 / 15.9 / 12.0 dB, about 4 dB above the trace's UL SNR on this testbed.
- **Shared trace.** In Grafana's Radio / MAC row, ue1 and ue2 moved together while both played
  `trace_1`: 10-second averages correlated 0.96 (DL CQI) and 0.99 (UL SNR). Against the UEs on
  other traces they correlated 0.34–0.40 and below 0.2.

### Limits

- **`--traces` setup rules.**
  - It needs the C++ broker, which starts every UE on the same gNB sample and so keeps their air
    times equal.
  - One `ul_noise_dbfs` goes to the broker and every UE: the traces table's value, or −65 dBFS.
  - It refuses a broker `--ul-gain-db`, which would shift every UL SNR.
- **SNR below 0 dB.** Traces with SNR below 0 dB are refused (`--allow-low-snr` to override). This UE
  then reports CQI 15 or 0 (`zmq-channel.md`, "Limits"), and a UE the gNB releases never comes back.
- **Fast changes.** The gNB adapts the DL from a CSI report every 20 ms, so faster SNR changes show
  up as block errors rather than as CQI.
- **Clean means 30 dB.** A run-time switch cannot remove the broker's noise floor. A link with no
  added noise at all needs step 5 with `--clean` instead (no channel and no trace control).

## Variants

Each of these replaces the command in step 5:

```bash
bash scripts/setup_zmq_chan_demo.sh 4 --traces duranta-oai-ue/ue_traces_4ue.conf --channel duranta-oai-ue/ue_channel_4ue.conf   # four base channels
bash scripts/setup_zmq_chan_demo.sh 4 --channel duranta-oai-ue/ue_channel_4ue.conf    # model only, no on-the-fly changes
bash scripts/setup_zmq_chan_demo.sh 4                  # every UE static 30 dB (CQI 15), no on-the-fly changes
bash scripts/setup_zmq_chan_demo.sh 4 --clean          # no channel model, no added noise
bash scripts/setup_zmq_chan_demo.sh 2 --channel duranta-oai-ue/ue_channel_mobility.conf   # ocudu-edgeric's 2-UE mobility run
bash scripts/setup_zmq_chan_demo.sh 8 --traces <file>  # any N up to 64; table lines beyond N are ignored
```

- Without `--telemetry` no xApp runs: load them by hand (7a) for the stats, but the UE identity
  chain (7b) needs them loaded before the UEs attach, which only `--telemetry` does.
- `--tcp` puts every ZMQ link on tcp, `--broker py` uses the GNU Radio broker (not with
  `--traces`), and anything after `--` goes to the broker (e.g. `-- --dl-gain-db 0 -6`).
- `--no-tune` skips `zmq_tune.sh`. That script pins the testbed to NUMA node 1 (`16-31,48-63`); on
  a host with a different layout, set `TESTBED_CPUS`.
- 8 UEs ran at 70–72% of real time in the README measurements, because the broker's single thread
  saturates.

## Gotchas

**No comments on table data lines.** In a channel table, columns 9 and 10 are read as `dl_min` /
`ul_min`, so a trailing `# note` reaches the UE as `dl_snr_min=#` and the UE aborts with `bad chan
config`; the setup now refuses it first. Put comments on their own lines.

**A UE without a channel line.** `ue_channel_mobility.conf` defines UEs 1–2 only, so with N=4, UEs 3
and 4 get no channel at all. With `--traces` they get a static 30 dB base channel instead.

**Keep the setup and `tcp_probe.py` in separate commands.** `stop_demo.sh` (step 9, and the setup's
first step) kills every process whose command line mentions `tcp_probe.py`. That includes a shell
running `setup_zmq_chan_demo.sh ...; ... tcp_probe.py ...`. The setup it started then carries on
unattended, and a second setup started on top of it fails.

**gNB rows are per RNTI, not per netns.** `/tmp/gnb.stdout` lists UEs by RNTI in attach order.
Grafana's identity table maps each RNTI to its UE.

**A UE that dies stays gone.** Once all UEs are streaming, the broker drops a UE that stops sending
UL (`[broker] UE<i> ... dropped` in the `grbroker` container's `/tmp/broker.log`) and the rest
carry on. It cannot rejoin; re-run step 5.

**The first ~1.5 s of each UE is calibration.** No noise is applied until the DL reference level
is measured (`calibrating DL reference level` in the UE log).

## Troubleshooting

| Symptom | Fix |
|---|---|
| step 5 prints `this gNB build only takes tcp://` | The gNB container runs `ocudu-gnb-jbpf:local`. Import `:ipc` and redo step 3 (the pod delete is required). |
| `the UE in container 'durue1' has no ZMQ channel support` | `durue1` runs an image without the channel model. Rebuild `duranta-nr-ue:local` (prerequisites), import it, redo step 3. |
| `cannot replay traces (liboai_zmqdevif.so predates trace_ctl)` | The UE image has no trace replay. Rebuild `duranta-nr-ue:local` from the current `duranta-ue` tree, import it (step 1), redo step 3. |
| `ERROR: build it first` | `bash duranta-oai-ue/broker_cpp/build.sh` |
| `ue<n> runs without trace control` | The UEs were brought up without `--traces`. Re-run step 5 with it. |
| a UE answers `... channel unchanged` | It rejected the control or trace file and keeps playing what it played; the message says why. |
| `rows with DL SNR < 0 dB` | The trace goes below 0 dB ([Limits](#limits)). Fix the trace, or pass `--allow-low-snr`. |
| `air time has not reached t ...` | The ZMQ chain stalled or a UE died: `bash scripts/zmq_rt_check.sh`, `bash scripts/trace_ctl.sh status`. |
| `cannot reach pod ran/srs-gnb-du1-0` | Stale `KUBECONFIG`, or running under sudo. Redo step 0 as your own user. |
| `kubectl debug` or the patch fails on a duplicate container name | The pod still carries the old ephemeral containers. Run step 3 from the top; it deletes the pod. |
| `!! 3/4 UEs attached` | Re-run step 5. UE logs: `kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- tail -20 /tmp/ues/ue<i>.log` |
| every step 5 output line appears twice, then `0/4 UEs attached` | Two setups ran at once (see Gotchas, `tcp_probe.py`). Check `pgrep -af setup_zmq_chan_demo` prints nothing, then re-run step 5 once. |
| all UEs attached, no throughput | N4: redo step 2 and look for `PFCP associated`. |
| `connection refused` on `127.0.0.1:30450` during a load | The forwarder is not running (7a). That load was partial (the app may be registered without its codelets), so unload it, then load again. |
| `400 Bad Request` on a load | The app is already loaded, usually from before a re-run of step 5. Unload it (7d), then load again. |
| Grafana panels empty | No downlink traffic (the codelets fire per packet), or `upt` is not loaded: 7b's VictoriaMetrics check lists no `upt_*` names. |
| Grafana's Radio / MAC row empty | The dashboard xApp is not loaded (7c), or it cannot reach VictoriaMetrics: look for `[dashboard] VictoriaMetrics write failed` in the jrtc log. |
| DL throughput (MAC) panel empty, other Radio / MAC panels fine | The dashboard xApp running predates `mac_dl_tput_mbps`: re-run step 5 with `--telemetry` to load the current one. |
| Series named `UE du<n>`, `UE cu<n>` or `UE <IP>`, identity table empty | No identity chain: step 5 ran without `--telemetry`, or the dashboard xApp or `jrtc-0` restarted since. Re-run step 5 with `--telemetry`. |
| Identity table empty right after step 5 | The core events did not arrive: run `bash scripts/core_ue_info.sh` and look for `CORE-AMF-UE` and `[dashboard] error` in the jrtc log. |
| Grafana TCP row empty | `tcp_probe.py` is not running, or its UE map is stale after a re-run of step 5. Check `tail /tmp/tcp_probe.log`, then rebuild the map and restart it (7b). |
| dashboard xApp prints nothing | `Failed to import module` in the jrtc log means a codelet directory was never built (README §5d). |
