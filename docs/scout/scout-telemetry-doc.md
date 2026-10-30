# Scout — Per-Packet User-Plane Telemetry (codelet implementation)

Reviewer-oriented description of the user-plane telemetry codelets: what each
one measures, exactly how each number is computed, which files implement it, and
which RAN call sites the data originates from.

> The RAN is the **OCUDU gNB** in the [`ocudu-jbpf`](../../ocudu-jbpf) submodule
> (an srsRAN-derived tree; the C++ namespace is `ocudu`). All file paths and line
> numbers below are from that tree.

Companion doc: [`scout-telemetry-grafana.md`](./scout-telemetry-grafana.md) (backend + dashboard).

---

## 0. Read this first — scope of changes

**No RAN source file was modified for the telemetry.** Every hook used here
already exists in the `ocudu-jbpf` tree as vendored in this repo. Nothing was
added to or patched into it for these codelets, and the gNB binary is the stock
build from `ocudu-jbpf/build.sh`.

(The *control* hook `rlc_dl_ctrl` **is** a modification to that tree, but it is
separate work — see [`scout-control-doc.md`](./scout-control-doc.md) and
[`rlc-buffer-control-hook.md`](./rlc-buffer-control-hook.md).)

The RAN sections below are therefore **"where the numbers come from"**, for
verifying measurement semantics — not a changelog. This matters because the
original design (RT2C deck, slides 24–26) *did* add hooks
(`gtpu_dl_enq/deq`, `rlc_dl_internal_enq/deq`, `pdcp_dl_sdu_segment`, …) to a
different fork. Those hook names **do not exist here**; see §5.

Everything new lives under:

```
jrtc-apps/codelets/upt/        # the codelets (new)
jrtc-apps/jrtc_apps/upt/       # the consumer app + deployment (new)
telemetry/                     # VictoriaMetrics + Grafana + dashboard (new)
```

---

## 1. What is measured

| # | Codelet | srsRAN hook | Dashboard panel |
|---|---|---|---|
| 1 | `gtp_arrival` | `pdcp_dl_new_sdu` | GTP Arrival Rate |
| 2 | `rlc_queueing` | `rlc_dl_sdu_send_started` | RLC Queuing Latency |
| 3 | `rlc_buffer_enq` | `rlc_dl_new_sdu` | RLC Buffer Occupancy |
| 3 | `rlc_buffer_deq` | `rlc_dl_sdu_send_completed` | (same stream as enq) |
| 4 | `rlc_pkt_record` | `rlc_dl_sdu_send_started` | RLC Queuing Latency (p95 overlay) — see §8 |

Five codelet entries in `upt.yaml` (the buffer pair counts as two). All are **downlink**. All are **per UE, per radio bearer**.

---

## 2. The bucketing scheme (common to all three)

Per-packet events are aggregated **inside the codelet** into fixed time buckets;
one protobuf message is emitted per bucket per stream.

```c
// jrtc-apps/codelets/upt/upt_helpers.h
#define UPT_BUCKET_SHIFT (27)
#define UPT_BUCKET_NS    (1ULL << UPT_BUCKET_SHIFT)   // 134217728 ns
```

**Bucket width = 2^27 ns = 134.217728 ms.**

A power-of-two shift is used instead of a division by 100 ms because 64-bit
division is awkward for the eBPF verifier. The consumer re-derives the exact
width from the same constant.

Rollover logic (identical in all three, e.g. `gtp_arrival.cpp`):

```c
uint64_t now    = jbpf_time_get_ns();
uint64_t bucket = now >> UPT_BUCKET_SHIFT;

if (*last_bucket != bucket) {              // bucket closed
    if (*last_bucket != 0 && out->stats_count > 0) {
        out->timestamp = now;
        out->bucket_id = *last_bucket;
        jbpf_ringbuf_output(&out_gtp_arrival, out, sizeof(*out));
    }
    JBPF_HASHMAP_CLEAR(&gtp_hash);
    jbpf_map_clear(&gtp_stats_map);
    *last_bucket = bucket;
}
// ... accumulate into out->stats[ind]
```

