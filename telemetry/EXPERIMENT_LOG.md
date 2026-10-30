# CC × RLC Buffer-Size Experiment — Full Log

Everything about the congestion-control vs RLC-buffer study on the Scout 5G
testbed: setup, the two runs, every script, the gotchas, and how to finish.

_Testbed: k3d, namespace `ran`. gNB `srs-gnb-du1-0` (jbpf-instrumented srsRAN) +
Open5GS + 2× srsUE over ZMQ. jrtc controller `jrtc-0`. VictoriaMetrics NodePort
30491, Grafana 30490._

---

## 1. Objective

Measure downlink **throughput** and **latency** for different **congestion-control
algorithms** as a function of the **RLC DL buffer size**, for 2 UEs, and find the
latency/throughput tradeoff (bufferbloat vs under-buffering).

## 2. How the RLC buffer is controlled

A control codelet `rlc_ctrl` on a custom srsRAN hook `rlc_dl_ctrl` (fires per DL
SDU in `rlc_tx_am_entity::handle_sdu`) writes `new_byte_limit` back through
`ctx.data`, and srsRAN applies it to the lock-free SDU queue. The codelet runs an
**open-loop sweep** baked in as a `switch` (NOT a const array — a file-scope
`static const uint32_t[]` lands in `.rodata`, which this jbpf loader doesn't
relocate, so the verifier sees 0 instructions and rejects it).

- File: [`jrtc-apps/codelets/upt/rlc_ctrl.cpp`](../jrtc-apps/codelets/upt/rlc_ctrl.cpp)
- Step width `SWEEP_STEP_SHIFT=37` → 2^37 ns = **137 s/step**, 8 steps (`&7`) →
  ~18 min per full sweep. `jbpf_time_get_ns()` is epoch-ns so the xApp annotates
  the same schedule.
