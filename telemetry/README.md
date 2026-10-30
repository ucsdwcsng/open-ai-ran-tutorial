# telemetry

The metrics backend, the Grafana dashboards, and the host-side probes that feed them.

| File | What | Used by |
|---|---|---|
| `telemetry-stack.yaml` | VictoriaMetrics (NodePort 30491) and Grafana (NodePort 30490) in namespace `ran` | README §4 |
| `upt-dashboard.json` | the user-plane dashboard `upt-userplane`: UE identity, radio / MAC, RLC queuing, TCP | README §4 (configmap `grafana-upt-dashboard`) |
| `rt-scheduling-dashboard.json` | the per-TTI scheduling dashboard `rt-scheduling` | `scripts/dashboard_realtime_scheduling.sh` |
| `tcp_probe.py` | the dashboard's TCP row: tcpdump on the UPF's `ogstun`, per-UE throughput, RTT and bytes in flight | `scripts/tcp_probe.sh` |
| `ue_rx_probe.py` | UDP as the UEs receive it, from the receivers' logs | `scripts/dashboard_realtime_scheduling.sh` |
| `fwd30450.py` | the forwarder in `jrtc-0` that bridges `jrtc-ctl`'s fixed `127.0.0.1:30450` to the gNB's jbpf proxy | the bring-up and load scripts; see [`docs/codelet-deployment.md`](../docs/codelet-deployment.md) |

xApps in `jrtc-0` push their metrics to VictoriaMetrics in InfluxDB line protocol; Grafana queries it.
