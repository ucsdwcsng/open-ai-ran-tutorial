# Scout — Dynamic RLC Buffer Control (Part 2)

Reviewer doc for the closed-the-loop control path: an xApp or codelet can change
a DRB's RLC DL buffer limits at runtime, and end-to-end TCP responds.

Companion docs:
[`rlc-buffer-control-hook.md`](./rlc-buffer-control-hook.md) (the API spec — read
that one if you are writing an xApp),
[`scout-telemetry-doc.md`](./scout-telemetry-doc.md) (the sensors you drive this
from),
[`scout-telemetry-grafana.md`](./scout-telemetry-grafana.md) (dashboard),
[`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md) (development log and open items).

> **History.** The first version of this work actuated **bytes only** and drove
> the setpoint from a time sweep baked into the codelet, against the legacy
> `jrtc-apps/containers/Docker/srsRAN_Project` tree. All three are gone: the hook
> now carries an SDU limit and the bearer's configured values, control is
> command-driven from an xApp, and the live RAN is the OCUDU gNB in `ocudu-jbpf`.
> This doc describes the current state (verified 2026-09-27).

---

## 0. Scope — this DID modify the RAN

Unlike the telemetry work, Part 2 **modifies the vendored RAN and rebuilds the
gNB**. All telemetry hooks already existed; there was **no actuation path** (the
RLC byte limit was `const` and nothing read codelet output). A handful of small,
localized edits add one control hook.

The RAN is the **OCUDU gNB** in the [`ocudu-jbpf`](../../ocudu-jbpf) submodule —
an srsRAN-derived tree whose C++ namespace is `ocudu`. Approach chosen (with the
user): a dedicated control hook whose codelet writes a value the gNB reads back.

---

## 1. Mechanism

```
 xApp ──command──> jbpf control-input channel ──> rlc_ctrl codelet
                                                      │ writes ci.new_byte_limit
                                                      │        ci.new_sdu_limit
                                                      ▼
 rlc_tx_am_entity::handle_sdu()            (once per DL SDU, DRBs only)
   └─ handle_jbpf_rlc_ctrl()
        ├─ fill ci.cur_* and ci.cfg_* from the queue
        ├─ hook_rlc_dl_ctrl(&ci)      ← the codelet writes through ctx.data
        ├─ clamp, and apply only if the value actually changed (logs the change)
        └─ sdu_queue.write(sdu)       → enqueue, or tail-drop over either limit
```

The control hook's `ctx.data` points at a **gNB-stack struct**
(`jbpf_rlc_ctrl_info`). A codelet writing through `ctx.data` writes the gNB's
memory, so the gNB reads the new value back after the hook returns — the same
in-place mechanism L4Span used for ECN marking. No jbpf shared-map-in-host access
is needed.

Two properties worth stating plainly, because they surprise people:

- This is a **drop threshold, not an allocator.** The queue's storage is sized
  once at bearer creation and never resized; a new limit affects arriving packets
  only, and lowering it sheds nothing already queued.
- **Unloading the codelet restores nothing.** Whatever limit was last applied
  stays in force until it is changed again, the bearer is recreated, or the gNB
  restarts. Send `RESET` before unloading.

Both are detailed in §1 and §3.4 of
[`rlc-buffer-control-hook.md`](./rlc-buffer-control-hook.md).

---

## 2. RAN changes (all under `ocudu-jbpf/`)

| # | File | Change |
|---|---|---|
| 1 | `include/srsran/jbpf/jbpf_srsran_contexts.h` | add `struct jbpf_rlc_ctrl_info` — `{du_ue_index, is_srb, rb_id, cur_byte_limit, new_byte_limit, cur_sdu_limit, new_sdu_limit, cfg_byte_limit, cfg_sdu_limit}` — 28 B (the `cfg_*` pair was appended to an earlier 20 B layout) |
| 2 | `srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h` | `DECLARE_JBPF_HOOK(rlc_dl_ctrl, …)` (`:1027`) — `ctx.data` → that struct |
| 3 | `lib/rlc/rlc_sdu_queue_lockfree.h` | `const uint32_t byte_limit` → `std::atomic<uint32_t>`, plus `sdu_limit`; `clamp_byte_limit()` / `clamp_sdu_limit()`, clamping `set_*_limit()`, `get_cfg_byte_limit()`, `get_capacity()`. `write()` tests both limits (`:97`, `:103`) |
| 4 | `lib/rlc/rlc_tx_am_entity.{h,cpp}` | `DEFINE_JBPF_CTRL_HOOK(rlc_dl_ctrl)` (`:19`); `handle_jbpf_rlc_ctrl()` (`:127`) builds the struct, calls the hook, clamps and applies; called from `handle_sdu` **only for DRBs** (`:180`) |

Notes for the reviewer:

- `byte_limit` was `const`; making it `std::atomic` keeps the lock-free queue
  lock-free. The class was already non-copyable (unique_ptr members), so the
  atomic adds no move/copy hazard.
- `DEFINE_JBPF_CTRL_HOOK` (control type) vs `DEFINE_JBPF_HOOK` (monitor) — the
  macro tags it `JBPF_HOOK_TYPE_CTRL`.
- **DRBs only.** `handle_sdu` guards the call with `if (not rb_id.is_srb())`, so
  SRBs always keep their configured limits and signalling cannot be starved.
- **Clamping lives in the gNB**, not the codelet: bytes to
  `[min(9007, cfg_byte_limit), cfg_byte_limit]`, SDUs to `[1, capacity]`. Limits
  can only be tightened relative to the config, never widened. A change is logged
  once: `rlc_dl_ctrl changed byte_limit. old=… new=… requested=…`.
- Only **AM** is wired (the DRB is AM). UM would be the same edit in
  `rlc_tx_um_entity.cpp`; there is a `TODO(scout)` on it in the source.
- Everything is inside `#ifdef JBPF_ENABLED`.
- Rebuilding the gNB is **not** just a rebuilt image: the gNB runs in an
  ephemeral container that cannot be replaced in place. Build, `k3d image
  import`, delete the pod, re-patch the ephemeral container, bring the pipeline
  up — exact commands in §6 of [`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md).

The legacy `jrtc-apps/containers/Docker/srsRAN_Project` tree still carries an
early, **byte-only** version of this struct. It is neither built nor deployed;
do not read it as current (open item 12 in the changes doc).

---

## 3. The control codelets

Only **one** codelet may attach a control hook, so the sets below are mutually
exclusive with each other. Both are built with
`cd jrtc-apps/codelets && ./make.sh -d <dir>`.

### 3.1 `bufctl` — the general-purpose executor

* Codelet: `jrtc-apps/codelets/bufctl/rlc_ctrl.cpp` · codeletset `bufctl.yaml`
* Hook: **`rlc_dl_ctrl`** · channels: `ctrl_in` (commands, depth 32) and
  `out_rlc_ctrl` (reports, depth 64)
* Verifier: **892 instructions** (measured 2026-09-27)

On each DL SDU it drains `ctrl_in` into a persistent `(UE, DRB) → limits` table
(`64 × 29` entries, indexed `du_ue_index * 29 + (drb_id - 1)`), then writes the
stored values for this bearer into the hook context. Commands are a 20-byte
little-endian `rlc_ctrl_msg` with three ops — **SET**, **RESET** (write the
bearer's `cfg_*` back) and **GET** (report only). It emits a report when a GET is
requested, when a RESET is applied, and whenever the limits in force change, so
an xApp can see what a SET was clamped to without reading the gNB log.

Full message format, valid ranges and failure modes: §3 of
[`rlc-buffer-control-hook.md`](./rlc-buffer-control-hook.md).

One verifier gotcha, kept here because it costs an afternoon to rediscover:
**no `.rodata`.** A file-scope `static const uint32_t SCHED[8]` makes the
verifier see *0 instructions*, because the loader does not relocate `.rodata`.
Use a `switch` returning the value instead.

### 3.2 `bufcap` — fixed caps, no xApp

`jrtc-apps/codelets/bufcap/bufcap.cpp` built once per cap (`-DCAP_BYTES=<bytes>`)
into six codeletsets: `bufcap_{16k,64k,128k,256k,1m}` and `bufcap_off`. While
loaded, a set asserts its byte limit on every DRB of every UE; `bufcap_off`
(`CAP_BYTES=0`) writes the `cfg_*` values back, which is how a cap is undone.
The setpoint is selected by *which codeletset you load* — there is no command
path and no xApp at all. This is what the tutorial experiments in
`buffer-control-experiments/` drive.

---

## 4. The xApp side — `jrtc-apps/jrtc_apps/bufctl/`

`bufctl_app.py` is the xApp; `bufctl_cli.py` is a terminal front end that runs on
the host. The CLI appends command lines to `commands.jsonl`; the app sends them
on `ctrl_in` and records every report to `state.json` and `events.jsonl`. The
split exists because xApps run inside the jrtc pod and have no terminal.

CLI commands: `scan`, `list`, `set <ue> <drb> sdu|byte <n>`, `reset <ue> <drb>`,
`load`, `unload`, `help`, `quit`. It loads the deployment on start and unloads it
on quit, and validates a value against the bearer's range before sending.

> **The closed-loop transport gap is closed.** The earlier version of this doc
> deferred it: `jrtc_router_input_channel_exists` never returned true, so
> declaring the control channel as an app stream made init time out. **The fix
> was the destination:** an input channel will not resolve against a wildcard
> device, so the TX stream must name the **concrete device**. `bufctl_app.py`
> declares an RX stream for reports (wildcard, like any telemetry stream) and a
> TX stream to `ctrl_in` on device 1, and `jrtc_app_router_channel_send_input_msg`
> then works. Nothing is baked into the codelet any more.

Two leftovers to be aware of:

- `commands.jsonl` is **append-only** while the set is loaded. The app tracks a
  byte offset, so a same-length rewrite is detected and re-read from the start,
  which **replays every command in the file**. Truncate it only while unloaded.
- A command has no effect while the bearer is idle, because the hook only runs
  when a packet arrives. Treat "applied" as unconfirmed until a report arrives.

### Stale: the `upt_ctrl` sweep observer

`upt_app.py`'s `_sweep_tick()` still recomputes an open-loop sweep schedule from
wall-clock and publishes it as `upt_ctrl_byte_limit`, and its `SWEEP_SCHED` /
`SWEEP_STEP_SHIFT` constants carry a "MUST match `codelets/upt/rlc_ctrl.cpp`"
comment. **That file no longer exists** — the sweep codelet was retired when
control moved to `bufctl`. The metric is a fiction; ignore it, or delete the
observer.

---

## 5. Verification — the control works

Direct evidence, on a 5QI 9 AM DRB configured for 8192 SDU / 8,388,608 B carrying
~40 Mbit/s of downlink traffic (2026-09-16):

| Command | Result |
|---|---|
| `GET` | reported the current *and* configured limits |
| `SET sdus=256`, `SET bytes=200000` | applied exactly as requested |
| `SET sdus=1` | took the legal floor |
| `SET bytes=100` | **clamped up to 9007** — `requested=` ≠ `new=` in the log |
| `RESET` | restored 8,388,608 B / 8192 SDU |

Every change appeared both in the gNB log and in a report to the xApp. Bearers
with no downlink traffic stayed silent, which is expected.

The gNB also drops at exactly the setpoint: under a tight byte cap the
`Dropped SDU … queued_bytes=` lines cluster just under the commanded value, and
`upt_rlc_buffer_bytes_max` tracks the setpoint across changes.

---

## 6. End-to-end TCP result (the point of the exercise)

One UE, ~35 Mbit/s DL, **base RTT 29 ms** (srtt of a paced flow alone, not an
idle ping), so **BDP ≈ 127 kB**. Two DL CUBIC flows share the UE's DRB1 and
therefore one RLC queue: a bulk download, and a 2 Mbit/s paced flow standing in
for a video call. The cap is applied with `bufcap`; RAN numbers are jbpf
telemetry, RTT is `tcp_probe` on the UPF. Measured 2026-09-22.

| Cap | vs BDP | Video RTT − 29 ms base | Download | Mean RLC queue |
|---|---|---|---|---|
| none (≈2.7 MB) | ~21× | 570–660 ms | 33–36 Mbit/s | 2.4–2.8 MB |
| 1 MB | ~8× | 165–195 ms | 30–34 Mbit/s | ~760 kB |
| 256 kB | ~2× | 35–40 ms | 29–31 Mbit/s | ~110 kB |
| **128 kB** | **~1×** | **10–13 ms** | **22–28 Mbit/s** | ~26 kB |
| 64 kB | ~0.5× | 1–5 ms | 14–19 Mbit/s | ~6 kB |
| 16 kB | ~0.13× | 0–3 ms (p95 spikes 100 ms+) | 1–9 Mbit/s | ~2 kB |

Reading it:

- **The queue *is* the latency.** Uncapped, the video flow's RTT, the download's
  RTT and the measured RLC queuing delay all lie on top of each other at
  600–800 ms. The flows are not different; they are in the same queue. Note what
  bounds that queue: **not** the 8 MB RLC config but the UPF sender's
  `tcp_wmem` max of 4 MB — there were **zero** RLC losses in a 90 s baseline.
- **A cap near 1 BDP buys ~50× lower latency for ~20% throughput**
  (620 ms → 10–13 ms). That is the bufferbloat trade in one row.
- **Below ~0.5 BDP throughput collapses.** After a loss CUBIC's window drops to
  0.7 × (BDP + cap), which keeps the link busy only while the cap is
  ≳ 0.43 × BDP ≈ 55 kB.
- **Mean occupancy sits well below the cap** (256 kB → ~110 kB, 43%) because
  CUBIC saws. `cap ÷ rate` is a ceiling on added delay, not an estimate of it.
- **BBR needs no cap**: it self-limits to ~1.5 BDP (~190 kB queue, ~72 ms srtt)
  at full rate, so the knob matters most for loss-based CC.

And the reason the knob has to be *runtime*: under a **sustained** rate drop the
right cap gets **smaller** (parked data becomes a standing queue), while under a
**fading** channel it gets **bigger** (the buffer rides through the dips — 256 kB
gives ~2× the throughput of 64 kB). The same knob moves in opposite directions
for two different channel conditions, which no config file can do.

Full method, plots and the fading/rate-drop experiments:
[`buffer-control-experiments/EXPERIMENTS.md`](../../buffer-control-experiments/EXPERIMENTS.md).

The natural next step is a **closed-loop AQM** holding RLC queuing latency at a
target by driving the limits — the transport for it now works (§4), and the
per-packet latency stream (`rlc_pkt_record`, codelet doc §8) is the sensor.

---

## 7. Reviewer checklist

```bash
# the gNB is running a binary that has the control hook
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  grep -ac "Registered codelet rlc_ctrl to hook rlc_dl_ctrl" /tmp/gnb.stdout   # >= 1

# the actuation: every applied change, with what was requested
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  grep -ao "rlc_dl_ctrl changed .*limit\. old=[0-9]* new=[0-9]* requested=[0-9]*" /tmp/gnb.log | tail

# drops pin to the commanded byte limit
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  bash -c 'grep -a "Dropped SDU" /tmp/gnb.log | grep -o "queued_bytes=[0-9]*" | sort | uniq -c | sort -rn | head'

# buffer occupancy should track the setpoint
curl -s 'http://localhost:30491/api/v1/query?query=upt_rlc_buffer_bytes_max'

# what the xApp saw (reports), from inside the jrtc pod
kubectl logs -n ran jrtc-0 -c jrtc | grep bufctl | tail
```

Registration lines go to `/tmp/gnb.stdout`; the gNB logger writes `/tmp/gnb.log`.

**Known gaps:** only AM RLC is wired (§2); no targeting by 5QI, since no hook
exposes it; the hook runs on every DL SDU while a codelet is loaded; commands go
stale after a UE is released, because the table is keyed on `du_ue_index`; and
RLC drops after PDCP has already numbered the packet, so a drop meant to signal
congestion costs a `t_reordering` stall at the UE — moving the decision up to
PDCP is the main open design item. Full list with priorities: §5 of
[`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md).
