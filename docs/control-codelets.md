# Control codelets

Most jbpf codelets only *observe*: a hook hands them a read-only context, they emit telemetry.
A **control** hook is different — it hands the codelet a writable, srsRAN-owned struct, and
whatever the codelet writes back is what the RAN then does. Four such hooks exist on this gNB.

All four are **no-ops until a codelet is loaded on them**, and each is **exclusive**: only one
codelet may attach to a given control hook at a time.

| Hook | Actuates | Fires | Codelets |
|---|---|---|---|
| `rlc_dl_ctrl` | DL RLC queue byte / SDU limit, per `(UE, DRB)` | per DL packet on the bearer | `bufctl`, `bufcap`, `bufdyn_ctl` |
| `mac_sched_dl_ctrl` | per-UE DL PRB-share cap | per UE, per slot, at the PRB-grant point | `dl_sched` (`edgeric_sched`) |
| `mac_sched_dl_mcs_ctrl` | per-UE DL MCS | per newTx UE, at link adaptation | `dl_mcs` (`edgeric_mcs`), `badchan` |
| `pdcp_dl_sdu_segment` | ECN bits of the DL IP packet, in place | per DL DRB SDU segment | `l4span_mark` |

Hook declarations live in `ocudu-jbpf/srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h`; the
context structs in `ocudu-jbpf/include/srsran/jbpf/jbpf_srsran_contexts.h`.

Throughout, `JRTC` is the helper from README §8:

```bash
JRTC() { kubectl exec -n ran jrtc-0 -c jrtc -- bash -c \
  "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl $*"; }
```

---

## 1. RLC DL buffer size — `rlc_dl_ctrl`

Sets the downlink RLC queue's byte and/or SDU-count limit for one `(UE, DRB)` at runtime. This
is the bufferbloat knob: a deep queue lets a loss-based sender build a multi-second standing
backlog, and capping it trades a little throughput for a large latency reduction.

Full hook API, mechanism and clamping rules: [`rlc-buffer-control-hook.md`](scout/rlc-buffer-control-hook.md).
Guided walk-throughs: [`open-ai-ran-tutorial/demo1`](open-ai-ran-tutorial/demo1/README.md) (one UE,
fixed caps) and [`demo2`](open-ai-ran-tutorial/demo2/README.md) (two UEs, per-UE limits).

### Context — `struct jbpf_rlc_ctrl_info`

```c
uint16_t du_ue_index;      /* -> codelet */
uint8_t  is_srb;           /* -> codelet: always 0 (layout compatibility) */
uint8_t  rb_id;            /* -> codelet: DRB id */
uint32_t cur_byte_limit;   /* -> codelet: the queue's current byte limit */
uint32_t new_byte_limit;   /* <- codelet: desired byte limit (0 = leave unchanged) */
uint32_t cur_sdu_limit;    /* -> codelet: current SDU-count limit */
uint32_t new_sdu_limit;    /* <- codelet: desired SDU-count limit (0 = leave unchanged) */
uint32_t cfg_byte_limit;   /* -> codelet: CONFIGURED byte limit; write back to reset */
uint32_t cfg_sdu_limit;    /* -> codelet: CONFIGURED SDU limit (queue capacity) */
```

Two details matter. Writing `0` means *leave unchanged*, so a codelet can set a byte limit
without touching the SDU limit. And `cfg_*` carries the originally configured sizes — writing
those back is the only way to undo a cap (see the gotcha below).

### Three codelets, three styles

| Codelet | Style | Driven by |
|---|---|---|
| `bufctl` | interactive / xApp-commanded, per `(UE, DRB)` | `jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py` |
| `bufcap` | one fixed cap per object (`bufcap_256k.o`, …), cell-wide | `buffer-control-experiments/bufcap.sh` |
| `bufdyn_ctl` | dynamic: cap = µ(MCS) × target window | loaded as `dyn` by `bufcap.sh`; observes via `bufdyn_obs` on `mac_sched_harq_dl` |

### Driving it

Interactive, per UE and per DRB — run on the host, from the clone the Helm release points at:

```bash
python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py
```