**Why bucket in the codelet rather than stream every packet:** at 50 Mbps a UE
produces ~4167 pkt/s; streaming one IO message per packet is ~130 MB/s per UE
(deck slide 61) and exhausts the jbpf IO mempool — `jbpf_mbuf_alloc` then fails
with *"error dequeuing memory from the mempool"* (deck slides 67–70). Bucketing
makes the output rate **independent of the traffic rate**: ~7.45 msg/s per
stream regardless of load. Per-packet *accuracy* is retained because every
packet still updates the accumulators; only the *reporting* is batched.

Per (UE, bearer) slotting uses the existing protohash helper:

```c
uint32_t ind = JBPF_PROTOHASH_LOOKUP_ELEM_64(out, stats, gtp_hash,
                                             ue_index, rb_id, new_val);
// all accesses are out->stats[ind % UPT_MAX_UE_RB]
```

`UPT_MAX_UE_RB` (16) **must equal** `max_count` in the `.options` files — see §7.

---

## 3. The three codelets in detail

### 3.1 `gtp_arrival` — DL arrival rate

* File: `jrtc-apps/codelets/upt/gtp_arrival.cpp`
* Proto: `gtp_arrival_stats.proto` → message `gtp_arrival_stats`
* Hook: **`pdcp_dl_new_sdu`**
* Context struct: `jbpf_pdcp_ctx_info` (UE id = `cu_ue_index`)

Per event:

```c
uint32_t sdu_length = (uint32_t)(ctx->srs_meta_data1 & 0xFFFFFFFF);
out->stats[i].pkt_count  += 1;
out->stats[i].byte_count += sdu_length;
```

The hook packs `srs_meta_data1 = (count << 32) | sdu_length`, so `sdu_length` is
the **low** 32 bits (`count` is the PDCP COUNT, unused here).

Consumer converts to a rate (`upt_app.py`):

```python
mbps = (byte_count * 8.0) / UPT_BUCKET_SECS / 1e6
pps  = pkt_count / UPT_BUCKET_SECS          # UPT_BUCKET_SECS = 0.134217728
```

> **Why PDCP and not GTP-U:** this RAN build declares **no GTP-U hooks at
> all** (64 hooks total, verified in §5). The DL path is
> GTP-U → SDAP → PDCP → RLC, and `pdcp_dl_new_sdu` is the first per-packet DL
> hook that exists. Every DL packet on an established DRB traverses it, so the
> measured **rate and volume equal the GTP-U arrival rate**; only the absolute
> timestamp differs by the (sub-microsecond) SDAP/PDCP entry cost. It does *not*
> capture packets dropped before PDCP.

### 3.2 `rlc_queueing` — RLC queuing latency

* File: `jrtc-apps/codelets/upt/rlc_queueing.cpp`
* Proto: `rlc_queue_stats.proto`
* Hook: **`rlc_dl_sdu_send_started`**
* Context struct: `jbpf_rlc_ctx_info` (UE id = `du_ue_index`)

The latency is **not computed in the codelet** — srsRAN computes it and passes
it in (see §4.2). The codelet only reduces it:

```c
uint32_t is_retx    = (uint32_t)(ctx->srs_meta_data1 & 0xFFFFFFFF);
uint64_t latency_ns = ctx->srs_meta_data2;      // <-- the measurement

UPT_LAT_UPDATE(out->stats[i], latency_ns);      // count, sum, min, max
if (is_retx) out->stats[i].retx_count += 1;
```

`UPT_LAT_UPDATE` (in `upt_helpers.h`) is a 64-bit min/max/sum accumulator;
`stats_utils.h`'s `STATS_UPDATE` was not reused because it is 32-bit and these
latencies exceed 4.29 s in practice.

Consumer:

```python
avg_ms = (latency_sum_ns / count) / 1e6
# also exported: latency_min_ms, latency_max_ms, sdus, retx
```

