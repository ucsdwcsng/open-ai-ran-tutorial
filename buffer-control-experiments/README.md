# buffer-control-experiments

Hands-on experiments with the RAN control hooks: the RLC downlink buffer cap
(`rlc_dl_ctrl`) and an emulated "bad radio" (`mac_sched_dl_mcs_ctrl`). Students load and unload
codelets on a running gNB and watch the effect in Grafana within seconds.
The plan, the math and all measured results are in [EXPERIMENTS.md](EXPERIMENTS.md).

**Just want to run it? Follow [HOW_TO_RUN.md](HOW_TO_RUN.md)** (step-by-step commands only).

Run everything on the testbed host from the **repo root**. The codelet sources live in
`jrtc-apps/codelets/bufcap/` and `jrtc-apps/codelets/badchan/`; the scripts here build them
(in the jbpf SDK container), write their YAMLs and load them through `jrtc-ctl`.

## Quick start: the interactive demo

```bash
bash scripts/setup_bufctl_1ue_demo.sh                              # fresh gNB + UE + telemetry (~5 min)
python3 buffer-control-experiments/make_dashboard.py         # once per Grafana install
# open http://localhost:30490/d/expA-bufcap   (use a clean URL: a refresh= in it overrides the 1 s refresh)

bash buffer-control-experiments/traffic_A.sh start           # 2 Mbit/s "video call" + CUBIC download
                                                      # (also starts flowmon: per-flow RTT/throughput)
bash buffer-control-experiments/bufcap.sh load 256k          # cap the buffer: the call's RTT falls in seconds
bash buffer-control-experiments/bufcap.sh load 128k
bash buffer-control-experiments/bufcap.sh load 16k           # too small: the download collapses
bash buffer-control-experiments/bufcap.sh build 96k          # make your own size, then load it
bash buffer-control-experiments/bufcap.sh unload             # unload only: the last cap STAYS in the gNB
bash buffer-control-experiments/bufcap.sh off                # restore the configured buffer (needs traffic)

python3 buffer-control-experiments/snapshot.py --minutes 5   # plot + per-phase table of what you just did
bash buffer-control-experiments/stop_traffic.sh              # stop all traffic + flowmon
```

Experiment B continues the same session. Once students have picked a cap, the instructor
changes the radio and students re-tune (answer key in EXPERIMENTS.md, section B):

```bash
bash buffer-control-experiments/radio.sh 10                  # sustained drop: MCS <= 10, download ~33 -> ~10 Mbit/s
bash buffer-control-experiments/radio.sh 16                  # milder: MCS <= 16, ~16.5 Mbit/s
bash buffer-control-experiments/radio.sh 10 fade             # fading: dips to MCS <= 10, 100 ms every 400 ms
bash buffer-control-experiments/radio.sh 5 fade 50 200       # fading: dips to MCS <= 5, 50 ms every 200 ms
bash buffer-control-experiments/radio.sh trace ped           # replay a recorded mobility trace (ped, veh)
bash buffer-control-experiments/radio.sh good                # channel back to normal (next grant)
```

`bufcap` and `badchan` sit on different hooks, so both can be loaded at once. Only one codelet
can attach to `rlc_dl_ctrl`: `bufcap.sh load` swaps caps for you, and refuses while the
`bufctl` xApp (`bufctl_cli.py`) is attached.

### What the dashboard shows
- **Now** tiles: the video call's RTT (green / amber / red at 100 / 150 ms), the download's
  throughput, the cap in force, and the channel's MCS ceiling.
- **Latency**: video RTT, download RTT and the gNB's RLC queuing delay. Both flows share the
  UE's one RLC queue, so their RTTs move together.
- **Throughput** per flow.
- **RLC buffer**: occupancy (per-bucket average and max) against the cap in force.
- Legends show each series' mean and max. Every `bufcap.sh` / `radio.sh` action appears as an
  annotation line across all panels.