```
scan                    # ask UE 0..N / DRB 1..M to report; discovers live bearers
list                    # table: UE, DRB, current limit, configured limit, last event
set 0 1 byte 200000     # cap UE 0 DRB1 at 200 kB
set 0 1 sdu 512         # or an SDU count
reset 0 1               # write cfg_* back
quit                    # unloads the codelet
```

Fixed caps, whole cell:

```bash
bash buffer-control-experiments/bufcap.sh build          # off 16k 64k 256k 1m
bash buffer-control-experiments/bufcap.sh load 256k
bash buffer-control-experiments/bufcap.sh off            # restore configured size
```

### Verify

```bash
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  grep -ao "rlc_dl_ctrl changed .*limit\. old=[0-9]* new=[0-9]*" /tmp/gnb.log | tail -3
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -c "Dropped SDU" /tmp/gnb.log
```

Over-tight requests are **clamped, not rejected** — the gNB enforces a floor of one
maximum-size PDCP PDU. Drops appear as `Dropped SDU`.

### Gotcha — a cap outlives its codelet

The gNB keeps the last limit it was given. Unloading the codelet stops new writes; it does not
restore anything. So `unload` (or `quit`) leaves your cap in force. To actually undo it, the
configured values must be written back:

- `bufctl`: `reset <ue> <drb>` for each bearer *before* `quit`
- `bufcap`: `off`, not `unload` — and it needs **downlink traffic running** to take effect,
  because the write only lands on the bearer's next DL packet

A gNB restart also clears it, since the limits die with the process.

---

## 2. DL scheduling, PRB share — `mac_sched_dl_ctrl`

EdgeRIC-style weighted scheduling: converts a per-UE weight into a per-UE cap on how many PRBs
that UE may take in a slot.

It fires at the **PRB-grant point** — `intra_slice_scheduler::schedule_dl_newtx_candidates`
(`ocudu-jbpf/lib/scheduler/ue_scheduling/intra_slice_scheduler.cpp:561`), per UE per slot — and
overrides the grant's `max_grant_size`. That location is deliberate: it is
scheduler-policy-agnostic, so the control works regardless of which policy (RR, PF) picked the
candidate order, and it is EdgeRIC's real weight→PRB-share mechanism rather than a priority
nudge. The override applies **only** when the codelet sets `new_rbs_valid`; otherwise the fair
RB cap stands and behaviour is stock.

> The comment above the hook declaration claims it fires at the end of
> `scheduler_time_pf::ue_ctxt::compute_dl_prio`. That is not where the call is. See
> *Known documentation defects* below.

### Context — `struct jbpf_mac_sched_dl_ctrl_info`

```c
uint16_t rnti;             /* -> codelet */
uint16_t du_ue_index;      /* -> codelet */
uint32_t tti;              /* -> codelet: RANtime slot counter */
uint32_t cqi;              /* -> codelet: wideband CQI */
uint32_t dl_backlog_bytes; /* -> codelet: pending DL newTx bytes */
uint32_t cur_max_rbs;      /* -> codelet: fair per-UE RB cap this slot (reference) */
uint32_t new_rbs_valid;    /* <- codelet: nonzero = apply new_max_rbs */
uint32_t new_max_rbs;      /* <- codelet: per-UE RB-share cap */
```

The `valid` flag is the pattern all three MAC/RLC control hooks share: write the value *and*
set its `*_valid` companion, or the write is ignored.

### Driving it

```bash
JRTC 'load -c /apps/edgeric/dl_sched_deployment.yaml'
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'echo "fixed:20,1,1,1" > /tmp/edgeric_policy'
kubectl logs -n ran jrtc-0 -c jrtc --tail=5 | grep edgeric_sched
JRTC 'unload -c /apps/edgeric/dl_sched_deployment.yaml'
```

Policies in `/tmp/edgeric_policy` inside `jrtc-0`:

- `maxweight` — weight = CQI × backlog
- `fixed:w0,w1,w2,w3` — static per-UE weights

Weights are relative, so `fixed:20,1,1,1` gives UE0 roughly 20/23 of the PRBs; the xApp log
shows the resulting caps (e.g. `92/5/5/5`).

---

## 3. DL MCS override — `mac_sched_dl_mcs_ctrl`

