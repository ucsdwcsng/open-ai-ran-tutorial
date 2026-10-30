# Scout — Telemetry Backend & Grafana Dashboard

How the codelet output reaches Grafana, what each panel queries, how to run it,
and how to view it from a laptop.

Companion doc: [`scout-telemetry-doc.md`](./scout-telemetry-doc.md) (codelets + srsRAN provenance).

---

## 1. Pipeline

```
 gNB (srsRAN + jbpf)
   │  hooks: pdcp_dl_new_sdu, rlc_dl_new_sdu,
   │         rlc_dl_sdu_send_started, rlc_dl_sdu_send_completed
   ▼
 upt codelets  ── bucket to 134.217728 ms, 1 msg/bucket/stream
   │            (rlc_pkt_record instead batches 64 raw records — §8 of the codelet doc)
   │  jbpf ringbuf  →  shared-memory IPC   (jbpf_enable_ipc: 1)
   ▼
 jrt-controller (jrtc-0 -c jrtc)
   │  4 streams
   ▼
 upt_app.py  ── converts a bucket to a sample, InfluxDB line protocol
   │  HTTP POST /write
   ▼
 VictoriaMetrics  (ran/victoria-metrics, NodePort 30491)
   │  PromQL (Prometheus-compatible API)
   ▼
 Grafana  (ran/grafana, NodePort 30490)
```

Both backends sit on NodePorts inside **30400–30500**, which the k3d cluster
already publishes to the host, so no extra port mapping is needed.

VictoriaMetrics is used (rather than Prometheus) because the data is **pushed**
at sub-second granularity. Prometheus is pull-based with a ≥1 s scrape floor,
which would destroy the 134 ms resolution. VM ingests InfluxDB line protocol
directly and is queried with PromQL, so Grafana just uses a Prometheus
datasource.

### Metric naming

VM maps line protocol `measurement,tags field=value` to metric
**`{measurement}_{field}`**. So `upt_rlc_buffer,ue=0 bytes_avg=…` becomes
`upt_rlc_buffer_bytes_avg{ue="0"}`.

| Measurement | Fields → metrics | Tags |
|---|---|---|
| `upt_gtp_arrival` | `mbps`, `pps`, `bytes`, `pkts` | `ue`, `bearer` |
| `upt_rlc_queue` | `latency_ms`, `latency_min_ms`, `latency_max_ms`, `sdus`, `retx` | `ue`, `bearer` |
| `upt_rlc_buffer` | `bytes`, `bytes_max`, `bytes_avg`, `pkts`, `pkts_max`, `enq_pkts`, `deq_pkts` | `ue`, `bearer` |
| `upt_rlc_pkt` | `lat_p50_ms`, `lat_p95_ms`, `lat_p99_ms`, `lat_max_ms`, `pkts_total` | `ue` |
| `upt_ctrl` | `byte_limit`, `step` | `src` |

**23 series names total** (4 + 5 + 7 + 5 + 2). `bearer` is `DRB1` / `SRB1`; `ue`
is the index described in §4.4 of the codelet doc (**note the du/cu caveat**).
`upt_rlc_pkt` carries no `bearer` tag — the percentile window is per UE only.

> ⚠️ **`upt_ctrl` is stale and publishes a fiction.** `upt_app.py`'s `_sweep_tick()`
> recomputes an open-loop sweep schedule from wall-clock and publishes it as
> `upt_ctrl_byte_limit`, but the codelet it was mirroring
> (`codelets/upt/rlc_ctrl.cpp`, which had a matching built-in sweep) **no longer
> exists** — buffer control moved to the command-driven `bufctl` codeletset, which
> has no sweep. The `SWEEP_SCHED` / `SWEEP_STEP_SHIFT` "MUST match" comment at
> `upt_app.py:63` points at a deleted file. Ignore `upt_ctrl` (or delete the
> observer) until it is re-pointed at the real setpoint.

### Timestamps

`upt_app.py` stamps each sample at the **bucket close time**:

```python
ts_ns = int((data.bucket_id + 1) * UPT_BUCKET_NS)   # UPT_BUCKET_SHIFT = 27
```