**Semantics: time from SDU arrival at RLC to the start of its transmission.**
It is a queuing delay, not an over-the-air delay.

### 3.3 `rlc_buffer` — RLC SDU-queue occupancy

* Files: `rlc_buffer_enq.cpp`, `rlc_buffer_deq.cpp`, `rlc_buffer_common.h`
* Proto: `rlc_buffer_stats.proto`
* Hooks: **`rlc_dl_new_sdu`** (grows) and **`rlc_dl_sdu_send_completed`** (drains)

Occupancy is **sampled directly** from the live queue state that srsRAN puts in
every RLC hook context (§4.3):

```c
UPT_RLCB_GET_QUEUE(rlc_ctx, qi);     // picks am_tx / um_tx / tm_tx union arm
if (qi) UPT_RLCB_SAMPLE(out->stats[i], qi);
```

`UPT_RLCB_SAMPLE` records `samples++`, `queue_bytes_last`, `queue_bytes_max`,
`queue_bytes_sum`, `queue_pkts_last`, `queue_pkts_max`. `enq_pkts` / `deq_pkts`
are counted separately so arrival/drain behaviour stays visible.

Consumer:

```python
avg_bytes = queue_bytes_sum / samples      # mean occupancy over the bucket
# also exported: bytes(last), bytes_max, pkts, pkts_max, enq_pkts, deq_pkts
```

> **Why direct sampling and not cumulative (enq − deq):** the reference design
> (deck slides 71–75) integrates `enqueued − dequeued` per bucket because that
> fork does not expose queue depth. Ours does. An integrated counter drifts
> **permanently** if a single event is missed, a bearer is re-established, or the
> codelet is loaded mid-flow; a direct sample is self-correcting. `enq/deq`
> counts are still reported, so the integrated view remains reconstructable.

**Shared state:** the two codelets share `rlcb_stats_map`, `rlcb_bucket_map` and
`rlcb_hash` via `linked_maps` in `upt.yaml`. Only the **enq** codelet declares
the output channel and flushes; **deq** only accumulates. Under DL load new SDUs
arrive continuously so the flush is prompt; the final bucket of a flow is
emitted when the next SDU arrives.

---

## 4. srsRAN provenance (what to verify)

All paths relative to `ocudu-jbpf/` (the live RAN submodule). Line numbers are
against the commit this branch records — see
[`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md) §1.

> `jrtc-apps/containers/Docker/srsRAN_Project/` is a **legacy** vendored srsRAN
> tree that is no longer built or deployed, and it has drifted from `ocudu-jbpf`
> (different `jbpf_rlc_ctrl_info` layout, `>=` vs `>` in the byte check). Do not
> read provenance from it.

Hook declarations (signatures + how args are packed into the context):
`srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h`
Context structs: `include/srsran/jbpf/jbpf_srsran_contexts.h`

### 4.1 `pdcp_dl_new_sdu` → gtp_arrival

| What | Where |
|---|---|
| `DEFINE_JBPF_HOOK(pdcp_dl_new_sdu)` | `lib/pdcp/pdcp_entity_tx.cpp:21` |
| call site | `lib/pdcp/pdcp_entity_tx.cpp:399` |
| enclosing function | **`void pdcp_entity_tx::handle_sdu(byte_buffer buf)`** |

```cpp
CALL_JBPF_HOOK(hook_pdcp_dl_new_sdu, st.tx_next, buf.length());
```

→ fires once per DL SDU entering PDCP TX. `buf.length()` is the byte count the
codelet accumulates.

### 4.2 `rlc_dl_sdu_send_started` → rlc_queueing  ← **the key one to verify**

| What | Where |
|---|---|
| `DEFINE_JBPF_HOOK` | `lib/rlc/rlc_tx_am_entity.cpp:22` |
| AM call site | `lib/rlc/rlc_tx_am_entity.cpp:414` |
| enclosing function | **`size_t rlc_tx_am_entity::build_new_pdu(span<uint8_t> rlc_pdu_buf)`** |
| UM call site | `lib/rlc/rlc_tx_um_entity.cpp:205` |
| TM call site | `lib/rlc/rlc_tx_tm_entity.cpp:137` |

The latency is computed by the gNB immediately before the hook fires
(`rlc_tx_am_entity.cpp:411-414`):

```cpp
auto latency = std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::high_resolution_clock::now() - sdu_info.time_of_arrival);
CALL_JBPF_HOOK(hook_rlc_dl_sdu_send_started,
               sdu_info.pdcp_sn.value(), sdu_info.is_retx,
               (uint64_t)latency.count());