Overrides the DL MCS that link adaptation chose. Fires per newTx UE at the PDSCH MCS-selection
point in `grant_params_selector`, i.e. *after* the CQI-driven choice has been made, so the
codelet sees that choice and can replace it.

### Context — `struct jbpf_mac_sched_dl_mcs_ctrl_info`

```c
uint16_t rnti;             /* -> codelet */
uint16_t du_ue_index;      /* -> codelet */
uint32_t tti;              /* -> codelet: RANtime slot counter */
uint32_t cqi;              /* -> codelet: wideband CQI */
uint32_t mcs_table;        /* -> codelet: 0 qam64, 1 qam256, 2 qam64LowSe */
uint32_t cur_mcs;          /* -> codelet: the MCS link adaptation picked (reference) */
uint32_t new_mcs_valid;    /* <- codelet: nonzero = apply new_mcs */
uint32_t new_mcs;          /* <- codelet: DL MCS override (0..31) */
```

### Two codelets

**`dl_mcs`** (`edgeric_mcs`) — policy-driven override:

```bash
JRTC 'load -c /apps/edgeric/dl_mcs_deployment.yaml'
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'echo "fixed:5" > /tmp/edgeric_mcs_policy'
kubectl logs -n ran jrtc-0 -c jrtc --tail=3 | grep edgeric_mcs
JRTC 'unload -c /apps/edgeric/dl_mcs_deployment.yaml'
```

Policies in `/tmp/edgeric_mcs_policy`:

| Policy | Effect |
|---|---|
| `passthrough` | no override — stock link adaptation (default) |
| `fixed:N` | force MCS N on every UE |
| `fixed:m0,m1,m2,m3` | per-UE MCS |
| `offset:K` | `cur_mcs + K` per UE (K may be negative), clamped to 0..28 |

**`badchan`** — "bad radio" emulation for experiment B. Caps every UE's DL MCS at a ceiling so
each PRB carries fewer bits and the UE's rate falls, as if the channel had faded. It only ever
*lowers* the MCS. Driven by `buffer-control-experiments/radio.sh`:

```bash
bash buffer-control-experiments/radio.sh 10          # sustained: cap at MCS <= 10 (~9.5 Mbit/s)
bash buffer-control-experiments/radio.sh 10 fade     # dip to MCS <= 10 for 100 ms every 400 ms
bash buffer-control-experiments/radio.sh trace ped   # replay a pedestrian channel trace
bash buffer-control-experiments/radio.sh good        # unload: back to the real channel
```

Verify the override is landing — the QAM order in the PDSCH log drops with the MCS:

```bash
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  bash -c 'grep -aoE "PDSCH: .* mod=[0-9]+QAM" /tmp/gnb.log | tail -3'
```

`dl_mcs` and `badchan` both want this hook, so they cannot be loaded together.

---

## 4. L4S ECN marking — `pdcp_dl_sdu_segment`

In-RAN L4S: mark the downlink IP packet's ECN bits so the *sender* backs off, instead of
letting the queue grow or dropping packets. Unlike the other three hooks, the writable object
is the packet itself — `ctx.data` is the first segment of the DL IP packet (IP header plus the
start of the payload), and the modified bytes are what go to the lower layers.

```c
ctx.data           = writable first segment of the DL IP packet
ctx.data_end       = ctx.data + pkt_len
ctx.srs_meta_data1 = ((uint64_t)ue_index << 32) | rb_id
ctx.srs_meta_data2 = count
```

### Two codelets, split signal and action

`l4span` is a codeletset of two parts that share an `l4s_map`:

- **`l4span_rate`** — the *signal*, on the observation hook `rlc_dl_sdu_send_completed`
  (whose `srs_meta_data2` carries this SDU's RLC queuing delay in ns). Per `(UE, DRB)` it
  EWMAs the queuing delay and maps it proportionally to a marking probability (per-mille),
  publishing it in `l4s_map`. Proportional-and-capped control settles at the *minimum* marking
  that holds the queue near target, which is what preserves throughput.
- **`l4span_mark`** — the *action*, on `pdcp_dl_sdu_segment`. For ECT IPv4 packets it sets
  ECN CE at the published probability and fixes the IPv4 checksum incrementally (RFC 1624).

```bash
JRTC 'load -c /apps/l4span/deployment.yaml'
JRTC 'unload -c /apps/l4span/deployment.yaml'
```

### Gotcha — the two halves must address the same bearer

They key the shared map as `slot = ((ue << 4) | (rb & 0xF)) % 64`, but reach it from different
layers: the mark side has the **CU-UP PDCP** `ue_index`, the signal side the **DU RLC**
`du_ue_index`. These are equal on this gNB only because both are assigned in attach order
(verified via the F1/E1 logs). If that ever stops holding — a different CU-UP, or reordered
index assignment — the two halves will silently act on different UEs. Nothing detects it.

---

## Rules that apply to all four

**One codelet per hook.** The exclusivity is per hook, so the practical conflicts are:

- `rlc_dl_ctrl` — `bufctl` vs `bufcap` vs `bufdyn_ctl`
- `mac_sched_dl_mcs_ctrl` — `dl_mcs` vs `badchan`

Loading a second codelet on an occupied hook fails. If demo 1 left a `bufcap` cap loaded,
`bash buffer-control-experiments/bufcap.sh off` clears it before you start `bufctl_cli.py`.

**They run on the fast path.** `rlc_dl_ctrl` fires on every downlink packet of the bearer,
`mac_sched_dl_ctrl` per UE per slot, `pdcp_dl_sdu_segment` per DL SDU segment. Unload a control
codelet when you are not actively commanding it. `bufctl_cli.py` unloads on `quit` for exactly
this reason.

**Writes need their `*_valid` flag** (`new_rbs_valid`, `new_mcs_valid`), except `rlc_dl_ctrl`,
which uses `0 = leave unchanged` instead.

**Actuation is asynchronous.** A command lands on the bearer's *next* matching event, not when
you type it. A bearer carrying no downlink traffic will not pick up a new limit at all — which
is why `bufcap.sh off` requires traffic, and why a starved UE in a multi-UE run can appear to
ignore a cap entirely.

**Confirm from the gNB, not the xApp.** The xApp log says what was *requested*; `/tmp/gnb.log`
says what was *applied*, after clamping.

---

## Known documentation defects

Both are in the same place: the comment above `DECLARE_JBPF_HOOK(mac_sched_dl_ctrl, …)` in
`jbpf_srsran_hooks.h`. Trust the struct and the call site, not that comment.

1. **A field that does not exist.** It says a control codelet "may WRITE `new_prio_bits`".
   `struct jbpf_mac_sched_dl_ctrl_info` has no such field — it exposes `new_rbs_valid` and
   `new_max_rbs`.
2. **The wrong fire location.** It says the hook fires "at the end of
   `scheduler_time_pf::ue_ctxt::compute_dl_prio`". The only call to
   `hook_mac_sched_dl_ctrl` is in `intra_slice_scheduler::schedule_dl_newtx_candidates`
   (`intra_slice_scheduler.cpp:561`), at the PRB-grant point.

Both read as leftovers from an earlier design that nudged scheduler *priority* and was later
reworked into a PRB-share cap applied at grant time. A one-line fix in the submodule would
settle it.

## See also

| Document | Covers |
|---|---|
| [`../rlc-buffer-control-hook.md`](scout/rlc-buffer-control-hook.md) | `rlc_dl_ctrl` in depth: mechanism, API, clamping |
| [`../buffer-control-experiments/EXPERIMENTS.md`](../buffer-control-experiments/EXPERIMENTS.md) | the buffer/channel experiments and measured results |
| [`../buffer-control-experiments/HOW_TO_RUN.md`](../buffer-control-experiments/HOW_TO_RUN.md) | commands-only guide for experiments A and B |
| [`open-ai-ran-tutorial/demo1/README.md`](open-ai-ran-tutorial/demo1/README.md) | 1 UE, fixed buffer caps, bufferbloat |
| [`open-ai-ran-tutorial/demo2/README.md`](open-ai-ran-tutorial/demo2/README.md) | 2 UEs, per-UE limits, BBR vs CUBIC |
| README §10 | the original quick reference these notes expand on |