`bucket_id` is `jbpf_time_get_ns() >> 27` from the codelet, and `jbpf_time_get_ns()`
is epoch-based, so this is absolute epoch-ns — it keeps the RAN series time-aligned with TCP series captured
separately (e.g. tcpdump) with no clock translation.

> `UPT_BUCKET_SHIFT` is duplicated in `codelets/upt/upt_helpers.h` and
> `jrtc_apps/upt/upt_app.py`. **Change both or every rate and timestamp is
> silently wrong by a power of two.**

---

## 2. Files

| File | Role |
|---|---|
| `telemetry/telemetry-stack.yaml` | VictoriaMetrics + Grafana Deployments/Services, datasource + dashboard provisioning |
| `telemetry/upt-dashboard.json` | the dashboard (provisioned via ConfigMap) |
| `jrtc-apps/jrtc_apps/upt/upt_app.py` | codelet streams → line protocol |
| `jrtc-apps/jrtc_apps/upt/deployment_fixed.yaml` | jrtc deployment (app + codeletset) |

---

## 3. Deploy

```bash
export CLUSTER=${CLUSTER:-janus-cluster}      # override if your cluster differs
export KUBECONFIG="$(k3d kubeconfig write "$CLUSTER")"

# dashboard first (the Grafana pod mounts this ConfigMap)
kubectl create configmap grafana-upt-dashboard -n ran \
  --from-file=upt-dashboard.json=telemetry/upt-dashboard.json \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f telemetry/telemetry-stack.yaml
kubectl rollout status deployment/grafana -n ran --timeout=120s
```

After editing either the dashboard or the datasource, recreate the ConfigMap and
`kubectl rollout restart deployment/grafana -n ran`.

Load the codelets + app (requires the `jrtc-ctl` forwarder — `fwd.py` — from
README §6b):

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c '
export JBPF_CODELETS=/codelets && export JRTC_APPS=/apps &&
/jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_fixed.yaml --log-level info'
```

Then generate DL traffic (README §7, `iperf3 -R`).

---

## 4. Dashboard

**Title:** *RAN User-Plane Telemetry (per-packet)* · **uid:** `upt-userplane`
URL: `/d/upt-userplane`

Top row = the three RAN panels (from the jbpf codelets). Middle row = the three
TCP panels (from tcpdump on the UPF) — see §8. Both rows share the same
134.217728 ms bucket grid and the same absolute epoch-ns timestamps, so they
line up sample-for-sample. A final panel holds the buffer-size characterization
sweep.

All panels are filtered by **both** template variables, `$ue` and `$bearer`:

| Panel | PromQL | Unit |
|---|---|---|
| GTP Arrival Rate | `upt_gtp_arrival_mbps{ue=~"$ue",bearer=~"$bearer"}`<br>`max_over_time(upt_gtp_arrival_mbps{…}[10s])` | Mbits |
| RLC Queuing Latency — mean (solid) vs p95 (dashed), per UE | `upt_rlc_queue_latency_ms{ue=~"$ue",bearer=~"$bearer"}` (mean)<br>`upt_rlc_pkt_lat_p95_ms{ue=~"$ue"}` (p95, dashed) | ms |
| RLC Buffer Occupancy — avg (solid) vs max (dashed) | `upt_rlc_buffer_bytes_avg{…}`<br>`upt_rlc_buffer_bytes_max{…}` | bytes |
| TCP Throughput | `tcp_throughput_mbps{ue=~"$ue"}`<br>`max_over_time(…[10s])` | Mbits |
| TCP RTT — mean (solid) vs max (dashed) | `tcp_rtt_ms{ue=~"$ue"}`<br>`tcp_rtt_max_ms{ue=~"$ue"}` | ms |
| TCP Bytes in Flight | `tcp_bytes_in_flight{ue=~"$ue"}`<br>`tcp_bytes_in_flight_max{ue=~"$ue"}` | bytes |
| Buffer-size characterization — setpoint vs latency / throughput | `last_over_time(bufsize_char_*{setpoint=~".+SDU",ue="0"}[6h])` | mixed |

> The latency panel's dashed series is the **per-packet p95** from
> `rlc_pkt_record` (codelet doc §8), not `upt_rlc_queue_latency_max_ms`. The
> per-bucket mean hides the tail that buffer sizing is meant to expose, so the
> p95 is the series to read. See [`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md)
> open item 18.

