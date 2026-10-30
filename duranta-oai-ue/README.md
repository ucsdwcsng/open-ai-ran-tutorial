# Duranta OAI nr-UE (ZMQ) — 4-UE testbed

The OAI-UE side of the OCUDU + jbpf testbed: a **Duranta** OAI `nr-UE` built for ZMQ, plus the
GNU Radio broker + per-UE network-namespace plumbing to run **4 UEs** against one OCUDU gNB.

The gNB (jbpf-enabled OCUDU) and its deploy configs live in the [`ocudu-jbpf`](../ocudu-jbpf)
submodule (`ocudu-jbpf/deploy/`).

## Why Duranta (not mainline OAI)

Mainline `oai-nr-ue` has interop bugs against OCUDU (DCI 1_1 crashes). The
[Duranta](https://github.com/duranta-project/openairinterface5g) fork (`-b 2026.w25`) attaches
cleanly. The crux gNB-side fix is `cell_cfg.nof_antennas_dl: 1` (else the UE computes
`max_mimo_layers=0` and crashes on the first DL DCI 1_1).

## Contents

- `duranta-ue/`           — submodule: our Duranta OAI fork ([ucsdwcsng/open-ai-ran-duranta-ue](https://github.com/ucsdwcsng/open-ai-ran-duranta-ue): ZMQ real-time fix + per-UE channel model).
- `Dockerfile`            — builds `nr-uesoftmodem` (ZMQ radio) from `duranta-ue/` → image `duranta-nr-ue:local`.
- `oaiue_zmq.conf`        — single-UE OAI config (point-to-point ZMQ).
- `oaiue{1..4}_netns.conf`— per-UE configs for the 4-UE broker setup (tx binds `10.201.N.2:200X`,
  rx connects `10.201.N.1:20X0`; IMSI ...001–004).
- `uecap_ports1.xml`      — UE capability file (referenced by the confs as `uecap_file`).
- `netns_setup.sh`        — creates `ue1..ue4` network namespaces + veth pairs, so each UE's
  identically-named `oaitun_ue1` is isolated (no collision in the shared pod netns).
- `broker4_netns.py`      — GNU Radio ZMQ broker: sums the 4 UE UL streams into the gNB and fans
  DL out to each UE via its veth IP.

## Build

```bash
git submodule update --init duranta-oai-ue/duranta-ue   # from the repo root, once
docker build -t duranta-nr-ue:local -f Dockerfile .
```

## Run the 4-UE setup (order matters: broker → gNB → UEs)

```bash
# 1. per-UE netns + veth (privileged; needs /var/run/netns)
./netns_setup.sh

# 2. broker (its own container/netns that shares the gNB pod netns)
python3 -u broker4_netns.py &

# 3. start the OCUDU gNB with ocudu-jbpf/deploy/gnb_ocudu_broker.yml (or gnb_ocudu_jbpf.yml for jrtc)

# 4. one UE per netns
for n in 1 2 3 4; do
  ip netns exec ue$n nr-uesoftmodem -O oaiue${n}_netns.conf \
      --band 78 --numerology 1 -r 51 -C 3489420000 --ue-scan-carrier -E &
done
```

Each UE gets its own `oaitun_ue1` (distinct core IP) and pings the UPF over the ZMQ radio.
ZMQ can't reconnect mid-run — always restart **broker → gNB → UEs** together.

## N UEs with a per-UE channel (k3d, scripted)

The k3d counterpart of ocudu-edgeric's `start_gnb_zmq.sh` + `start_oai_ues.sh <c> --mobility`:

```bash
bash scripts/setup_zmq_chan_demo.sh <N>                       # N UEs, static 30 dB each (CQI 15)
bash scripts/setup_zmq_chan_demo.sh 2 --channel duranta-oai-ue/ue_channel_mobility.conf
bash scripts/setup_zmq_chan_demo.sh 3 --clean                 # no channel, no added noise
bash scripts/setup_zmq_chan_demo.sh 4 --traces duranta-oai-ue/ue_traces_4ue.conf   # SNR traces, switchable
bash scripts/trace_ctl.sh set 1 trace_2                       # mid-run: ue1 onto another trace
bash scripts/traffic_nue.sh start                             # DL TCP (UPF -> every UE)
bash scripts/traffic_nue.sh status                            # per-UE + total, measured at the UE
bash scripts/traffic_nue.sh stop && bash scripts/stop_demo.sh
```

- `broker_cpp/zmq_broker.cpp` (default, `--broker cpp`) — single-threaded C++ broker, same options as
  the Python one. Build: `bash duranta-oai-ue/broker_cpp/build.sh`. Logs the sample rate it moves
  every 5 s (`[broker] rate ... (100% = real time)` in the grbroker container's `/tmp/broker.log`).
- The broker survives a UE that dies (e.g. an OAI assert): once every UE is streaming, a UE that
  has sent no UL for 200 ms while the others have is dropped for good and the rest of the cell
  carries on (`[broker] UE<i> ... dropped` in its log). It cannot be re-admitted: ZMQ UL/DL timing
  is sample counting from a common start, so a late UE would transmit at an arbitrary offset.
  Start-up stays frozen until every UE has delivered UL, for the same reason.
- `multi_ue_broker.py` (`--broker py`) — ocudu-edgeric's GNU Radio broker for any N.
- Links: every ZMQ socket is an `ipc://` socket in the broker container's `/tmp/zmq`; the UE and gNB
  containers reach them through `/proc/<broker pid>/root`. The gNB side needs the ocudu-jbpf build
  that accepts `ipc://` (patched `lib/radio/zmq/radio_config_zmq_validator.cpp`); with an older gNB
  the setup falls back to tcp for that link. `--tcp` puts every link back on tcp.
- `netns_setup_n.sh` — netns + veth for N UEs.
- `ue_channel.conf` / `ue_channel_mobility.conf` — one line per UE: distance, speed, bounce range,
  DL/UL SNR, shadowing. Parsed into `--zmq.[0].chan` (model: `duranta-ue/docs/zmq-channel.md`).
- `--traces ue_traces_4ue.conf` — each UE replays a DL/UL SNR trace from attach (several UEs may
  share one), and `scripts/trace_ctl.sh` switches any UE to another trace mid-run. The library is
  `traces/lib/*.csv`: random walks `trace_1`..`trace_8` and reference traces, generated by
  `traces/gen_traces.py` from `traces/scenarios.conf` (`traces/check_parity.py` checks its port of
  the UE's model against `chan_sim.cpp`). Walkthrough: `docs/run_system.md`.
- The gNB runs `gnb_zmq_tdd_n78_20mhz.yml`: ocudu-edgeric's n78 20 MHz ZMQ config (warning-level
  log, OLLA, per-UE stdout metrics in `/tmp/gnb.stdout`), TDD 7 DL : 3 UL, k3d core/ports.
- IMSIs beyond the four provisioned ones are cloned from `999700000000001` on demand.
- Changing N = re-run the script; ZMQ cannot add a UE to a running broker.
- `scripts/zmq_tune.sh apply` (run by the setup unless `--no-tune`): SCHED_FIFO for the gNB and broker
  threads + all of it on NUMA node 1. `scripts/zmq_rt_check.sh` measures how close to real time the
  chain runs (23.04 MS/s on the gNB link = 100%).
- `GNB_ARGS=...` / `UE_ARGS=...` pass extra options to the gNB / every nr-uesoftmodem.

### duranta-ue fixes carried here

The OAI UE `AssertFatal`s on some fields of a falsely detected DCI (random bits that pass the CRC),
which kills the UE. Seen twice in 11-minute 4-UE soaks; patched to drop the DCI instead, as the UE
already does for an invalid MCS:
- `mac_tables.c` `set_antenna_port_parameters`: bound the table index (`antenna_ports` 12-15 read
  past the table and gave 0 CDM groups -> assert in `nr_dlsch_extract_rbs`).
- `nr_ue_procedures.c`: reject 0/out-of-range CDM groups in DCI 1_1, and a non-2-bit SRS request
  field in DCI 1_1 and 0_1 (was `AssertFatal`).

### Measured (2026-09-30, k3d, static 30 dB channel on every UE, default settings)

Air capacity is fixed: the gNB MAC carries 63-64 Mbit/s per air-second at CQI 15 / MCS 27 / 0% BLER
for any N. Wall-clock goodput = that x how close to real time the lock-step ZMQ chain runs.

| UEs | real time under load | UDP (at the UEs) | TCP (at the UEs) |
|---|---|---|---|
| 1 | 96-97% | 59.2 Mbit/s | 59.1 Mbit/s |
| 2 | 98-99% | 54.0 Mbit/s (one 10 s window) | 59.6 Mbit/s |
| 4 | 100% | 61.6 Mbit/s | 59.9 Mbit/s |
| 8 | 70-72% | - | 42.6 Mbit/s (6/8 attached) |

4 UEs held 11 min of full TCP load at 100% real time every minute, 59.9-61.7 Mbit/s total, all at
CQI 15 / MCS 27, with no drop, re-attach, RLF or UE crash (with the duranta-ue DCI fixes above).
At 8 UEs the broker's one thread is saturated (~95% of a core moving ~6 GB/s of sample copies):
the next limit.

How 4 UEs got from 67% to 100% of real time under load (each step measured on its own):

| change | 4-UE real time under load |
|---|---|
| baseline (Python broker, tcp everywhere, both directions paced, SCHED_OTHER) | 66-69% |
| SCHED_FIFO for gNB + broker threads, NUMA node 1 (`zmq_tune.sh`) | 77% |
| + `expert_phy.max_proc_delay: 8`, broker paces DL only | 81.5% |
| + ipc broker <-> UE | 85% |
| + C++ single-threaded broker (gNB link still tcp) | 97-99% |
| + ipc gNB <-> broker (patched gNB) | **100%** |
| no gain: `max_request_headroom_slots`, flexible PDSCH / 8-thread pool, 8 UE DL actors | |
| worse: narrow per-component core pinning | 50% |

Not core count (no cgroup limits, host mostly idle): the chain is a closed REQ/REP loop, so its speed
is set by round-trip latency; the GNU Radio broker (~110 threads, a copy per block) was most of it.
The host CPU governor is `powersave` and was deliberately left alone.
