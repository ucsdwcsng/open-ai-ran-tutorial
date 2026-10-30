# EdgeRIC-RT: a TTI-synchronized scheduling loop over jbpf, and an RL scheduler muApp

EdgeRIC (NSDI'24, Ko et al.) runs AI-in-the-loop RAN control at the TTI timescale. Every TTI the RAN reports its
state, a muApp computes an action for the next TTI, and the RAN applies it in exactly that TTI. This testbed does that
over jbpf and jrtc:

- the OCUDU gNB gets two per-slot hooks;
- two codelets carry state out and actions in;
- a thin jrtc xApp bridges them to muApps in a dedicated container (`edgeric-0`, where PyTorch is available).

It ships classic schedulers and a PPO scheduler trained online against the live gNB.

## How the loop works

```
gNB slot t (cell strand)                            jrtc-0                     edgeric-0 (muApps)
 slot_indication: RANtime_t; [mac_sched_slot_ctrl] ◄─ rt_act_in ◄─ rt_bridge ◄─ UDS ◄─ action(tti=T, seq)
     rt_ctrl ring[T&15]: tti == RANtime ? HIT : NONE
 DL newTx: HIT -> weight-proportional PRBs ; NONE -> stock time_qos
 post_process_results: [mac_sched_slot_report] ─► out_rt_report ─► rt_bridge ─► UDS ─► RICtime logic -> scheduler
```

**RANtime** is the gNB's slot counter. It is kept per cell in `intra_slice_scheduler::slot_indication` and advanced by
the slots elapsed, so a skipped slot still counts. Every report carries it.

**Actions** are tagged with the RANtime of the slot they are for. The muApp targets the first DL slot after the report
(`--lead 1`, the paper's RANtime + 1); the report's `dl_mask` says which upcoming slots carry DL.

The **`rt_ctrl` codelet** drains the action channel at the start of every slot into a 16-entry ring indexed by target
TTI, then applies an action only in the exact TTI it targets (**HIT**). Every other slot runs the stock scheduler,
which is the paper's default policy for a Lazy RIC. Actions that arrive after their slot are counted as `late` and
dropped. For the same TTI, a newer action replaces an older one; this happens while UL slots go by.

**The muApp** follows the paper's RICtime rules:
- It blocks until a report newer than RICtime arrives (**Lazy RAN**: nothing new yet, so wait).
- It reads every queued report, for logs and rewards, but acts only on the newest (**Lazy RIC**: it fell behind).
- RICtime then jumps to the newest report.

The **report of the slot an action was applied in** echoes the action's `seq` with status HIT. That is how the RL
trainer matches each reward to the action that earned it: state, action and reward always belong to one TTI.

**Weights to PRBs.** On a HIT slot the default DRB slice is split as R_i = w_i / Σw · R_total, with largest-remainder
rounding, where R_total is the RBs left for newTx after reTx, SSB, SIB and RAR.
- **Strict** (default, as in the paper): a share a UE cannot use stays idle.
- **`--work-conserving`**: the unused RBs go to the UEs that can use them.

Unlike the stock heuristic, which serves about one full-buffer UE per slot, every weighted UE gets its share in the
same slot. UEs whose share rounds to zero are skipped before grants are built.

## Components

| Piece | Where |
|---|---|
| Hooks, RANtime, EdgeRIC DL path, per-slot report | `ocudu-jbpf`: `lib/scheduler/ue_scheduling/{intra_slice_scheduler.*, edgeric_rt.h}`, `include/srsran/jbpf/jbpf_srsran_contexts.h`, `srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h` |
| Unit tests (weight split, zero-weight UE, strict vs work-conserving, no-action fallback) | `ocudu-jbpf/tests/unittests/scheduler/policy/scheduler_policy_test.cpp` (`scheduler_edgeric_rt_test`) |
| Codelets `rt_report`, `rt_ctrl` and the wire contract | `jrtc-apps/codelets/edgeric_rt/` |
| Bridge xApp and the Python wire format | `jrtc-apps/jrtc_apps/edgeric_rt/{rt_bridge_app.py, rt_wire.py, rt_deployment.yaml, bridge.conf}` |
| muApp host container (pod `edgeric-0`) | `edgeric-rt/Dockerfile`, `jrtc-apps/containers/Helm/templates/edgeric.yaml` (`edgeric:` values) |
| muApps | `edgeric-rt/muapps/{sched_muapp.py, train_ppo.py, eval.py}`, library `edgeric-rt/edgeric_rt/` |
| CQI 4..15 channel traces | `duranta-oai-ue/traces/` (`cqi`/`staircase` recipes, `cqi_calib.csv`, `src_cqi/`), `duranta-oai-ue/ue_traces_cqi_*.conf` |
| Scripts | `scripts/{edgeric_rt.sh, edgeric_muapp.sh, edgeric_eval.sh, ue_rnti_map.sh}`, `setup_zmq_chan_demo.sh --edgeric` |
| Dashboard: codelet `rts_stats`, xApp `dashboard-realtime-scheduling`, Grafana JSON | `jrtc-apps/codelets/dashboard_realtime_scheduling/`, `jrtc-apps/jrtc_apps/dashboard_realtime_scheduling/`, `telemetry/rt-scheduling-dashboard.json`, `scripts/dashboard_realtime_scheduling.sh` |
| Trained PPO models | `edgeric-rt/models/` (`ppo_walk4`, `ppo_walk2`, `ppo_load8844`; see its README) |

## Build (one-time)

```bash
# gNB with the EdgeRIC-RT hooks (image as in README §5b)
( cd "$REPO_ROOT/ocudu-jbpf" && ./build.sh ) && k3d image import ocudu-gnb-jbpf:local -c "$CLUSTER"
# codelets (jbpf SDK image): both must print "Program terminates within N instructions"
bash scripts/edgeric_rt.sh build
# muApp host image, then deploy pod edgeric-0 (README §4 values; an existing release: helm upgrade)
docker build -t edgeric:local edgeric-rt && k3d image import edgeric:local -c "$CLUSTER"
```

`helm upgrade` of an existing release restarts `jrtc-0`: the rendered `hugepages-1Gi` limit changes from `"0"` to
empty. A restarted jrtc loses the gNB's jbpf IPC peer, so deploy `edgeric-0` before a run, not during one, and
bring the radio up afterwards.

## Run

```bash
# 4 UEs with CQI 4..15 channels, fresh jrtc-0, EdgeRIC-RT codelets and bridge loaded once the gNB is up
bash scripts/setup_zmq_chan_demo.sh 4 --edgeric --traces duranta-oai-ue/ue_traces_cqi_4ue.conf
bash scripts/ue_rnti_map.sh 4                 # ue1..ue4 -> C-RNTI (attach order differs from UE numbering)
bash scripts/traffic_nue.sh start --udp 100M  # full buffer: 100 Mbit/s per UE, well above the ~64 Mbit/s cell

# the scheduler muApp: pick the scheduler on the command line
bash scripts/edgeric_muapp.sh start sched --scheduler maxweight
bash scripts/edgeric_muapp.sh logs sched      # one line every 5 s: Mbps per UE, DL HIT %, latency, RB use
bash scripts/edgeric_muapp.sh ctl "scheduler fixed ue1=0.2 ue2=0.2 ue3=0.2 ue4=0.4"   # live switch
bash scripts/edgeric_muapp.sh ctl "scheduler rr"
bash scripts/edgeric_muapp.sh stop sched
```

By default every scheduler gives **one UE the whole slot** (`--alloc single`), as the stock scheduler does with full
buffers. There are two kinds of policy:
- **Metric policies** give the slot to the UE with the highest metric. Ties go to the UE served least recently, so
  equal channels rotate.
- **Share policies** (`fixed`, `rl`) hand out whole slots in proportion to their weights, using smooth weighted round
  robin.

`--alloc shared` splits each slot's RBs by weight among all the weighted UEs. Every OAI UE then decodes a PDSCH every
slot, which slows the ZMQ emulation to about 88% of real time with 4 UEs.

| `--scheduler` | One UE per slot (`--alloc single`, default) |
|---|---|
| `stock` | none: the gNB's own scheduler (time_qos PF) |
| `fixed --weights ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4` | share: 20/20/20/40% of the slots, by UE name (or `0.2,0.2,0.2,0.4` by UE order) |
| `rr` | metric, all equal: the UEs with data in turn |
| `pf` | metric CQI / EWMA(CQI) (the paper's PF); `--pf-mode rate` for served-rate PF |
| `maxcqi` | metric CQI (with `--alloc shared`: RBs proportional to CQI) |
| `bestcqi` | metric CQI, always one UE per slot (the full-buffer throughput optimum) |
| `maxweight` | metric CQI × backlog |
| `rl --model runs/<run>/best.npz` | share: the policy's probabilities as slot shares; `--rl-pick argmax` for its top UE |

Other `sched_muapp.py` options:
- `--lead N`: target the first DL slot ≥ N TTIs ahead. A large N emulates a near-RT RIC: 15 ms is about 30 TTIs.
- `--work-conserving`, `--hold K`, `--min-share`.
- `--duration S`, `--tag NAME`.
- `--cpus`, `--fifo`.

Each run saves every TTI to `edgeric-rt/runs/<tag>/ttis.npz` and a `summary.json`. With `VM_URL` set, which is the
default in the pod, it also pushes 10 Hz aggregates to VictoriaMetrics: measurements `edgeric_rt` and `edgeric_rt_ue`.

## Train the PPO scheduler (full buffer, CQI 4..15 walks)

```bash
bash scripts/edgeric_muapp.sh stop sched      # one reports consumer at a time
bash scripts/edgeric_muapp.sh start train --ues 4 --iters 150 --steps 5000 --tag ppo_walk4
bash scripts/edgeric_muapp.sh logs train
python3 edgeric-rt/tools/plot_training.py edgeric-rt/runs/ppo_walk4 --ref bestcqi=<Mbit/s>   # on the host (matplotlib)
```

**State, action, reward** (paper §6):

| | Definition |
|---|---|
| State | per UE: CQI/15 and log(1+backlog)/log(1+8 MB) |
| Action | `--action single` (default): pick the one UE that gets the slot (categorical over the UEs). `--action shared`: RB shares, softmax of Gaussian logits |
| Reward | DRB bytes scheduled in the applied TTI |
| Network | `--arch set` (default): one 64-64 tanh network scores every UE from its own CQI and backlog plus the mean and max over all UEs; the critic averages per-UE embeddings. `--arch mlp`: one network over the whole state, the original shape |

**Why a network shared across UEs.** A 5000-TTI batch covers about 4 s of channels that change over seconds, so
the best UE is often the same one for a whole batch. The whole-state network learns "pick UE k" from such a batch
and forgets it on the next: in an offline check with 4 UEs it reached 93-100% best-UE picks, then fell back to
32-39%. The shared network scores every UE with the same weights, so it learns "higher CQI, higher score" from any
UE, cannot prefer a position, and runs with any number of UEs. It held 99.4% in the same check.

**PPO settings:** γ 0.9, λ 0.95, clip 0.2, 10 epochs, minibatch 256, Adam 3e-4, entropy 1e-3.

**Each iteration:**
1. Collect 5000 applied TTIs (~3.7 s). Only the actor runs on the per-TTI path (about 70 µs per decision in
   numpy); the critic values and the convergence measures are computed for the whole batch afterwards.
2. Pause about 0.9 s while torch updates. The gNB runs its own scheduler meanwhile: a deliberate Lazy RIC.
3. The muApp drains the backlog and resyncs.

**Convergence** is judged on two channel-independent measures in `train.csv`, since throughput per iteration
swings with the stretch of channel each batch sees: `train_best_pct`, the share of applied actions that gave the slot
to a UE with the highest CQI, and `train_eff`, the CQI spectral efficiency of the choice over that of the best CQI.
`tools/plot_training.py` writes `train_curve.png`/`.svg` and `convergence.json` (the iteration from which the
10-iteration mean of `train_best_pct` stays within 2 points of its final level).

**Output.** Every 5 iterations the mean policy is evaluated. `runs/<tag>/` holds `train.csv`, `last.npz`, `best.npz`
(numpy, used by `--scheduler rl`; a categorical model gives each slot to its top UE) and `ckpt.pt` (`--resume`).

## Compare schedulers on the same channel

```bash
bash scripts/edgeric_eval.sh --duration 60 \
  rl:runs/ppo_4ue_fullbuf/best.npz bestcqi maxcqi maxweight pf rr fixed:0.25,0.25,0.25,0.25 stock
```

Before each run the script re-applies the trace table, so every scheduler sees the same channel from t = 0. It ends
with `muapps/eval.py`'s table:
- throughput per air time, total and per UE;
- Jain fairness;
- DL HIT %;
- report → action latency.

## Dashboard: dashboard-realtime-scheduling

The Grafana dashboard "EdgeRIC real-time scheduling" (http://localhost:30490/d/rt-scheduling) shows what the scheduler
does, per UE, while it runs. It is built like the upt dashboard: a codelet buckets inside the gNB, a jrtc xApp writes to
VictoriaMetrics. Two host-side probes add what the UEs received: `tcp_probe.py` for TCP (tcpdump on the UPF) and
`ue_rx_probe.py` for UDP (the UE receivers' logs).

```bash
bash scripts/dashboard_realtime_scheduling.sh load        # codelet + xApp, UE names, Grafana dashboard, probes
bash scripts/traffic_nue.sh start                          # TCP, or e.g. --udp 8M --plain --ue 1 (one call per UE)
bash scripts/edgeric_muapp.sh start sched --scheduler maxweight
bash scripts/edgeric_muapp.sh ctl "scheduler rl models/ppo_walk4/last.npz"     # switch live and watch
bash scripts/dashboard_realtime_scheduling.sh status      # latest samples, from VictoriaMetrics
bash scripts/dashboard_realtime_scheduling.sh unload
```

**Layout.**
- **Top:** the UE identity table (UE n, IMSI, IP, NGAP UE IDs, DU UE index, C-RNTI). Next to it: the scheduler, the
  share of TTIs the muApp ran (RT-E2 HIT), and the cell's DL throughput from the gNB and from TCP.
- **A scheduler strip:** which scheduler ran when.
- **Per UE (plus the total for throughput):**

  | | |
  |---|---|
  | DL CQI | DL backlog |
  | DL throughput received (TCP: tcpdump on the UPF; UDP: UE receivers) | DL throughput, gNB |

  Both throughput plots also draw a thick mean line in the color of the scheduler that runs. It shows the mean of
  the total under that scheduler since it started (within the time range), from 5 s after the switch. A switch
  therefore shows as a step between two flat lines. A scheduler starts from the queues the previous one left, so its
  mean takes a minute or two to settle. Measured live at 8/8/4/4, gNB throughput:
  - the gNB's own scheduler: 14.0 Mbit/s;
  - max-weight: 15.7 Mbit/s;
  - PPO: 18.4 Mbit/s.

  The UEs received 13.3, 15.0 and 17.7 Mbit/s.

  The "Average over" selector sets a trailing moving average for these four panels: 134 ms (each bucket), 0.5, 1, 5
  (default), 10 or 30 s.
  - At 134 ms, TCP shows bursts above the link rate: ACKs arrive in bunches, and all the bytes they acknowledge land
    in one bucket.
  - Most of the variation is the channel, not the measurement. Each UE's CQI wanders across 4–15 for seconds at a
    time, and with one UE per slot the cell total follows the CQI of whichever UEs the scheduler serves. The emulator
    adds little: its pace varies by about 2% per bucket.
  - Measured under max-weight at 8/8/4/4, the cell total varies by 45% per bucket. Averaging brings that down to 32%
    over 1 s, 18% over 5 s, 12% over 10 s and 4% over 30 s. A longer window reacts that much later to a scheduler
    switch.

**Pieces.**
- **Codelet `rts_stats`** on `mac_sched_slot_report`. Over each 1 << 27 ns bucket (134 ms, the upt grid) it
  aggregates every slot's report per UE:
  - CQI mean, min and max;
  - DL backlog mean and max;
  - DRB bytes, TB bytes and retransmitted bytes;
  - PRBs, slots with a grant, and MCS.

  Per cell it counts DL slots, HIT and HOLD slots, and PRBs. It sends one message per bucket, about 7.5 per second.
  Any codelet on that hook makes the gNB build the report, so it also works without the EdgeRIC-RT loop, under the
  gNB's own scheduler.
- **xApp `dashboard_realtime_scheduling.py`.** It stamps each bucket at its close and writes it as:
  - `rts_ue_*{du, rnti}`;
  - `rts_cell_*`;
  - every 2 s, `rts_ue_ids{n, imsi, ip, amf_ue_ngap_id, ran_ue_ngap_id, du, rnti}`.

  The UE names come from two sources:
  - `ue_map.txt` (`ue_rnti_map.sh`) names each C-RNTI `ue<n>`, and UE n has IMSI ...n;
  - `core_ue_info.sh` sends the core's IMSI, IP and NGAP IDs to UDP 127.0.0.1:30503 in jrtc-0, polled without
    blocking.

  Neither needs the xApp loaded before the UEs attach. It runs one thread and writes twice a second.
- **Throughput received.** Two host-side probes, one per kind of traffic:
  - **TCP:** `telemetry/tcp_probe.py`, with tcpdump in the UPF's `tcpdumper` container. It counts the bytes each UE
    ACKs on its TCP flows, on the same bucket grid. It trails the gNB panels by about 3 s, because the pcap streams
    through `kubectl exec`.
  - **UDP:** `telemetry/ue_rx_probe.py`. The UPF cannot see UDP delivery, because UDP has no ACKs. So this probe
    follows the one-second logs of the receivers `traffic_nue.sh` runs in the UE namespaces (`udp_sink.pl` with
    `--plain`, `iperf3 -s` otherwise), using one `tail -F` through `kubectl exec`. It captures nothing on the UEs.
    The lines are labelled "UE n (UDP)", one sample per second.

**Cross-check** (4 TCP flows, full buffer). Totals over the same windows, in Mbit/s:

| Scheduler | gNB | iperf3 at the UEs |
|---|---|---|
| max-CQI | 28.9 | 27.4 |
| max-weight | 23.4 | 23.1 |
| PPO | 24.2 | 23.3 |
| RR | 18.6 | 17.4 |
| gNB's own | 14.1 | 13.5 |

Against a 30 s pcap taken on the UPF under max-CQI, `tcp_probe`'s per-UE byte counts were within 4% of the ACKs in the pcap. A later 30 s max-CQI window read 29.9 Mbit/s of TCP against 31.5 from the gNB.

`tcp_probe` used to write nothing for a UE in a bucket where it saw none of its packets. Averages and sums over the
grid then skipped those zeros, so bursty schedules read high: max-CQI showed 44 Mbit/s of TCP against 27 at the UEs.
It now writes zeros.

**Cost to the loop.** This xApp is a second Python xApp next to the bridge, and they share one GIL. Every bit of
Python it runs can delay the bridge. So it parses each bucket with one struct unpack (14 µs), and writes over a
kept-alive HTTP connection: 19 µs of CPU per write, against 394 µs with urllib.

A/B with TCP traffic, DL HIT over 40–60 s windows, xApp loaded vs not loaded:

| Scheduler | Loaded | Not loaded |
|---|---|---|
| PPO | 94.7–94.9% (3 runs) | 95.2% (2 runs) |
| max-CQI | 99.81–99.84% | 99.89% |

- PPO loses about 0.4 points, and its p50 latency is unchanged (235 µs).
- The max-CQI runs used an earlier version of the xApp that wrote through urllib.

## Channel traces: reported CQI 4..15

On this AWGN ZMQ channel the OAI UE's reported CQI follows the DL SNR (measured: 0 dB → CQI 4, 8 → 9, 16 → 13,
≥ 22 → 15). The `cqi` recipe mode in `duranta-oai-ue/traces/gen_traces.py` builds a CQI process in [4, 15] and maps it
to DL SNR through `traces/cqi_calib.csv`, holding UL at 20 dB.

Processes:
- `walk`: uniform over 4..15;
- `band`: good 8..15 / bad 4..7;
- `iid`;
- `triangle`;
- `replay`: the EdgeRIC NSDI'24 car / drone / robot / turntable traces in `traces/src_cqi/`, rescaled to 4..15.

| Table | Content |
|---|---|
| `ue_traces_cqi_4ue.conf` | 4 walks (the RL scenario) |
| `ue_traces_cqi_4ue_mob.conf` | 2 car + 2 drone |
| `ue_traces_cqi_s1.conf` | 2 UEs on walks |
| `ue_traces_cqi_s2.conf` | 2 UEs, good vs bad |
| `ue_traces_cqi_s3.conf` | 2 UEs, iid |

Switch the running UEs with `bash scripts/trace_ctl.sh apply <table>`.

**Calibration.** `cqi_calib.csv` is seeded from the measured points. To refine it:
1. `trace_ctl.sh apply duranta-oai-ue/ue_traces_cal.conf` (UE 1 sweeps 0 → 24 dB);
2. record with `--scheduler stock --tag cal`;
3. run `tools/calibrate_cqi.py runs/cal/ttis.npz --rnti <ue1> --calib <path> --write`;
4. `gen_traces.py --all`.

## Bare-loop measurement

`bash scripts/edgeric_rt.sh load --inline fixed:0.2,0.2,0.2,0.4` makes the bridge act by itself on every report,
without a muApp. Its log line (`kubectl exec -n ran jrtc-0 -c jrtc -- tail /tmp/edgeric_rt_bridge.log`) gives:
- the DL HIT % (actions applied in their exact TTI);
- report → action latency inside jrtc.

## Measured (4 UEs, full-buffer UDP, 30 kHz SCS, 0.5 ms slots)

| Path | DL slots with the action applied in its exact TTI | report → action at the bridge |
|---|---|---|
| bridge acting inline (jbpf + jrtc only) | 99.9–100 % | p50 ≈ 51 µs, p99 ≈ 90 µs |
| muApp in `edgeric-0`, classic schedulers (bridge ↔ muApp over Unix sockets) | 99.8–99.9 % | p50 ≈ 110 µs, p99 ≈ 180–210 µs |
| muApp in `edgeric-0`, PPO (shared network) | 98.1–98.5 % | p50 ≈ 250 µs, p99 ≈ 390–430 µs |

The muApp sends its action before it logs the reports it read (logging costs about 50 µs per report). Before that
change the classic schedulers ran at 99.4–99.6 % and p50 ≈ 180 µs.

## Results (60 s per scheduler, same channel, one UE per slot)

gNB throughput per air-second and UDP delivered at the UEs; `edgeric-rt/runs/<prefix>.csv` has the full tables
(per UE, Jain, DL HIT, latency).

**4 UEs, full buffer, CQI 4..15 walks** (`walk4`). PPO: `ppo_walk4/last.npz`, 150 iterations, shared network.

| Scheduler | gNB Mbit/s | UDP Mbit/s |
|---|---|---|
| bestcqi | 41.54 | 37.85 |
| maxcqi | 41.39 | 37.56 |
| maxweight | 40.91 | 37.42 |
| PPO | 40.80 | 36.89 |
| fixed 0.25 x 4 | 19.86 | 18.67 |
| rr | 19.83 | 18.70 |
| pf | 17.33 | 16.01 |
| stock (gNB time_qos) | 15.49 | 14.76 |

**2 UEs, full buffer** (`walk2`, `ppo_walk2`): maxcqi 29.26 / 25.02, maxweight 28.87 / 24.62, bestcqi 28.86 / 24.24,
PPO 28.66 / 23.91, pf 20.19 / 17.05, rr 19.63 / 16.50, fixed 19.47 / 16.25, stock 16.94 / 14.61 (gNB / UDP).

Both PPO runs converged within 12–15 iterations: the share of slots given to a best-CQI UE and the CQI efficiency of
the choice reach their plateau and hold it (`ppo_walk*/convergence.json`, `train_curve.png`). With full buffers the
optimum is "best CQI", and PPO lands within 2% of it.

**4 UEs, unequal finite loads 8/8/4/4 Mbit/s** (`load8844`, `ppo_load8844`, trained with `--gamma 0.98`):

| Scheduler | gNB Mbit/s | UDP Mbit/s | Jain |
|---|---|---|---|
| PPO | 18.42 | 17.50 | 0.93 |
| fixed 1/3, 1/3, 1/6, 1/6 | 16.36 | 15.43 | 0.92 |
| bestcqi | 16.23 | 15.45 | 0.91 |
| maxcqi | 16.14 | 15.40 | 0.92 |
| rr | 15.75 | 14.96 | 0.97 |
| maxweight | 15.73 | 15.00 | 0.71 |
| pf | 15.69 | 14.88 | 0.99 |
| stock | 14.63 | 13.92 | 0.99 |

A repeat in a different order (`load8844b`) gave PPO 18.50, maxweight 16.30, bestcqi 16.09. With one UE per slot and
finite queues, max-CQI leaves about 30% of its slots' PRBs empty (the best UE's queue is short) and max-weight fills
its slots at a median CQI of 8 (it serves the fastest-growing queues in their fades); PPO uses 94% of the PRBs at a
median CQI of 10 and carries 15% more bytes per slot. The cell is overloaded at this point (14-18 of 24 Mbit/s
delivered), where max-weight's stability guarantee says nothing about throughput; below capacity every stable
scheduler delivers the full load, so no scheduler can beat max-weight there.

**Choosing a finite-load point.** `edgeric-rt/tools/load_sim.py` models the cell slot by slot (walk traces, a
measured bytes-per-slot table per CQI, 1,400-byte datagrams, the 6.2 MB RLC queue, each scheduler's rule) and can
train PPO in the model:

```bash
python3 edgeric-rt/tools/load_sim.py --inputs edgeric-rt/runs/loadsim/inputs.json compare --loads 8,8,4,4
python3 edgeric-rt/tools/load_sim.py --inputs edgeric-rt/runs/loadsim/inputs.json train --loads 8,8,4,4 --gamma 0.98
```

**Finite-load runs need plain UDP.** iperf3's TCP control connection shares the UE's DL queue; a UE starved for
seconds loses it and the flow stops. `traffic_nue.sh start --udp 8M --plain --ue 1` uses iperf2 at the UPF and
`scripts/udp_sink.pl` in the UE netns (iperf3-style log lines). `edgeric_eval.sh --loads 8,8,4,4` uses it, and before
every run stops the traffic, drains the queues with bestcqi (`--drain`), and restarts each UE's flow, so no run
inherits another's backlog.

## Known issues

- **PPO decisions are slower than the classic ones** (p50 about 250 µs against 110 µs), so 1.5-1.9% of PPO's DL
  slots miss their TTI and run the stock scheduler.
- **A UE released mid-run.** ue4 (RNTI 0x4604) received an RRCRelease about 20 minutes after attach. The release was
  gNB-initiated (NGAP UE Context Release in the AMF log), and a released ZMQ UE does not come back.
  - Ruled out: the channel was good (CQI 11–15); the CU-CP `inactivity_timer` is 7200 s; and the EdgeRIC path made no
    empty grants in about 1M selected UE-slots.
  - Possibly related: the PPO policy had given ue4 no DL PRBs for several seconds before.
  - The gNB logs at `warning` (`/tmp/gnb.log`), so the cause was not recorded. Raise `log: du_level` / `cu_level` to
    `info` to catch it next time, and re-run `setup_zmq_chan_demo.sh` to get the UE back.
- **Test binary is not persistent.** The gNB binary was copied into the running `ocudujbpf` container for these
  runs, so a pod restart reverts it. Build the image with the hooks (`ocudu-jbpf/build.sh`) and re-patch the
  ephemeral container (README §5b, §6a).

## Notes

- **One report consumer.** The bridge sends reports to a single muApp socket, so stop `sched` before `train` and vice
  versa.
- **Keep jrtc free of heavy Python xApps.** Python xApps in jrtc share one GIL with the bridge. The upt and dashboard
  xApps handle many streams; `setup_zmq_chan_demo.sh --edgeric` without `--telemetry` gives a fresh jrtc-0 with only the
  bridge. The dashboard-realtime-scheduling xApp is light (one bucketed stream); see its A/B above.
- **Old sets.** Unload the older `edgeric_sched` / `edgeric_mcs` sets while `edgeric_rt` is loaded.
  They act on the same scheduler.