```

The **t0** is stamped when the SDU arrives at RLC
(`lib/rlc/rlc_tx_am_entity.cpp:165`, inside
`rlc_tx_am_entity::handle_sdu(byte_buffer sdu_buf, bool is_retx)`):

```cpp
sdu.time_of_arrival = std::chrono::steady_clock::now();
```

and is carried into the TX-window entry at `:406`
(`sdu_info.time_of_arrival = sdu.time_of_arrival;`).

So: **latency_ns = (start of PDU build) − (SDU arrival at RLC)**, in ns.

Two caveats a reviewer should note:

1. All of this is inside `#ifdef JBPF_ENABLED`. The gNB must be built with jbpf
   enabled (it is — `ocudu-jbpf/build.sh`), or `latency_ns` is never produced.
2. Both ends of the interval use `std::chrono::steady_clock`
   (`time_of_arrival` is declared `std::chrono::time_point<std::chrono::steady_clock>`
   at `lib/rlc/rlc_tx_am_entity.h:39`), so the difference is a monotonic duration
   and is immune to wall-clock steps. Note this is **not** the same clock as
   `jbpf_time_get_ns()`, which the codelets use for bucket ids — so `latency_ns`
   is a duration only, never an absolute timestamp.

Related hooks carrying the same latency at later stages, available if you want
completion/delivery latency instead of queuing latency:
`rlc_dl_sdu_send_completed` (`:465`, `:631`) and `rlc_dl_sdu_delivered`
(`:912`, `:914`).

### 4.3 `rlc_dl_new_sdu` / `rlc_dl_sdu_send_completed` → rlc_buffer

| What | Where |
|---|---|
| `DEFINE_JBPF_HOOK(rlc_dl_new_sdu)` | `lib/rlc/rlc_tx_am_entity.cpp:18` |
| AM call site | `lib/rlc/rlc_tx_am_entity.cpp:196` |
| enclosing function | **`void rlc_tx_am_entity::handle_sdu(byte_buffer sdu_buf, bool is_retx)`** |
| `DEFINE_JBPF_HOOK(rlc_dl_sdu_send_completed)` | `lib/rlc/rlc_tx_am_entity.cpp:23` |
| AM call sites | `lib/rlc/rlc_tx_am_entity.cpp:465`, `:631` |
| UM equivalents | `lib/rlc/rlc_tx_um_entity.cpp:113`, `:260` |
| TM equivalents | `lib/rlc/rlc_tx_tm_entity.cpp:89`, `:174` |

**Queue depth is populated by the gNB's own `CALL_JBPF_HOOK` macro** — i.e. it is
attached to *every* RLC hook invocation, not just these two
(`lib/rlc/rlc_tx_am_entity.cpp:31-49`):

