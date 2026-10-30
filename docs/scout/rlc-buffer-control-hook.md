# RLC DL buffer-size control hook (`rlc_dl_ctrl`)

Developer guide and API spec for xApp authors.

Companion docs: [`scout-control-doc.md`](./scout-control-doc.md) (original design and first experiment),
[`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md) (development log, decisions and open items),
[`scout-telemetry-doc.md`](./scout-telemetry-doc.md) (the sensors you'd drive this from).

## 1. What it does

`rlc_dl_ctrl` lets an xApp change a bearer's **downlink RLC buffer size at run time**, without restarting the gNB or
touching its config. A codelet on this hook can set, per UE and DRB:

- a **byte limit**: the most data allowed to sit in that bearer's RLC SDU queue, and
- an **SDU limit**: the most packets allowed to sit in it.

Sizing this buffer well is a real problem, and no single value fits every case. Too small, and the queue cannot absorb
wireless rate variability or a burst, so packets are dropped. Too large, and the queue simply fills up, adding delay
(bufferbloat) without adding throughput. The right size depends on conditions inside the RAN (bandwidth, scheduling,
retransmissions) and outside it (the transport protocol and how the application sends), so it needs study and tuning per
application and deployment. That is what this hook exists to support.

> **Unload the control codelet when you are not actively commanding.** While it is loaded, this hook runs on **every
> downlink packet** of every AM DRB. Unloading costs nothing: every limit already applied stays exactly as it was.

### What it does *not* do

**It does not allocate or free memory, and it does not resize the queue.** It writes two threshold variables:

| Variable | Where | Meaning |
|---|---|---|
| `byte_limit` | `std::atomic<uint32_t>` in `rlc_sdu_queue_lockfree` (`ocudu-jbpf/lib/rlc/rlc_sdu_queue_lockfree.h`) | Byte threshold tested on arrival |
| `sdu_limit` | `std::atomic<uint32_t>` in the same class | SDU-count threshold tested on arrival |

The queue's storage (`sdu_states`, `sdu_sizes` and the lock-free SPSC queue) is allocated once when the bearer is
created, sized by `capacity` (= the configured `queue-size`), and is never resized. Consequences:

- The new value affects **the arriving packet and later ones only**. It is evaluated in `write()` when a packet arrives.
- **Packets already queued are never touched.** Lowering a limit sheds nothing: the queue drains normally while new
  arrivals are dropped until occupancy falls back under the limit.
- The SDU limit can never exceed `capacity`, because those slots are all that exist.
- Memory usage, and therefore the worst case, is unchanged: it is always the configured `queue-size`.

So this is a **drop threshold**, not a buffer allocator.

## 2. Mechanism

```
xApp ──command──> jbpf control-input channel ──> control codelet ──writes──> jbpf_rlc_ctrl_info
                                                                                    │
  rlc_tx_am_entity::handle_sdu()  (once per DL SDU, before the enqueue decision)     │
      ├─ hook_rlc_dl_ctrl(&ci) ─────────────────────────────────────────────────────┘
      ├─ clamp + apply to sdu_queue (only if the value actually changes)
      └─ sdu_queue.write(sdu) ──> enqueue, or tail-drop if over either limit
```

The test inside `write()` is, in order:

```cpp
if (sdu.buf.length() + st.n_bytes > byte_limit) return false;   // byte limit
if (st.n_sdus >= sdu_limit)                    return false;    // SDU limit
```

A `false` return means the SDU is dropped: it is logged, counted as a lost SDU, reported on the `rlc_dl_lost_sdu` hook,
and freed. TCP sees the loss and backs off.

**Scope.** Downlink only, RLC **AM** only (the hook exists only in `rlc_tx_am_entity`), and **DRBs only** — the gNB does
not call the hook for signalling bearers, so SRBs always keep their configured limits. UM DRBs cannot be controlled
today; see §7.

## 3. API

### 3.1 Hook context (gNB ↔ codelet)

`struct jbpf_rlc_ctrl_info` (`ocudu-jbpf/include/srsran/jbpf/jbpf_srsran_contexts.h`), passed as `ctx.data`:

| Field | Direction | Meaning |
|---|---|---|
| `du_ue_index` | gNB → codelet | DU UE index of this bearer |
| `is_srb` | gNB → codelet | Always 0 (kept for layout compatibility) |
| `rb_id` | gNB → codelet | DRB id |
| `cur_byte_limit`, `cur_sdu_limit` | gNB → codelet | The limits in force right now |
| `new_byte_limit`, `new_sdu_limit` | codelet → gNB | Requested limits; **0 means leave unchanged** |
| `cfg_byte_limit`, `cfg_sdu_limit` | gNB → codelet | The bearer's **configured** limits, so a codelet can restore them (RESET) or report them (GET) |

Codelets must bounds-check `ctx.data` against `ctx.data_end` before use, and must define this struct locally: they build
against the SDK image's headers, not the gNB tree, so the two definitions are the wire contract.

### 3.2 Command message (xApp → codelet)

The `rlc_ctrl` codelet takes a 20-byte little-endian message on its `ctrl_in` channel:

```c
struct rlc_ctrl_msg {
    uint32_t du_ue_index;  // target UE
    uint32_t op;           // 0 = SET, 1 = RESET, 2 = GET
    uint32_t rb_id;        // target DRB id
    uint32_t byte_limit;   // bytes, or 0 to keep the stored value
    uint32_t sdu_limit;    // SDUs,  or 0 to keep the stored value
};
```

Python: `struct.pack("<IIIII", ue, op, drb, byte_limit, sdu_limit)`.

### 3.3 Inputs

| Input | Required | Valid range | If missing or invalid |
|---|---|---|---|
| `du_ue_index` | Yes | 0…63 (codelet table size) | Message **silently ignored**; no bearer is changed |
| `rb_id` (DRB) | Yes | 1…29 (OCUDU `MAX_NOF_DRBS`) | Message **silently ignored** |
| `op` | Yes | 0 = SET, 1 = RESET, 2 = GET | An unknown op is **ignored**. RESET and GET ignore `byte_limit` / `sdu_limit`. |
| `byte_limit` | No | 9007 … configured `queue-bytes` | 0 keeps the stored value; out-of-range values are **clamped**, not rejected |
| `sdu_limit` | No | 1 … configured `queue-size` | 0 keeps the stored value; out-of-range values are **clamped** |

There is no wildcard: a command targets exactly one (UE, DRB). Send one message per bearer.

### 3.4 Behaviour

1. The codelet stores the command per (UE, DRB). A 0 field keeps whatever was stored before.
2. On that bearer's **next downlink packet**, the codelet writes the stored values into the hook context.
3. The gNB clamps them and applies them only if they differ from the current values, logging one line per change:
   `rlc_dl_ctrl changed byte_limit. old=… new=… requested=…` (`requested` ≠ `new` means it was clamped).
4. The limit stays in force until it is changed again, the bearer is recreated (UE re-attach) or the gNB restarts.
   **Unloading the codelet does not restore anything**, and jbpf deletes the codelet's stored commands with it.

Timing: a command has no effect while the bearer is idle, because the hook only runs when a packet arrives.

### 3.5 Limits and clamping

| Limit | Range | Why |
|---|---|---|
| SDU count | `[1, capacity]`, where `capacity` = configured `queue-size` | 1 keeps the bearer alive; the queue has only `capacity` slots |
| Bytes | `[min(9007, cfg_byte_limit), cfg_byte_limit]`, where `cfg_byte_limit` = configured `queue-bytes` | 9007 = one maximum PDCP PDU (9000 + 3 header + 4 MAC-I), so an empty queue can always take a packet; above the configured value has no effect anyway, because PDCP admission uses the configured value. The floor is `min(…)` so that a bearer configured *below* 9007 stays reachable rather than being clamped upward past its own config |

Limits can only be tightened relative to the configuration, never widened. Configured values come from `qos.yml`
(`rlc.am.tx.queue-size` / `queue-bytes`) or, if absent, the built-in defaults (16384 SDUs / 6,172,672 B).

**Restoring the default.** `RESET` makes the codelet write the bearer's configured values, which the gNB passes down as
`cfg_byte_limit` / `cfg_sdu_limit`, so an xApp never needs to know or store them. Sending a very large value (for
example `0xFFFFFFFF`) has the same effect, because the clamp turns it into the configured value.

**Reading the current values.** `GET` makes the codelet emit one report for that bearer — UE, DRB, current limits and
configured limits — on its output channel, which is how an xApp or CLI displays what is in force without keeping any
state of its own.

### 3.6 Return values and error handling

| Layer | Success | Failure |
|---|---|---|
| xApp send (`jrtc_app_router_channel_send_input_msg`) | 0 | Non-zero: the channel is missing (codelet not loaded) or full (32-message depth) |
| Codelet `jbpf_main` | `JBPF_CODELET_SUCCESS` | `JBPF_CODELET_FAILURE` only if the context bounds check fails |
| gNB | Applies the clamped value, logs the change | Never rejects a command; out-of-range values are clamped |

**Verified on the testbed (2026-09-16).** On a 5QI 9 AM DRB configured for 8192 SDU / 8,388,608 B carrying ~40 Mbit/s
of downlink traffic: `GET` reported the current and configured limits; `SET sdus=256` and `SET bytes=200000` applied
exactly as requested; `SET sdus=1` took the legal floor; `SET bytes=100` was **clamped up to 9007**; and `RESET`
restored 8,388,608 B / 8192 SDU. Every change appeared both in the gNB log and in a report to the xApp. Bearers with no
downlink traffic stayed silent, which is expected — the hook only runs when a packet arrives.

**Confirming a change.** The `rlc_ctrl` codelet emits a report on its `out_rlc_ctrl` channel when a `GET` is requested,
when a `RESET` is applied, and whenever the limits in force change — which is how an xApp sees that a `SET` took effect,
and what it was clamped to. Each report carries `{du_ue_index, rb_id, cur_byte_limit, cur_sdu_limit, cfg_byte_limit,
cfg_sdu_limit, reason}`. The gNB log line above is the second source of truth.

Failure modes worth handling in an xApp:

| Situation | What happens | What to do |
|---|---|---|
| Codelet not loaded | Send returns non-zero | Load the codelet set, then retry |
| Bearer idle | The command waits in the codelet | Treat "applied" as unconfirmed until traffic flows |
| Bearer released while a command is stored | The command stays in the table and will apply to a future UE that reuses the index | Re-send on bearer changes; see open item 9 in the changes doc |
| Value below the minimum | Clamped, not rejected | Compare `requested` and `new` in the gNB log |
| More than one xApp | Only one codelet can be attached to the hook, and the last command for a bearer wins | Arbitrate before sending |

## 4. Byte limit, SDU limit, or both?

Both limits are always active, and **whichever is hit first causes the drop**. What you leave at 0 keeps its previous
value, which at bearer start means the configured value.

| You set | Effective behaviour | Assumption to be aware of |
|---|---|---|
| **SDU limit only** | Drops at N packets; the byte limit stays at the configured value, usually far larger | Buffer size in *bytes* then depends on packet size. N full-size packets ≈ N × 1507 B, but a flow of small packets holds far less data, so queuing delay is not what you assumed |
| **Byte limit only** | Drops at B bytes; the SDU limit stays at `capacity` | Usually the cleaner choice, since delay tracks bytes ÷ rate. With many tiny packets you may hit `capacity` first |
| **Both** | Drops at whichever binds first | Recommended. To make the SDU count the binding one, set bytes ≈ N × 1507 so the byte cap can't trip first |

Use 1507 B per packet, not 1500. An RLC SDU is a PDCP PDU, so a 1500-byte IP packet costs 1500 + 3 (PDCP header) + 4
(MAC-I) = 1507 — the same 7 bytes of overhead the gNB's own `min_runtime_byte_limit` (9000 + 3 + 4) accounts for. OCUDU's
default sizing uses it too: `default_rlc_queue_size_bytes = 4096 * (1500 + 7)` = 6,172,672
(`include/ocudu/du/du_high/du_qos_config_helpers.h`).

## 5. Example xApps

Two worked examples ship in the repo, at opposite ends of the effort scale:

| Example | What it does | When to copy it |
|---|---|---|
| `jrtc-apps/codelets/bufcap/` | **No xApp at all.** One source built once per cap (`-DCAP_BYTES=<bytes>`); while loaded it asserts that byte limit on every DRB of every UE. `bufcap_off` (CAP_BYTES=0) writes the bearer's `cfg_*` values back, which is how a cap is undone. Six codeletsets (`16k`…`1m`, `off`) with matching deployments in `jrtc_apps/bufcap/`. | A fixed setpoint you select by loading a codeletset. Used by the tutorial experiments in `buffer-control-experiments/`. |
| `jrtc-apps/jrtc_apps/bufctl/` | The full command path: xApp + terminal CLI, SET/GET/RESET per bearer. | Anything dynamic, per-UE, or closed-loop. |

**The reference example: `bufctl`.** `jrtc-apps/jrtc_apps/bufctl/bufctl_app.py` is the xApp, and `bufctl_cli.py` is a
terminal front end for it.

- `bufctl_cli.py` runs on the host. It loads the `bufctl` deployment on start and unloads it on quit, since the hook
  runs on every downlink packet while the codelet is loaded. Commands: `scan` (ask a range of UEs and DRBs to report),
  `list`, `set <ue> <drb> sdu|byte <n>`, `reset <ue> <drb>`, `load`, `unload`, `quit`. It shows each bearer's current
  and configured sizes side by side with the valid range, and checks a value before sending it.
- `bufctl_app.py` declares two streams: an RX stream for the codelet's reports (wildcard destination, like any
  telemetry stream) and a **TX stream to `ctrl_in` on the concrete device** — an input channel will not resolve against
  a wildcard, which is the one non-obvious thing about sending on a control hook. It is otherwise a relay: it reads
  command lines the CLI appends to `commands.jsonl`, sends them on `ctrl_in`,
  records every report to `state.json` and `events.jsonl`, and prints them to the jrtc log. It keeps no persistent
  state, so there is no ordering requirement against UE attach — a bearer appears as soon as it carries traffic.
- Those files live in `jrtc_apps/bufctl/`, which the jrtc pod sees as `/apps/bufctl`. They are the CLI ↔ xApp
  transport, not a database, and can be deleted at any time.

When you replace the CLI with an algorithm, only `bufctl_app.py` changes: the codelet, the hook and the gNB stay as
they are.

## 6. Loading and building

```bash
# build the control codelet (runs in the SDK container; run from codelets/)
cd jrtc-apps/codelets && ./make.sh -d bufctl     # or: -d bufcap
# expect "Program terminates within 892 instructions" for bufctl/rlc_ctrl (2026-09-27)

# load a codelet set containing a control codelet, from inside the jrtc pod
jrtc-ctl load -c /apps/bufctl/deployment.yaml

# unload (limits stay as last applied -- send RESET first to restore the config)
jrtc-ctl unload -c /apps/bufctl/deployment.yaml
```

`bufctl_cli.py` does the load/unload for you; its `load` / `unload` commands exist for when you want to hold a session
open across them.

**Changing the gNB side** (this hook, or `jbpf_rlc_ctrl_info`) takes more than a rebuilt image: the gNB runs in an
ephemeral container that cannot be replaced in place, so restarting the gNB process alone keeps the old binary. Build
the image, `k3d image import` it, delete the gNB pod, re-patch the ephemeral container, then bring the pipeline up —
the exact commands are in §6 of [`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md). To check which binary is live, compare
`md5sum /usr/local/bin/gnb` inside the gNB pod's `ocudujbpf` container
(`kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- md5sum /usr/local/bin/gnb`) with the same file in the image. A codelet built against
newer context fields than the running gNB fails its bounds check on every packet.

