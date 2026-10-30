# Tests — RLC downlink buffer-size control

Scripts that drive the OCUDU gNB + one OAI UE so you can exercise the **runtime RLC
downlink buffer-size control** hook (`rlc_dl_ctrl`) and watch the effect on Grafana
(`:30490`, dashboard `upt-userplane`).

Run everything from the **repo base directory**; the scripts derive their own paths, so
any clone location and any user works. They drive an existing deployment — do README
sections 1-5 first, once per server.

## Bring-up and traffic are separate

```bash
bash scripts/setup_bufctl_1ue_demo.sh      # pipeline only: gNB + UE + upt telemetry + tcp_probe (~4-5 min)
bash scripts/traffic.sh start   # then whatever application data you want
bash scripts/status.sh          # read-only: what is up, what limits are in force
bash scripts/stop_demo.sh       # stop everything and reclaim /tmp/gnb.log
```

`setup_bufctl_1ue_demo.sh` deliberately starts **no** traffic, so you can drive your own. If you
just want the original one-shot demo, `bash scripts/restart_demo.sh [seconds]` does both.

### Choosing your traffic

```bash
bash scripts/traffic.sh start                              # DL, CUBIC, 1 hour
bash scripts/traffic.sh start --cc bbr --time 600          # DL, BBR, 10 minutes
bash scripts/traffic.sh start --dir ul --cc reno           # uplink
bash scripts/traffic.sh start --rate 20M --streams 4       # rate-capped, 4 streams
bash scripts/traffic.sh start --udp --rate 50M             # UDP
bash scripts/traffic.sh status                             # live flows + last sender line
bash scripts/traffic.sh stop
bash scripts/stop_traffic.sh                               # stop ALL iperf3 (any script) + report leftovers
bash scripts/traffic.sh --help
```

**Why the server runs in the UE for downlink.** `iperf3 -C` sets congestion control on
the process that *sends*. For a downlink flow the sender is the core side, so
`traffic.sh` runs `iperf3 -s` in the UE container and the client on the UPF with `-C`.
The obvious alternative — a UE-side client with `-R` — makes the **server** the sender,
so `-C` is silently ignored and you always get the node default. Uplink is the mirror.

**Why iperf3 is started through a host-side `kubectl exec`.** Starting it with
`setsid … & disown` *inside* a container orphans it to the container's PID 1 (`tail` /
`open5gs-upfd`), which never reaps it, so every finished iperf3 used to linger as a zombie.
An exec'd process is reaped by the container runtime instead.

## Driving the buffer control

```bash
python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py
```

```
bufctl> scan                    # discover bearers
bufctl> list                    # UE / DRB / current vs configured limits
bufctl> set 0 1 sdu 512         # clamp the DL SDU queue for UE 0, DRB 1
bufctl> set 0 1 byte 200000     # or clamp by bytes
bufctl> reset 0 1               # back to the configured size
bufctl> quit                    # unloads the codeletset
```

Occupancy and queuing latency drop within a few 134 ms buckets while throughput holds.
The gNB clamps out-of-range requests rather than rejecting them, and every report carries
both the live limits and the configured defaults. See `../rlc-buffer-control-hook.md` for
the hook API and `../RLC-CTRL-CHANGES.md` for the development log.

## Notes worth knowing

- **Unload when you are not commanding.** While `bufctl` is loaded the `rlc_dl_ctrl` hook
  runs on *every* downlink packet. `bufctl_cli.py` loads it on start and unloads it on
  quit, including on a crash; `status.sh` prints `loads`/`removes` so you can check.
  Applied limits survive the unload, so `reset` first if you want the configured sizes.
- **`stop_demo.sh` unloads `bufctl` before killing the gNB**, since the unload needs a
  live gNB and jrtc. Limits die with the gNB, so no `reset` is needed first.
- **`/tmp/gnb.log` grows ~1.5 GB/h** at `info` PHY logging. It lives on the container's
  own overlay, not the host root, but it reached 12 GB twice; `stop_demo.sh` truncates it.
- **Telemetry only flows while there is downlink traffic.** "no samples in the last
  5 min" from `status.sh` usually means the flow ended, not that the pipeline broke —
  the codelets fire per packet.
- If the UE fails to attach twice in a row the ZMQ pair needs a fresh gNB; it cannot
  reconnect mid-run, which `setup_bufctl_1ue_demo.sh` handles by starting one each time.

## Known limitation: 4-UE runs do not exercise the cap on every UE

Under four concurrent saturating downlink flows the ZMQ front-end cannot carry four
full-rate UEs and throughput splits very unevenly (observed 10.5 / 10.5 / 0.9 /
0.4 Mbit/s). The starved UEs sit with a full queue that never enqueues, so a limit never
binds on them and their occupancy stays at baseline for the whole run.

That is a traffic-distribution artifact of the ZMQ ceiling, not a control-path bug: the
same limit on a single UE binds immediately and reproducibly. Use the single-UE path for
anything where the limit's effect is what you are measuring.

See `../TESTBED_RUNBOOK.md` for post-cluster-restart bring-up and gotchas.