```cpp
#define CALL_JBPF_HOOK(hook_fn, ...)   {                    \
  struct jbpf_rlc_ctx_info jbpf_ctx = {0};                  \
  jbpf_ctx.ctx_id      = 0;                                 \
  jbpf_ctx.gnb_du_id   = (uint64_t)gnb_du_id;               \
  jbpf_ctx.du_ue_index = ue_index;                          \
  jbpf_ctx.is_srb      = rb_id.is_srb();                    \
  jbpf_ctx.rb_id       = ...;                               \
  jbpf_ctx.direction   = JBPF_DL;                           \
  jbpf_ctx.rlc_mode    = JBPF_RLC_MODE_AM;                  \
  jbpf_ctx.u.am_tx.sdu_queue_info = {                       \
      true,                                                 \
      sdu_queue.get_state().n_sdus,      /* num_pkts  */    \
      sdu_queue.get_state().n_bytes};    /* num_bytes */    \
  jbpf_ctx.u.am_tx.window_info = {                          \
      true, (uint32_t)tx_window.size(), tx_window_bytes};   \
  hook_fn(&jbpf_ctx, ##__VA_ARGS__);                        \
}
```

So `queue_bytes_*` traces directly to **`sdu_queue.get_state().n_bytes`** — the
live RLC SDU queue, sampled at the instant of the event. `jbpf_queue_info_t` is
`{uint8_t used; uint32_t num_pkts; uint32_t num_bytes;}`.

### 4.4 UE identity — read this before correlating panels

| Layer | Field | Struct |
|---|---|---|
| RLC (`rlc_queueing`, `rlc_buffer`) | `du_ue_index` | `jbpf_rlc_ctx_info` |
| PDCP (`gtp_arrival`) | `cu_ue_index` | `jbpf_pdcp_ctx_info` |

**These are different index spaces** (DU-side vs CU-side). With 2 UEs they
happened to line up (0↔0, 1↔1) in every run observed, but that is not
guaranteed. Both are exported as the Grafana label `ue`, so a mismatch would
silently mis-attribute the GTP panel relative to the two RLC panels.

For a rigorous mapping, load the existing `ue_contexts` codeletset
(`jrtc-apps/codelets/ue_contexts/` — `du_ue_ctx_creation`, `cucp_uemgr_ue_add`,
`e1_cuup_bearer_context_setup`) and resolve with
`jrtc-apps/jrtc_apps/libs/ue_contexts_map.py` (`UeContextsMap`). **Not done here** —
flagged as the main known gap.

---

## 5. Hook availability (why the design deviates from the deck)

The deck (slides 24–26) lists hooks added to a different fork. Enumerated from
`srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h` in *this* tree:

```bash
grep -c 'DECLARE_JBPF_HOOK' srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h   # 64
grep -o 'DECLARE_JBPF_HOOK([a-z0-9_]*' ... | grep -i gtp                     # (none)
```

| Deck hook | Present here? | Used instead |
|---|---|---|
| `gtpu_dl_enq` / `gtpu_dl_deq` | **No** | `pdcp_dl_new_sdu` |
| `rlc_dl_internal_enq` / `_deq` | **No** | `rlc_dl_new_sdu` / `rlc_dl_sdu_send_completed` |
| `pdcp_dl_sdu_segment` | **No** | n/a |
| `sdap_dl_*` | **No** | n/a |

To get true GTP-U-level timestamps: add a `gtpu_dl_enq` hook to the RAN, rebuild
the gNB image (`ocudu-jbpf/build.sh`, then re-import and re-patch the pod — see
[`RLC-CTRL-CHANGES.md`](../RLC-CTRL-CHANGES.md) §6), and change **only**
`hook_name` in `upt.yaml` —
`gtp_arrival.cpp` needs no change beyond the context struct if the new hook
carries a length.

---

## 6. File map

