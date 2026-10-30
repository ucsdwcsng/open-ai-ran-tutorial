# How to run the control-hook experiments

All commands run on the testbed host, from the **repo root** (`open-ai-ran-tutorial/`).
Background, math and measured results: [EXPERIMENTS.md](EXPERIMENTS.md).

## 0. Before you start

- The testbed is deployed (README sections 1–5) and the cluster is up
  (`TESTBED_RUNBOOK.md`, section 2, after any cluster restart).
- Grafana is reachable on port 30490 (SSH tunnel if you work remotely).
- Nothing else is attached to the RLC control hook: if `bufctl_cli.py` is open, type `quit`.

## 1. Bring up the testbed (~5 min)

```bash
bash scripts/setup_bufctl_1ue_demo.sh
```

Wait for `SETUP DONE. UE 10.45.0.x attached`.

## 2. Open the dashboard

```bash
python3 buffer-control-experiments/make_dashboard.py      # only needed once per Grafana install
```

Open `http://localhost:30490/d/expA-bufcap` with this exact URL. A `refresh=` parameter left in
the URL overrides the dashboard's 1 s refresh and makes it update in big jumps.

## 3. Experiment A: one knob for bufferbloat

Start a 2 Mbit/s "video call" and a download on the same UE:

```bash
bash buffer-control-experiments/traffic_A.sh start
```

Within ~20 s the video call's RTT climbs to ~600 ms: the download fills the gNB's buffer.

Load buffer caps and watch latency and throughput:

```bash
bash buffer-control-experiments/bufcap.sh load 256k     # RTT drops to ~60 ms, download ~30 Mbit/s
bash buffer-control-experiments/bufcap.sh load 128k     # ~40 ms, download a little lower
bash buffer-control-experiments/bufcap.sh load 64k      # below the BDP: download drops to ~15-20 Mbit/s
bash buffer-control-experiments/bufcap.sh load 16k      # far too small: download collapses
bash buffer-control-experiments/bufcap.sh off           # no cap: the bloat comes back
```

Build and try your own size (any number of bytes, with a `k` or `m` suffix):

```bash
bash buffer-control-experiments/bufcap.sh build 96k
bash buffer-control-experiments/bufcap.sh load 96k
```

Other traffic:

```bash
bash buffer-control-experiments/traffic_A.sh stop
bash buffer-control-experiments/traffic_A.sh start --cc bbr    # BBR download instead of CUBIC
```

## 4. Experiment B: the right buffer depends on the channel

Keep the traffic and a cap running (e.g. 128k), then change the radio:

```bash
bash buffer-control-experiments/radio.sh 10             # sustained drop to MCS <= 10 (~9.5 Mbit/s)
bash buffer-control-experiments/radio.sh 16             # milder drop (~16.5 Mbit/s)
bash buffer-control-experiments/radio.sh 10 fade        # dips to MCS <= 10 for 100 ms every 400 ms
bash buffer-control-experiments/radio.sh 5 fade 50 200  # dips to MCS <= 5 for 50 ms every 200 ms
bash buffer-control-experiments/radio.sh trace ped      # replay a recorded pedestrian channel (or: veh)
bash buffer-control-experiments/radio.sh good           # back to the normal channel
```

After each change, re-tune the cap with `bufcap.sh load ...` and find the new best size.
Expected answers are in EXPERIMENTS.md, section B.

## 5. Save what you did

```bash
python3 buffer-control-experiments/snapshot.py --minutes 5
```

It prints a table per phase (cap, channel, RTT, throughput) and writes a plot to
`buffer-control-experiments/results/session_<time>/timeline.png`.

## 6. Stop

```bash
bash buffer-control-experiments/bufcap.sh off           # restore the configured buffer (needs traffic running)
bash buffer-control-experiments/radio.sh good
bash buffer-control-experiments/stop_traffic.sh         # stops all traffic and the per-flow monitor
bash scripts/stop_demo.sh                          # optional: stop the gNB and UE
```

`bufcap.sh unload` removes the codelet but **leaves the last cap in force**. Use `off` to undo a
cap.

## 7. Scripted runs (optional)

Each run does a fresh bring-up, runs a timed schedule, and writes plots to
`buffer-control-experiments/results/<name>/`:

```bash
bash buffer-control-experiments/run.sh buffer-control-experiments/configs/expA_cubic_down.json   # caps stepped down
bash buffer-control-experiments/run.sh buffer-control-experiments/configs/expA_cubic_up.json     # caps stepped up
bash buffer-control-experiments/run.sh buffer-control-experiments/configs/expA_bbr_down.json     # same, BBR download
bash buffer-control-experiments/run_expD.sh 3     # static vs dynamic cap: fade + mobility traces, 3 repeats (~2.5 h)
python3 buffer-control-experiments/compare_D.py   # summary tables and trade-off plots for run_expD.sh
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `bufcap.sh load` says bufctl is attached | Quit `bufctl_cli.py` (type `quit`) |
| Dashboard panels empty | Traffic running? `bash buffer-control-experiments/traffic_A.sh start` |
| Dashboard updates in 10 s jumps | Reopen the dashboard URL without `refresh=` in it |
| `off` doesn't restore the buffer | It needs downlink traffic flowing; run it while traffic is on |
| UE has no IP after setup | Re-run `bash scripts/setup_bufctl_1ue_demo.sh` |