> The `bufsize_char_*` series are **not** written by `upt_app.py` or `tcp_probe.py`
> — they come from the buffer-size characterization campaign
> (`buffer-control-experiments/`). That panel is empty unless a campaign has run.

Legend format `UE {{ue}} {{bearer}}` → one series per UE per bearer.
Template variables: `$ue` = `label_values(upt_rlc_queue_latency_ms, ue)`,
`$bearer` = `label_values(upt_rlc_buffer_bytes, bearer)`. Both multi-select,
both default to **All** (so SRB series show up alongside DRB ones).

Refresh **5 s**, default window **last 5 minutes**.

> The Deployment sets `GF_DASHBOARDS_MIN_REFRESH_INTERVAL=500ms`, because Grafana
> refuses any dashboard refresh faster than `min_refresh_interval` (default 5 s)
> and silently falls back. The dashboard JSON currently asks for `"refresh": "5s"`
> anyway, so that override buys nothing as shipped — it is there so you *can* set
> `500ms` without editing the Deployment.

Data resolution is 134 ms, so even a 500 ms refresh draws every ~3rd–4th bucket as
it lands. To go finer, lower `UPT_BUCKET_SHIFT` (26 → 67 ms, 25 → 34 ms) — in
**both** files, and `rm -f upt/*.o` before rebuilding.

### The intended correlation (deck slide 64)

| RAN panel (top) | TCP panel (bottom) |
|---|---|
| GTP Arrival Rate | TCP Throughput |
| RLC Queuing Latency | TCP RTT |
| RLC Buffer Occupancy | TCP Bytes in Flight (cwnd proxy) |

Each pair should be similarly shaped: arrival drives throughput, queue depth
drives RTT, buffer occupancy tracks in-flight bytes.

---

## 5. Viewing Grafana from a laptop

The testbed runs on the remote host; Grafana is NodePort **30490** there.

```bash
ssh -N -L 3000:localhost:30490 <user>@<testbed-host>
```

Open <http://localhost:3000> → `admin` / `admin` (anonymous viewing enabled).

`ssh -N` prints nothing and appears to hang — **that is what success looks
like**. Leave it running. Use `-f` to background it
(`pkill -f 'ssh -f -N -L 3000'` to stop). Add `-L 8428:localhost:30491` to query
VictoriaMetrics directly from the laptop.

---

## 6. Verifying / troubleshooting

```bash
# metric names present (expect 23 upt_* + 10 tcp_* once tcp_probe runs)
curl -s localhost:30491/api/v1/label/__name__/values

# a value, exactly as the dashboard asks for it (through Grafana's proxy)
curl -s -u admin:admin --get \
  "http://localhost:30490/api/datasources/proxy/uid/victoriametrics/api/v1/query" \
  --data-urlencode "query=upt_rlc_buffer_bytes_avg"

# datasource uid must be 'victoriametrics'
curl -s -u admin:admin http://localhost:30490/api/datasources

# grafana health / dashboard present
curl -s localhost:30490/api/health
curl -s -u admin:admin "http://localhost:30490/api/search?query=User-Plane"
```