- The xApp [`upt_app.py`](../jrtc-apps/jrtc_apps/upt/upt_app.py) publishes the
  active setpoint as `upt_ctrl_byte_limit` (its `SWEEP_SCHED` **must** match the
  codelet's `sweep_limit()`).

### Rebuild + reload the codelet (docker-mode, `SRS_JBPF_DOCKER=1`)

```bash
# 1. Build the .o inside the SDK image (paths are baked in: /src/out, /nanopb...)
docker run --rm --network=host -v "$PWD/jrtc-apps/codelets":/codelets \
  -e JBPF_CODELETS=/codelets \
  --entrypoint bash ghcr.io/microsoft/jrtc-apps/srs-jbpf-sdk:latest -c '
    cd /codelets/upt
    export CPP_INC=/usr/include/c++/13.2.0 NANO_PB=/nanopb JBPF_OUT_DIR=/src/out \
           SRSRAN_INC_DIR=/src/include SRSRAN_EXTERNAL_DIR=/src/external
    rm -f rlc_ctrl.o && make rlc_ctrl.o'      # verifier runs in-make: expect "505 instructions"

# 2. /codelets is bind-mounted into jrtc-0, so the pod sees the new .o immediately.
#    Reload the whole upt codeletset + app (unload then load):
KC=<good-kubeconfig>          # see §8 kubeconfig gotcha
kubectl --kubeconfig $KC exec jrtc-0 -n ran -c jrtc -- bash -c \
 'export JBPF_CODELETS=/codelets JRTC_APPS=/apps; \
  /jrtc/out/bin/jrtc-ctl unload -c /apps/upt/deployment_fixed.yaml --log-level info'
kubectl --kubeconfig $KC exec jrtc-0 -n ran -c jrtc -- bash -c \
 'export JBPF_CODELETS=/codelets JRTC_APPS=/apps; \
  /jrtc/out/bin/jrtc-ctl load -c /apps/upt/deployment_fixed.yaml --log-level info'
```
Verify: send a short traffic burst, then `upt_ctrl_byte_limit` should show the new
sizes and `upt_rlc_buffer_bytes_max` should clip near the setpoint.
Procedure is also in [`codelet-deployment.md`](../codelet-deployment.md).

## 3. Transport + congestion control

Kernel TCP here has only cubic/reno (no root to `modprobe tcp_bbr`), so we use
**picoquic** (userspace QUIC/UDP) and pick CC with `-G`: **newreno, cubic, bbr,
prague** (Prague substitutes for PCC, not in mainline). The picoquic binary is an
Ubuntu build copied into the gNB pod at `/tmp/picoquicdemo` (+ `/tmp/libcrypto.so.3`),
run with `LD_LIBRARY_PATH=/tmp`. Downloads run inside each UE netns:
```
ip netns exec ue1 env LD_LIBRARY_PATH=/tmp /tmp/picoquicdemo -a perf -G bbr -D \
   -d 1200000 10.45.0.1 4433 "*1:397:200000000000;"
```
- `-a perf` quicperf ALPN; scenario `*<id>:<post>:<response_bytes>;`.
- `-D` = don't write to disk. `-d` = **idle timeout in ms** (NOT a duration cap!).
- `-q <dir>` = qlog (dir must pre-exist). Server side runs quicperf on 10.45.0.1:4433.

## 4. Measurements

| metric | source | transport-agnostic? |
|----|----|----|
| goodput | d(UE `tun_srsue` RX bytes)/dt, sampled 2 s | yes |
| RLC queuing latency | `upt_rlc_queue_latency_ms` codelet | yes (RAN-side proxy for RTT) |
| **QUIC RTT / cwnd / in-flight / loss** | **picoquic qlog (`quic_probe.py`)** | QUIC-native (Run 2) |
| RLC queue occupancy | `upt_rlc_buffer_bytes_max` | yes (is the limit binding?) |

### `quic_probe.py` — the QUIC analog of `tcp_probe.py`

`tcp_probe.py` reconstructs RTT/in-flight passively from a pcap because TCP headers
are in the clear. **QUIC encrypts all of that**, so on-path capture can't see it —
the endpoint is the source. `quic_probe.py` runs a picoquic download with `-q`,
then parses the qlog's `recovery/metrics_updated` events
(`smoothed_rtt, min_rtt, latest_rtt, cwnd, bytes_in_flight, pacing_rate`) and
`packet_lost`, pushing them to VM as `quic_*` tagged by ue+cc.

**Timestamp anchoring (critical):** the qlog `reference_time` is picoquic's
**monotonic** clock, not epoch. So event times are anchored to the client's
**launch wall-clock** (`epoch0_ns`) + the per-event delta, which lines the samples
up with VM/Grafana. picoquic emits only *changed* fields per event, so the parser
carries forward last-known values.

## 5. Run 1 — buffers 100 KB … 6 MB (DONE)

Config: sizes `[100,200,400,700,1200,2000,3500,6172] KB`, CCs newreno/cubic/bbr/prague,
CYCLE 1140 s. Artifacts: [`report.md`](report.md), `plots/*.png`,
`campaign_results.json`. Scripts: [`cc_campaign.py`](cc_campaign.py),
[`plot_campaign.py`](plot_campaign.py).

**Result: completely flat** — all CCs ~6.9 Mbps total, ~29 ms, at every size.
**Why:** measured RLC queue occupancy over the whole run was mean 12.8 KB / p95
25 KB / **max 40 KB** — always below the smallest 100 KB limit. The limit never
bound; the bottleneck is the ~3.4 Mbps/UE ZMQ air interface. **The bufferbloat
knee is below 40 KB.**

## 6. Run 2 — small binding buffers + QUIC probe (IN PROGRESS)

Two changes vs Run 1:
1. Sweep moved **down** to `[5,10,20,40,60,80,150,400] KB` (brackets the 40 KB
   working set). Codelet rebuilt + reloaded (see §2); `SWEEP_SCHED` in the xApp
   updated to match.
2. QUIC endpoint telemetry integrated: [`cc_campaign2.py`](cc_campaign2.py) runs
   the same continuous downloads **with `-q`**, and at each CC's end harvests the
   qlog and pushes `quic_*` via `quic_probe.parse_qlog`.

Plotter: [`plot_campaign2.py`](plot_campaign2.py) → `plots2/` (throughput,
QUIC-RTT, cwnd, loss vs buffer, + RTT/throughput tradeoff) and
`campaign2_results.json`. Manifest: `cc_manifest2.json`.

**Smoke test (cubic, 90 s) already shows the binding regime Run 1 lacked:** at a
20 KB setpoint (queue clipped ~19 KB) cubic ran a **3 KB cwnd**, took **real
losses**, goodput dropped, QUIC smoothed RTT 25 ms / min RTT 12 ms.

Status at last check (19:23): campaign PID 2151517 ALIVE, on **newreno**.
Expected finish **~20:21**. A background waiter (`bjuc8f5jj`) fires on completion;
then run `plot_campaign2.py` and append a Run 2 section to `report.md`.

## 7. Script inventory (`telemetry/`)

| file | role |
|----|----|
| `tcp_probe.py` | passive TCP RTT/throughput/in-flight from a UPF pcap (pre-existing) |
| `quic_probe.py` | **QUIC endpoint** RTT/cwnd/in-flight/loss from picoquic qlog → VM |
| `cc_campaign.py` | Run 1 orchestrator (goodput + RLC latency) |
| `plot_campaign.py` | Run 1 plots + table |
| `cc_campaign2.py` | Run 2 orchestrator (goodput + qlog harvest → `quic_*`) |
| `plot_campaign2.py` | Run 2 plots (throughput/RTT/cwnd/loss) + table |
| `report.md` | results report (Run 1 done; Run 2 to append) |
| `EXPERIMENT_LOG.md` | this file |

## 8. Gotchas / learnings

- **k3d kubeconfig port rotation:** fresh shells get a stale/`/root` kubeconfig →
  `kubectl` "connection refused". Real API port = serverlb `->6443` host port
  (`docker ps | grep serverlb`). Fix: `k3d kubeconfig get janus-cluster > f;
  KUBECONFIG=f kubectl …`. Export the good config before `nohup`-ing campaigns.
- **Verifier `.rodata` trap:** sweep table must be a `switch`, not a const array.
  Changing only the switch's integer returns keeps it at 505 instructions → still
  passes.
- **picoquic `-d` is idle timeout (ms), not a duration cap.** Continuous runs are
  stopped with `pkill`, not `-d`.
- **qlog `reference_time` is monotonic**, not epoch → anchor to launch wall-clock.
- **qlog dir must pre-exist** or picoquic writes nothing.
- Codelets only emit on packets — with no traffic, `upt_*` queries read empty;
  that's "no traffic", not "broken".

## 9. Reproduce

```bash
export KUBECONFIG=<good-kubeconfig>              # §8
# Run 1:
CC_LIST=newreno,cubic,bbr,prague CYCLE_S=1140 python3 telemetry/cc_campaign.py
python3 telemetry/plot_campaign.py
# Run 2 (after codelet rebuilt+reloaded with small buffers, §2):
CC_LIST=newreno,cubic,bbr,prague CYCLE_S=1140 python3 telemetry/cc_campaign2.py
python3 telemetry/plot_campaign2.py
# Standalone QUIC probe:
python3 telemetry/quic_probe.py --ue both --cc bbr --seconds 60
```

## 10. Remaining

- [ ] Run 2 campaign to finish (~20:21), then `plot_campaign2.py`.
- [ ] Append **Run 2** section to `report.md` with the 5 plots + CC comparison.
- [ ] (optional) `git add` the new scripts + codelet change and commit.