| File | Role |
|---|---|
| `jrtc-apps/codelets/upt/upt_helpers.h` | bucket constants, map macros, 64-bit latency accumulator |
| `jrtc-apps/codelets/upt/gtp_arrival.cpp` | codelet 1 |
| `jrtc-apps/codelets/upt/rlc_queueing.cpp` | codelet 2 |
| `jrtc-apps/codelets/upt/rlc_buffer_common.h` | shared sampling macros for codelet 3 |
| `jrtc-apps/codelets/upt/rlc_buffer_enq.cpp` | codelet 3a (owns output channel) |
| `jrtc-apps/codelets/upt/rlc_buffer_deq.cpp` | codelet 3b (linked maps) |
| `jrtc-apps/codelets/upt/rlc_pkt_record.cpp` | codelet 4 — per-packet records (§8) |
| `jrtc-apps/codelets/upt/*.proto` / `*.options` | wire format; `max_count` **must** equal `UPT_MAX_UE_RB` |
| `jrtc-apps/codelets/upt/upt.yaml` | codeletset: hooks, output channels, `linked_maps` |
| `jrtc-apps/codelets/upt/Makefile` | `PROTO_AND_SCHEMA` for the 3 protos |
| `jrtc-apps/jrtc_apps/upt/upt_app.py` | consumer → InfluxDB line protocol |
| `jrtc-apps/jrtc_apps/upt/deployment_fixed.yaml` | jrtc deployment (app + codeletset) |

Build: `cd jrtc-apps/codelets && ./make.sh -d upt`
Each codelet must print `Program terminates within N instructions` (verifier OK).
Measured 2026-09-27: gtp_arrival **348**, rlc_queueing **411**, rlc_buffer_enq
**450**, rlc_buffer_deq **419**, rlc_pkt_record **215**.

---

## 7. Verification checklist

```bash
# 1. codelets compile + pass the jbpf verifier
cd jrtc-apps/codelets && rm -f upt/*.o && ./make.sh -d upt

# 2. all five upt codelets registered in the gNB (expect 5)
#    NB: registration lines go to gnb.stdout; the gNB logger writes gnb.log
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  bash -c 'grep -ac "Registered codelet" /tmp/gnb.stdout'

# 3. codeletset accepted
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  bash -c 'grep -a "Codeletset is loaded OK" /tmp/gnb.stdout'

# 4. metrics present (23 series names -- see the grafana doc §1)
curl -s localhost:30491/api/v1/label/__name__/values
```

**Internal-consistency check** (the strongest evidence the three agree). With
DL-only iperf3 (`-R`) on 2 UEs, observed:

| Quantity | UE0 | UE1 |
|---|---|---|
| GTP arrival, peak | 54.2 Mbps | 31.5 Mbps |
| Sustained DL goodput (iperf3) | 3.61 Mbps | 2.89 Mbps |
| RLC buffer, peak | 2.74 MB | 2.87 MB |
| RLC queuing latency, peak | 5.64 s | 5.99 s |

Cross-check: `2.74 MB × 8 / 3.61 Mbps ≈ 6.1 s` vs **5.64 s** measured. The
buffer and latency codelets are independent (different hooks, different maps),
so their agreement to ~10% is meaningful.

These values are **not** anomalies: the gNB's configured limit is
`rlc_queue_bytes_limit=6172672` (~6 MB, visible in `/tmp/gnb.log`), so a ~2.7 MB
standing queue drained at ~3.6 Mbps *is* multi-second bufferbloat. This is the
effect the L4S/L4Span work targets.

### Known gaps / things a reviewer should push on

1. **`du_ue_index` vs `cu_ue_index` not reconciled** (§4.4) — the GTP panel's UE
   label comes from a different index space than the two RLC panels.
2. **GTP arrival is measured at PDCP**, so it excludes anything dropped before
   PDCP and is offset by the SDAP/PDCP entry cost.
3. **Bucket width is 134.217728 ms, not 100 ms.** Any rate derived from these
   messages must divide by the true width. The constant is duplicated in
   `upt_helpers.h` (`UPT_BUCKET_SHIFT`) and `upt_app.py` — **changing one without
   the other silently scales every rate**.
4. **`rlc_buffer` flushes on the enqueue hook only**, so the last bucket of a
   flow is emitted only when the next SDU arrives.
5. **SRB traffic is included** (`is_srb` is exported). The dashboard's `bearer`
   template variable defaults to **All**, so SRB series appear alongside DRB
   ones until you narrow it.

### Operational traps (all hit during development)

