# buffer-control-experiments

The tools behind [Demo 3](https://edgeric.github.io/open-ai-ran-tutorial/demo3-buffer-management.html)
of the Open AI-RAN tutorial: TCP flow optimization with real-time RLC buffer management. Run them on the
testbed host from the repo root, after a bring-up with telemetry
(`bash scripts/setup_zmq_chan_demo.sh 2 --telemetry`, or its `--ran` / `--ues` split).

| File | What |
|---|---|
| `traffic_A.sh` | a paced 2 Mbit/s "video call" and a bulk download on one UE's bearer, sent from the UPF: `start`, `bulk-only`, `video-only`, `stop` (`--cc`, `--video-rate`, `--ue N`). Starts and stops `flowmon` |
| `flowmon.sh`, `flowmon.py` | per-flow RTT and throughput from the sender's TCP stack (`ss -ti` in the UPF), written to VictoriaMetrics as `flow_rtt_ms` and `flow_tput_mbps` |
| `bufcap.sh` | fixed RLC DL buffer caps on `rlc_dl_ctrl`: `build [SIZE ...]`, `load SIZE`, `off`, `unload`, `status`, `list` |
| `make_dashboard.py` | creates the Grafana dashboard `demo3-bufcap` (once per Grafana install) |
| `snapshot.py`, `plotlib.py` | a timeline plot and a per-cap table of the last N minutes, in `results/session_<time>/` |

```bash
python3 buffer-control-experiments/make_dashboard.py         # then open http://localhost:30490/d/demo3-bufcap
bash buffer-control-experiments/traffic_A.sh start           # video call + CUBIC download on UE 1
bash buffer-control-experiments/bufcap.sh load 256k          # cap the buffer: the call's RTT falls in seconds
bash buffer-control-experiments/bufcap.sh load 16k           # too small: the download collapses
bash buffer-control-experiments/bufcap.sh off                # restore the configured buffer (needs traffic)
python3 buffer-control-experiments/snapshot.py --minutes 5   # plot + per-cap table of what you just did
bash buffer-control-experiments/traffic_A.sh stop
```

The codelet sources are in `jrtc-apps/codelets/bufcap/`. Only one codelet can attach to `rlc_dl_ctrl`:
`bufcap.sh load` swaps caps for you, and refuses while the `bufctl` xApp
(`jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py`, Demo 3 part 2) is attached. The hook's API is in
[`docs/rlc-buffer-control-hook.md`](../docs/rlc-buffer-control-hook.md).