**Unload as soon as you have finished commanding.** While the set is loaded, the hook runs on every downlink SDU of
every AM DRB, and between commands that work is wasted. Unloading leaves every applied limit in place, so the usual
pattern is: load → set the sizes you want → unload → run the experiment. To return a bearer to its configured size,
send the maximum value (§3.5) *before* unloading.

Only **one** codelet can attach to a control hook, so every codeletset that targets `rlc_dl_ctrl` is mutually
exclusive with the others. Today that is `codelets/bufctl/bufctl.yaml` and the six
`codelets/bufcap/bufcap_*.yaml`. The telemetry set `codelets/upt/upt.yaml` is **not** among them — its control codelet
was moved out to `bufctl` for exactly this reason, so `upt` and `bufctl` can be loaded together (and normally are:
`upt` supplies the sensors you size the buffer against).

## 7. Limitations and TODO

**Move the drop decision up to PDCP.** Today RLC drops a packet *after* PDCP has already given it a sequence number.
That leaves a hole in the sequence, so the UE's PDCP holds every later packet until `t_reordering` expires (220 ms for
5QI 9, which has `out_of_order_delivery: false`), turning a drop meant to signal congestion into a latency spike. PDCP
already has a clean drop point before numbering (`check_early_drop()`), but it compares against a *static*
`desired_buffer_size` that RLC reports from config, so it never learns the dynamic limit. Reporting the live byte limit
there would make drops happen before numbering. Two caveats: PDCP counts bytes not yet *delivered*, including packets
already sent by RLC but unacknowledged, so it trips earlier than RLC would; and PDCP has no SDU-count equivalent.

Also open:

- **RLC UM DRBs** have no hook, so only AM bearers can be controlled.
- **Targeting by 5QI** is not possible; no hook exposes 5QI, so xApps must use DRB ids.
- **Per-packet cost**: the hook runs on every downlink SDU while a codelet is loaded.
- **Stale commands** after a UE is released, because the table is keyed on `du_ue_index`.

The full list, with priorities, is in section 5 of [`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md).

## 8. Operational contract for the example xApp

`commands.jsonl` is **append-only** while the codeletset is loaded. The xApp tracks how
many bytes it has consumed, so rewriting the file in place can collide with that offset;
it now detects a same-length rewrite and re-reads from the start, which **replays every
command in the file**. Delete or truncate it only while bufctl is unloaded.

A command takes effect on the bearer's **next downlink packet** -- an idle bearer will not
report until traffic resumes. `GET` is the way to read back what is in force without
changing it, and every report carries both the live limits (`cur_*`) and the configured
defaults (`cfg_*`), so an xApp never needs to know the gNB's configuration in advance.