* `max_count` in the `.options` files **must equal** `UPT_MAX_UE_RB`. Oversized
  protos + a deep ringbuf make the codeletset descriptor large enough that the
  LCM IPC load times out and **kills the gNB's jbpf agent**; every later load
  then fails with `Error connecting to /tmp/jbpf/jbpf_lcm_ipc: Connection
  refused` until the **gNB is restarted**.
* `make` does **not** track header dependencies. After editing `upt_helpers.h`,
  `rm -f upt/*.o` — otherwise codelets link shared maps of mismatched size.
* **Never start a thread in a jrtc Python app.** jrtc runs apps in Python
  sub-interpreters and calls `Py_EndInterpreter` on unload, which aborts the
  process (`Fatal Python error: Py_EndInterpreter: not the last thread`,
  SIGSEGV) if any other thread is alive. `upt_app.py` is single-threaded by
  necessity and flushes on the app timeout callback.

---

## 8. Per-packet records (lossless, batched)

`rlc_pkt_record.cpp` — the aggregating codelets (§3) measure per packet but
export ~134 ms summaries, discarding PDCP SN and the latency distribution. This
codelet keeps **one record per packet** and batches them, giving lossless
per-packet fidelity at ~batch× lower IO than streaming each packet (which is
what exhausted the mempool in the original design, deck slides 67-70).

* File: `jrtc-apps/codelets/upt/rlc_pkt_record.cpp`
* Proto: `rlc_pkt_records.proto` (message `rlc_pkt_records` = `repeated t_rlc_pkt pkts`)
* Hook: **`rlc_dl_sdu_send_started`** (same hook as `rlc_queueing`; both run)
* Record: `{du_ue_index, is_srb, rb_id, pdcp_sn, latency_ns, is_retx, queue_bytes}`

Batching (`UPT_PKT_BATCH = 64`, must equal `max_count` in the `.options`):

```c
bool bucket_rolled = (*last_bucket != 0) && (*last_bucket != bucket);
if (out->pkts_count >= UPT_PKT_BATCH || bucket_rolled) {
    if (out->pkts_count > 0) { out->timestamp = now;
        jbpf_ringbuf_output(&out_rlc_pkts, out, sizeof(*out)); out->pkts_count = 0; }
}
... out->pkts[i % UPT_PKT_BATCH] = record; out->pkts_count = i + 1;
```

Flush on **full OR bucket boundary** — the boundary bounds staleness to ~one
bucket under light load, which matters because this stream is the sensor for the
buffer control loop (§ control-loop, separate doc).

`queue_bytes` is the live RLC SDU-queue depth at send time (free from the ctx,
§4.3). `bytes`-per-SDU is deliberately absent: join to the arrival stream by
`(ue, rb, pdcp_sn)` in the xApp — the slide-66 PDCP-SN join.

### xApp handling (three tiers)

`jrtc_apps/upt/upt_app.py`, `handle_rlc_pkts()`:

1. **Archive (DB tier)** — every batch appended to `/tmp/upt_pkt_records.ndjson`
   (`UPT_PKT_LOG`), one JSON object per batch, for offline joins / training.
2. **Rolling window** — last `PCTL_WINDOW` (2048) latencies per UE, in memory.
3. **Percentile export** — `upt_rlc_pkt_lat_{p50,p95,p99,max}_ms` to VM, the
   control-facing tail the aggregate mean cannot give.

This is the three-tier design: per-packet in RAN → batched to xApp for fast
inference/control → file for long-term. VM holds only percentiles (a metrics
TSDB is the wrong store for per-packet events; the NDJSON file is the archive).

### Verified

2 UEs, 25 s DL: **18011 packets in 474 batches (~38/batch, ~38× fewer IO ops)**,
lossless. Percentiles p50 ≈ 5.6/6.2 s, **p99 ≈ 6.7/7.4 s** — a ~1 s tail the
aggregate mean hid. Dashboard: p95/p99 overlaid on the RLC Queuing Latency panel.

Same operational traps as §7: `UPT_PKT_BATCH` must equal `max_count`;
`rm -f upt/*.o` after header edits.