Per-flow metrics come from `flowmon.py`: it reads `ss -ti` for each iperf3 sender socket in the
UPF, labels it by port (5201 = download, 5202 = video), and averages onto the same 134 ms
bucket grid as the `upt` codelets and `tcp_probe`. The older dashboard's `tcp_rtt_ms` is per UE
and merges both flows.

Other traffic options: `traffic_A.sh start --cc bbr` makes the download BBR; `video-only` and
`bulk-only` start one flow at a time.

## Files

| File | What it does |
|---|---|
| **Knobs** | |
| `bufcap.sh` | Build / load / unload the RLC buffer cap. `SIZE` = bytes with k/m suffix, `off` = restore config, `dyn` = the experimental dynamic controller |
| `radio.sh` | Instructor's channel switch: `radio.sh MCS`, `MCS fade [ON PERIOD]`, `trace NAME`, `good` |
| `badchan.sh` | Build / load / unload the MCS-ceiling codelet behind `radio.sh` |
| **Traffic and monitoring** | |
| `traffic_A.sh` | Paced video flow + bulk download on the single UE |
| `stop_traffic.sh` | Stop every iperf3 flow and flowmon (wraps `scripts/stop_traffic.sh`) |
| `flowmon.py` / `flowmon.sh` | Per-flow RTT / throughput / cwnd into VictoriaMetrics (started by `traffic_A.sh`) |
| `make_dashboard.py` | Creates the Grafana dashboard `expA-bufcap` |
| `snapshot.py` | Timeline plot + per-phase table for the last N minutes of an interactive session |
| **Scripted runs** | |
| `run.sh` | Fresh single-UE bring-up (unless `NO_SETUP=1`), then `runner.py`, per config |
| `runner.py` | Runs one config: flows, timed cap / radio events, data collection, then `analyze.py` |
| `analyze.py`, `plotlib.py` | Per-phase `summary.json`, `timeseries.csv`, `timeline.png`, `cap_curve.png` |
| `configs/expA_*.json` | Experiment A: caps stepped down, stepped up, with BBR |
| `make_expB.py`, `compare_B.py`, `grid_B.sh` | Experiment B: step schedule, static vs dynamic; the channel × cap grid |
| `make_expT.py`, `compare_T.py`, `traces/`, `gen_trace_h.py` | Mobility-trace replay (ped, veh from the Amarisoft runs) |
| `make_expD.py`, `run_expD.sh`, `compare_D.py`, `mu_table.csv`, `gen_mu_h.py` | Static vs dynamic controller study (experimental) |
| `setup_4ue.sh` | 4-UE broker bring-up (4 saturating UEs are unstable; not used by the demos) |

Results go to `results/` (not committed).

## Scripted runs

```bash
bash buffer-control-experiments/run.sh buffer-control-experiments/configs/expA_cubic_down.json
TAG=r2 bash buffer-control-experiments/run.sh buffer-control-experiments/configs/expA_cubic_down.json   # repeat
NO_SETUP=1 bash buffer-control-experiments/run.sh ...                                            # skip bring-up
bash buffer-control-experiments/run_expD.sh 3                # full static-vs-dynamic matrix, resumable
```

A config lists flows (start times, congestion control, rate) and timed events
(`{"t": 60, "bufcap": "256k"}`, `{"t": 90, "radio": "10"}`). See the docstring in `runner.py`.

## Measurement notes

- Latency is **TCP srtt** (iperf3 sender on the UPF) and **RLC queuing delay** (jbpf `upt`
  telemetry). It is not ping: a sparse ping pays the SR → UL-grant cycle each time, so it
  doesn't see what a full-buffer flow sees.
- The first `settle` seconds of every phase are dropped from the summary.
- A cap applies on the bearer's next DL packet. Loading takes ~1.5 s, a swap ~4 s. The runner
  records when each load actually completed (`t_actual`) and cuts phases there.
- The ZMQ gNB runs slots at ~0.44× real time, so `badchan` fades and traces are timed by the
  wall clock, not the slot counter.
- iperf3 is started through a host-side `kubectl exec`, never `setsid … & disown` inside a
  container: orphans there become zombies of the container's PID 1, which never reaps them.