| Symptom | Cause / fix |
|---|---|
| **All panels "No data"** but VM has the metrics | Datasource uid mismatch. `uid: victoriametrics` **must** be pinned in the provisioning ConfigMap; otherwise Grafana generates a random uid while the panels reference the fixed one. |
| **"Templating — Failed to upgrade legacy queries"** | Grafana 11 wants the variable query as an object (`{qryType, query, refId}`), not a bare string. |
| Panels empty, VM empty | No traffic, or codelets not loaded — `grep -ac "Registered codelet" /tmp/gnb.stdout` (in container `ocudujbpf`) should be 5 for `upt`. Collectors only emit when there is traffic; **no traffic legitimately means no output**. |
| Refresh stuck at 5 s | Expected as shipped — the JSON asks for `5s`. To go faster, set `"refresh"` in the dashboard JSON *and* keep `GF_DASHBOARDS_MIN_REFRESH_INTERVAL` at or below it. |
| TCP panels empty, RAN panels fine | `tcp_probe.py` not running, or its UE autodetect failed — see §8. |
| `bufsize_char_*` panel empty | Expected unless a characterization campaign has been run. |
| Data stops after a reload of the codeletset | jrtc may have crashed — check `kubectl get pod -n ran jrtc-0` restart count; see the threading trap in the codelet doc. |
| `upt_app` writing but VM empty | Check app logs: `kubectl logs -n ran jrtc-0 -c jrtc | grep upt`. It prints `[upt] VM write failed …` on HTTP errors. |

### Sanity ranges (2 UEs, DL-only iperf3 `-R`, 20 MHz ZMQ)

| Metric | Expected |
|---|---|
| `upt_gtp_arrival_mbps` | ~3–6 sustained, bursts to 30–55 |
| `upt_rlc_queue_latency_ms` | ~4000–6000 (bufferbloat — see below) |
| `upt_rlc_buffer_bytes_avg` | ~2–3 MB |

Multi-second latency is **expected, not a bug**: the gNB's
`rlc_queue_bytes_limit=6172672` (~6 MB) drained at ~3.6 Mbps gives
`2.74 MB × 8 / 3.61 Mbps ≈ 6.1 s`, matching the measured ~5.6 s. Buffer and
latency come from independent codelets on different hooks, so their agreement is
a genuine cross-check.

---

## 7. Retention / footprint

VictoriaMetrics runs with `-retentionPeriod=7d` on an **`emptyDir`** — data is
lost if the pod is rescheduled. Swap in a PVC for anything you need to keep.

Ingest from the RAN side is ~22 **messages**/s (3 bucketed streams × ~7.45
buckets/s); each message carries one entry per (UE, bearer), so with 2 UEs on one
DRB that is ~45 **samples**/s, plus the `rlc_pkt_record` percentile stream. Either
way, negligible. The load is bounded by the bucket width, **not** by traffic rate —
which is the whole point of aggregating in the codelet.

---

## 8. Bottom row — TCP metrics from tcpdump

`telemetry/tcp_probe.py` implements the libpcap method (deck slide 77): it
streams pcap out of the UPF, reconstructs per-UE downlink TCP state, and writes
throughput / RTT / bytes-in-flight to VictoriaMetrics.

### Capture point and why

Capture is on **`ogstun` in the UPF**, which carries the un-tunnelled per-UE IP
traffic (`10.45.0.x`) — the server side of the DL flow. Two alternatives were
rejected:

* **UE side (`oaitun_ue1` inside each `ue<n>` netns)** — wrong for RTT. There you
  see data arrive and the ACK leave, i.e. a local turnaround of microseconds, not
  the round trip through the RAN.
* **gNB N3** — traffic there is GTP-U encapsulated, so it would need an extra
  decapsulation step for no benefit.

**The `open5gs-upf` container cannot capture**: it runs as uid 999 with an empty
effective capability set, so tcpdump fails with *"You don't have permission to
capture on that device (socket: Operation not permitted)"*. Attach a privileged
ephemeral container sharing the UPF network namespace (same trick as the
GNU Radio broker in the gNB pod):

```bash
UPF=$(kubectl get pods -n open5gs --no-headers | awk '/upf/{print $1}' | head -1)
kubectl debug -n open5gs "$UPF" --image=nicolaka/netshoot --profile=sysadmin \
  -c tcpdumper --target=open5gs-upf -- sleep infinity
```

`ogstun` is a TUN device, so the pcap link type is **RAW (101)** — raw IP with
**no** Ethernet header. The parser keys off the link type from the pcap header.

### Run

```bash
# --map is REQUIRED on the current testbed (see the note below)
python3 telemetry/tcp_probe.py --map 10.45.0.4=0,10.45.0.5=1
```

UE mapping: `ue1 -> 0`, `ue2 -> 1`, matching UE start order (and therefore
`du_ue_index`). The UE IP is also exported as the `ip` label so the mapping is
verifiable on the dashboard. Stop with Ctrl-C.

> ⚠️ **Autodetect is broken on the current testbed.** `autodetect_ue_map()`
> (`tcp_probe.py:283`) looks for an interface named **`tun_srsue`** in the `ue1`
> and `ue2` netns, from when the UE was srsUE. The testbed now runs the OAI
> Duranta UE (`nr-uesoftmodem`, 4 UEs), whose interface is **`oaitun_ue1`**, so
> autodetect finds nothing and you must pass `--map`. Read the UE IPs with the
> command in README §6d, or fix the interface name and the `("ue1","ue2")` tuple
> to cover `ue1`…`ue4`.

Other flags: `--iface` (default `ogstun`), `--container` (default `tcpdumper`),
`--vm` (or `$UPT_VM_URL`).

### Computation (per UE, per bucket)

Same 134.217728 ms grid and same absolute epoch-ns bucket-close timestamps as
the RAN codelets — that is what makes the rows align sample-for-sample.

| Metric | How |
|---|---|
| `tcp_throughput_mbps` | bytes newly ACKed in the bucket × 8 / bucket width |
| `tcp_rtt_ms` | for each ACK, `now − send_time` of the newest data segment it covers; mean per bucket (also `_min_ms` / `_max_ms`) |
| `tcp_bytes_in_flight` | `highest_seq_sent − highest_ack_received` (proxy for cwnd) |
| `tcp_retx` | data segments re-covering an already-sent range |

Also written, and useful when a panel looks wrong: `tcp_bytes_acked`,
`tcp_dl_bytes` (every DL data byte seen, ACKed or not), `tcp_bytes_in_flight_max`,
`tcp_rtt_min_ms` and `tcp_rtt_samples` (0 ⇒ the RTT fields are absent for that
bucket, not zero). Ten `tcp_*` series names in all, tagged `ue` and `ip`.

Sequence arithmetic is RFC-1982 serial comparison, so 32-bit wraparound is safe.

### Measured correlation

2 UEs, DL-only `iperf3 -R`, averaged over the run:

| Quantity | UE0 | UE1 |
|---|---|---|
| iperf3 goodput (ground truth) | 3.72 | 2.99 Mbps |
| `tcp_throughput_mbps` (this probe) | **3.60** | **2.88** Mbps |
| `upt_rlc_queue_latency_ms` (RAN) | **2902** | **3458** ms |
| `tcp_rtt_ms` (tcpdump) | **2999** | **3373** ms |
| `upt_rlc_buffer_bytes_avg` (RAN) | 1.16 | 1.16 MB |
| `tcp_bytes_in_flight` (tcpdump) | 1.79 | 1.73 MB |
| `upt_gtp_arrival_mbps` (RAN) | 6.82 | 5.34 Mbps |

Reading this:

* **Throughput validates the probe** — within ~3% of iperf3.
* **RTT ≈ RLC queuing latency** (within 2–3%), from completely independent
  measurement paths: one from srsRAN's in-RLC timestamps via jbpf, the other
  from packet timestamps on the UPF. This is the strongest evidence both are
  correct, and it confirms the RLC queue is the dominant RTT term.
* **In-flight > RLC buffer** (~1.55×) — expected, not an error: in-flight also
  covers bytes sitting in PDCP/GTP-U and on the wire, not just the RLC SDU queue.
* **Arrival > throughput** (~1.9×) — expected: arrival counts every byte pushed
  into PDCP including TCP retransmissions (~780 / ~729 retx in this run), while
  throughput counts only ACKed bytes.

### Caveat on RTT samples

RTT is sampled from the first transmission time of a range. Karn's algorithm
says not to sample RTT for retransmitted segments; this probe keeps the first
send time and does not exclude such samples, so **RTT peaks can be inflated**
where retransmissions are heavy (peaks of 10–13 s were observed against a ~5.6 s
RLC queuing latency). The **means are sound** — those are what the correlation
above rests on. Excluding retransmitted ranges from RTT sampling is the obvious
next refinement.
