# RLC DL buffer control (`rlc_dl_ctrl`): fixes and open items

Status: **merged** into this tutorial; the gNB side is `ocudu-jbpf` commit `2920811`.
Last updated: 2026-09-10.

Please review sections 2–4. **Section 5 tracks every open item** (moving control to PDCP is item 2). Section 6 describes
work that is designed but not yet written.

This file is the **development log**: what changed at each stage, the decisions behind it, and what is still open. The
hook itself — what it does, its API, error handling and example xApps — is documented separately in
[`rlc-buffer-control-hook.md`](./rlc-buffer-control-hook.md).

**Status (2026-09-16)**

| Part | State |
|---|---|
| Fix A: per-(UE, DRB) targeting, hook called for DRBs only (§2) | ✅ Committed and pushed |
| Fix B: limit clamping in the gNB (§3) | ✅ Committed and pushed |
| Decisions on reset and unload (item 1) | ✅ Taken on 2026-09-14; see item 1 |
| `SET` / `GET` / `RESET` ops + report channel in `rlc_ctrl` (§6) | ✅ Coded; codelet builds and passes the verifier (892 instructions) |
| `cfg_byte_limit` / `cfg_sdu_limit` in the hook context (§6) | ✅ Coded |
| `bufctl` codelet set, xApp and CLI (§6) | ✅ Coded; Python syntax-checked |
| Testbed run (gNB rebuild, UE attach, live traffic) | ✅ **Passed 2026-09-16** — results in §4 |
| Commit to git (both repos) | ✅ Committed: `ocudu-jbpf` @ `2920811` (gNB), and this repo (codelet, xApp, CLI) |
| Verified on the pushed binary | ✅ gNB rebuilt from `2920811`; `GET` returned `cur = cfg = 8,388,608 B / 8192 SDU` |
| Colleague-authored files untouched | ✅ `upt/rlc_ctrl.cpp`, `upt.yaml`, `upt/Makefile`, `erctrl.yaml`, `erctrl_app.py` byte-identical to the tutorial branch |

## 1. Background: how the control path works

```
xApp ──rlc_ctrl_msg──> jrtc ──> ctrl_in (jbpf control-input channel) ──> rlc_ctrl codelet
                                                                              │ writes ci.new_byte_limit / new_sdu_limit
rlc_tx_am_entity::handle_sdu ── hook_rlc_dl_ctrl(&ci) ◄───────────────────────┘
      └─> rlc_sdu_queue_lockfree: atomic byte_limit / sdu_limit ──> write(): tail-drop when over a limit
```

- Config sets the starting limits once, when the bearer is created. `queue-bytes` becomes the byte limit, and `queue-size`
  becomes the queue capacity and SDU limit. The config is never reloaded at run time.
- The design uses one hook (`rlc_dl_ctrl`) and one codelet (`rlc_ctrl`). These fixes add no new hooks or codelets.

## 2. Fix A: per-(UE, DRB) targeting

**Problem**

- **The UE was dropped from the key.** `rlc_ctrl` indexed its override table with
  `slot_of(ue, rb) = ((ue << 4) | (rb & 0xF)) % 16`. The `% 16` keeps only the low 4 bits, which hold the bearer, so
  the UE was discarded:
  - A command for UE 0 / DRB1 was applied to DRB1 of **every** UE.
  - DRB8 and above landed on the SRB0–3 slots.
- **No SRB filter.** The codelet's built-in default also changed SRB1/SRB2, raising their byte limit from 64 kB to 3.07 MB.
- **Built-in policy.** The codelet ran its own policy: a sweep, later a fixed 2048-SDU limit for all UEs. So loading
  it changed buffers even when no xApp had sent anything.

**Fix**

| Layer | Change |
|---|---|
| xApp | Chooses the target bearer and the value. The message layout is unchanged (20 bytes, uint32 LE): `{du_ue_index, is_srb, rb_id, byte_limit, sdu_limit}`, with `is_srb = 0` and `rb_id` = DRB id. |
| Codelet `jrtc-apps/codelets/bufctl/rlc_ctrl.cpp` | **Just applies what the xApp sent.** One array map `limit_map`, index `du_ue_index * 29 + (drb_id - 1)`, value `{byte_limit, sdu_limit}`. It replaces the two 16-slot maps. Messages with `is_srb != 0`, `du_ue_index >= 64` or `drb_id` outside `[1, 29]` are ignored. A 0 field keeps the previously stored value. Bearers with no command are left unchanged. The sweep and fixed default are removed. |
| gNB `ocudu-jbpf/lib/rlc/rlc_tx_am_entity.{h,cpp}` | The hook call moves into `handle_jbpf_rlc_ctrl()`, which runs **only for DRBs**. SRBs always keep their configured limits. |

Scope: RLC **AM** DRBs only, because the hook exists only in `rlc_tx_am_entity`. UM is TODO 7.

## 3. Fix B: limit sanity checks (gNB)

**Problem.** The gNB applied any non-zero value a codelet wrote:

- An SDU limit above capacity had no effect.
- A byte limit above `queue-bytes` was accepted, but PDCP admission still caps traffic at `queue-bytes`.
- A byte limit smaller than one packet (e.g. 1000 B) dropped every full-size packet, shutting the bearer off.
- Change detection compared against `ci.cur_*`, which the codelet can overwrite.

**Fix.** Clamping lives in `ocudu-jbpf/lib/rlc/rlc_sdu_queue_lockfree.h` (`clamp_byte_limit`, `clamp_sdu_limit`, used by
`set_*_limit`). Every codelet on the hook gets it, including `rlc_ctrl`, `rlc_fixed` and `rlc_perue`.

| Limit | Allowed range | Rationale |
|---|---|---|
| SDU count | `[1, capacity]` | capacity = configured `queue-size` = number of allocated SDU slots: 8192 via `telemetry/fresh_start_demo.sh`, 16384 in `configs/qos.yml` and the code default. 0 is never applied; it means "no change". |
| Bytes | `[9007, queue-bytes]` | 9007 = largest PDCP PDU (9000 B SDU + 3 B header + 4 B MAC-I), so an empty queue always accepts a packet. The upper bound is the configured value (8,388,608 via fresh_start). A higher limit would have no effect, because PDCP admission uses the configured value. |

Limits can only be tightened relative to the config, never widened. `handle_jbpf_rlc_ctrl()` compares the clamped value
with the queue's current value and changes the queue only when they differ. Each change logs one info line, e.g.
`rlc_dl_ctrl changed byte_limit. old=... new=... requested=...` (if `requested != new`, the value was clamped).

## 4. Behaviour changes and review notes

- **No default.** `rlc_ctrl` does nothing until an xApp sends a command. The previous uncommitted fixed-2048-SDU
  experiment is saved as `git stash` entry "wip: rlc_ctrl fixed 2048-SDU per-UE characterization". To reproduce it, send
  `{ue, 0, 1, 0, 2048}` for each UE.
- **0 means "no change" at every step.** A 0 field in an xApp message keeps the value the codelet already stored, and a
  0 written by the codelet leaves the gNB's limit unchanged.
  - Before this change, `byte_limit = 0` cleared the codelet's override, and the codelet fell back to its own built-in
    sweep or fixed value, never to the configured one.
  - How to restore the configured default, and what unloading should do, are open decisions (TODO 1).
- **`erctrl_app.py`.** Its SDU setpoints (45000…) now clamp to capacity instead of being ignored. They are still wrong
  (TODO 5).
- **`rlc_fixed` / `rlc_perue`.** No behaviour change: they already skip SRBs and all their values are in range.
- **Byte minimum.** 9007 B is a judgment call. It rules out byte caps below ~9 kB; the smallest demo option is 10 kB. If
  smaller byte caps are needed, 1507 (one 1500 B packet) is the alternative. **Reviewer decision.**
- **ABI.** The `jbpf_rlc_ctrl_info` layout is unchanged; `is_srb` is now always 0.
- **Legacy tree.** `jrtc-apps/containers/Docker/srsRAN_Project` is **not** changed (TODO 12).
- **Operational rule: unload the control set when you are not commanding.** While a codelet is attached to
  `rlc_dl_ctrl`, the hook runs on every downlink SDU of every AM DRB. Unloading leaves the applied limits in place
  (decision B1 in item 1), so the pattern is: load → set → unload → run. Reducing the cost *while* loaded is item 3.

**Files changed**

```
ocudu-jbpf/ (submodule)
  lib/rlc/rlc_sdu_queue_lockfree.h                 clamp_*_limit, clamping set_*_limit, cfg_byte_limit
  lib/rlc/rlc_tx_am_entity.h / .cpp                handle_jbpf_rlc_ctrl(): DRB-only hook call, clamp, change log
  include/srsran/jbpf/jbpf_srsran_contexts.h       comments (DRB-only, clamping)
  srsran_jbpf/verifier/specs/jbpf_srsran_hooks.h   comment
open-ai-ran-tutorial/
  jrtc-apps/codelets/bufctl/rlc_ctrl.cpp           per-(UE, DRB) executor (own directory)
  RLC-CTRL-CHANGES.md                              this file
```

**Testing**

- [x] `rlc_ctrl.cpp` compiles in the SDK image and passes `srsran_verifier_cli` (terminates within 930 instructions).
  The verifier also prints "Code is unreachable after 417:437", which the old codelet didn't. This is most likely the
  `continue` inside the unrolled drain loop; it doesn't fail verification.
- [x] gNB compile check: the `ocudu_rlc` library (`-DENABLE_JBPF=on`, same flags as `docker/Dockerfile`) builds with no new
  warnings.
- [x] **End to end on the testbed, 2026-09-16.** Rebuilt gNB (binary md5 `f26aefa2…`), UE 10.45.0.14 attached with a
  5QI 9 AM DRB (8192 SDU / 8,388,608 B), ~40 Mbit/s downlink iperf3, `bufctl` loaded. 17 commands sent, 0 failed,
  8 reports received.

  | Step | Command | gNB log line | Report to the xApp |
  |---|---|---|---|
  | GET | `get ue0 drb1` | — (reads only) | `cur` 8,388,608 B / 8192 SDU, `cfg` identical, `reason=get` |
  | SET | `sdus=256` | `changed sdu_limit. old=8192 new=256 requested=256` | `cur_sdus=256` |
  | SET | `bytes=200000` | `changed byte_limit. old=8388608 new=200000 requested=200000` | `cur_bytes=200000` |
  | SET | `sdus=1` (legal floor) | `changed sdu_limit. old=256 new=1 requested=1` | `cur_sdus=1` |
  | SET | `bytes=100` (below floor) | `changed byte_limit. old=200000 new=9007 requested=100` → **clamped up** | `cur_bytes=9007` |
  | RESET | `reset ue0 drb1` | `byte_limit … new=8388608`, `sdu_limit … new=8192` | `cur == cfg`, `reason=reset` |

  Only UE 0 / DRB 1 ever answered, which is correct: it is the only bearer carrying downlink traffic. The other 11 GETs
  (UE 0–3 × DRB 1–3) were silently ignored, as designed, and SRB limits were never touched.

  Worth knowing for future runs: with **no** downlink traffic nothing happens at all — no reports and no changes —
  because the hook only runs when a packet arrives. An earlier attempt looked like a total failure purely because the
  UPF's iperf3 server had died; use `scripts/traffic.sh start` and check that `TX SDU` in the gNB log is climbing before
  concluding anything about the control path.

## 5. Open items

P1 = correctness or blocks experiments · P2 = design · P3 = cleanup

- [ ] **1. (P1) Restoring the default, and what unloading a codelet does. Decided 2026-09-14; not yet implemented.**

  **Current behaviour** (with this change):

  | Action | Effect |
  |---|---|
  | xApp sends a field = 0 | The codelet keeps the value it already stored for that field. A stored command can't be removed. |
  | xApp sends a value ≥ the configured one (e.g. `0xFFFFFFFF`) | The gNB clamps it to `queue-bytes` / `queue-size`, which is the configured default. This works today, but only as a side effect of clamping. |
  | Unload the codelet set (`jrtc-ctl unload -c …` or `jrtc-apps/utils/unload_codeletSet.sh`) | jbpf destroys the codelet **and its maps**, so the stored commands are gone. jbpf has no unload callback, so no reset runs. The hook becomes a no-op and every bearer **keeps its last applied limits**. |
  | Reload the codelet set | The map starts empty, so nothing is applied until a new command arrives. Bearers keep the old limits. |
  | Load `rlc_fixed` / `rlc_perue` after `rlc_ctrl` | The byte limit is overwritten, but the SDU limit keeps `rlc_ctrl`'s value. |
  | UE re-attach or gNB restart | A new RLC entity is created with the configured limits. This is how the `scripts/*.sh` scripts get a clean state today. |

  **Constraints**
  - Only one codelet can attach to a control hook, so a separate "reset" codelet can't sit next to `rlc_ctrl`.
  - Maps live and die with their codelet set.
  - The gNB already stores each bearer's configured limits (`cfg_byte_limit`, `capacity` in `rlc_sdu_queue_lockfree`).

  **Question A: how does the xApp restore the default while `rlc_ctrl` is loaded?**
  - **A1. Explicit `RESET` op in the message.** The codelet writes a sentinel (e.g. `UINT32_MAX`) into `new_*`, and the gNB
    restores its stored config values. Small change and clear intent. The message gains an `op` field; add a version field
    too, so an old 20-byte message can't be misread.
  - **A2. Append `cfg_byte_limit` / `cfg_sdu_limit` to `jbpf_rlc_ctrl_info`** (layout-compatible). The codelet can then write
    the defaults on RESET and report them to the xApp, so the xApp knows each bearer's default and allowed range.
  - **A3. Keep the implicit trick** (send a value ≥ config). No code change, but it depends on clamping, and the xApp can't
    tell a real limit from "default".
  - **A4. Capture defaults into a shared map at bearer creation** (`rlc_dl_creation`). That hook's context has no limits
    today, so this needs a gNB change. It also misses bearers created before the set was loaded, and the map is destroyed
    on unload. The values could not be inferred from `cur_*` instead, because an earlier codelet may already have
    changed them. Only useful for reporting.
  - **Decision (2026-09-14, revised 2026-09-15): A1 + A2.** The xApp message gains an `op` field (SET / GET / RESET).
    `jbpf_rlc_ctrl_info` gains `cfg_byte_limit` / `cfg_sdu_limit` (appended, so the layout stays compatible), and on
    RESET the codelet writes those configured values straight back. A2 is needed anyway for `GET`, which is how the CLI
    displays current and configured sizes. RESET therefore works for any bearer, whether or not the xApp saw it appear.
    The clamp still turns an oversized request into the configured value, so A3 remains as a fallback.

  **Question B: what happens when the codelet is unloaded?**
  - **B1. Keep the last applied limits** (today).
    - ✅ You can set a limit and then unload to remove the per-packet cost.
    - ❌ Leftover state is invisible and skews the next run unless the gNB restarts.
  - **B2. Restore on unload** ("limits are valid only while a control codelet is attached"). Before calling the hook, the
    gNB checks whether a codelet is attached to `rlc_dl_ctrl` (the hook's `codelets` pointer). If none is attached, or a
    different codelet has been attached since, it restores the config values. Every newly loaded codelet then starts from
    config.
    - ✅ Clean, reproducible runs. Also fixes the stale SDU limit when switching to `rlc_fixed`.
    - ❌ gNB code reads jbpf hook internals. The restore happens per bearer, on its next packet.
  - **B3. Orchestrated reset.** An unload script or the xApp sends RESET for all bearers, waits, then unloads.
    - ✅ No gNB change beyond A1.
    - ❌ Only bearers that carry traffic before the unload get reset, so it's racy.
  - **Decision (2026-09-14): B1, unloading leaves the limits in place.** The control codelet is loaded only while a
    command is being applied and unloaded straight after, so there is no per-packet cost between commands (§6). RESET is
    the way back to the default.

  The resulting design (two codelet sets, an xApp that owns the bearer table, and a CLI) is in §6.

  Fix the wrong comments at the same time: `telemetry/buffer_ctrl.py` "Unlimited (clear override → gNB default)" and the
  `erctrl_demo.py` header.
- [ ] **2. (P2) Control at PDCP instead of (or as well as) RLC.**
  - RLC drops packets after PDCP has numbered them. This leaves a gap, and the UE's PDCP holds later packets for up to
    `t_reordering` (220 ms for 5QI 9, `out_of_order_delivery: false`).
  - PDCP admission (`check_early_drop()` in `lib/pdcp/pdcp_entity_tx.cpp`) uses `desired_buffer_size` from F1-U DL Data
    Delivery Status. RLC fills that with the static `cfg.queue_size_bytes` (`on_transmitted_sdu()` in
    `rlc_tx_am_entity.cpp`).
  - Option: report `sdu_queue.get_byte_limit()` instead.
  - Caveats: PDCP counts bytes not yet *delivered*, including packets RLC has sent but the UE hasn't acknowledged, so it
    trips earlier than RLC would. PDCP has no SDU-count equivalent. The report lags by at least `t_notify` (5 ms).
- [ ] **3. (P2) Per-packet cost.**
  - The hook runs for every DL SDU on every AM DRB. Each call does a control-input receive, a map lookup and a
    write-back.
  - Options: drain `ctrl_in` less often (time-gated); skip the write-back when `cur_*` already equals the stored value.
- [ ] **4. (P2) Commands only take effect when there is traffic.**
  - `ctrl_in` is drained only when a DL SDU arrives on some AM DRB. With no DL traffic, commands wait in the queue
    (depth 32), and xApp sends fail once it is full.
  - `erctrl_app.py` sends every 100 ms regardless of change; it should send only on change.
  - Consider draining from a periodic hook.
- [x] **5. (P1) `erctrl_app.py` unit bug — moot on this branch (2026-09-16).**
  - The bug (`CTRL_UNIT = "sdu"` with byte-sized setpoints, from `2007afe`) is gone because `erctrl_app.py` is restored
    to its tutorial version. It would return if that change is ever re-applied.
- [ ] **6. (P2) `upt_app_demo.py` still publishes a sweep annotation nothing applies.**
  - It emits a 5-step SDU sweep as `upt_ctrl_*`, but no loaded codelet performs that sweep, so the dashboard shows a
    setpoint that is not in force. Either drop the annotation or publish the values actually commanded (which the
    `bufctl` report channel now provides).
  - `upt_app.py` is no longer affected: it is back at its tutorial version.
- [ ] **7. (P2) UM bearers.**
  - Add the same hook call in `rlc_tx_um_entity::handle_sdu`. UM 5QIs (1, 2, 3, 7, 65–67, …) can't be controlled today.
  - TODO comments are in `rlc_ctrl.cpp` and `rlc_tx_am_entity.cpp`.
- [ ] **8. (P2) Target by 5QI.**
  - No jbpf hook exposes 5QI today; the RLC entity only knows the DRB id.
  - This needs a hook where the bearer ↔ 5QI mapping is known (DU UE context / bearer setup) to report it to the xApp.
    The xApp then keeps sending DRB ids.
- [ ] **9. (P2) Stale commands after UE release.**
  - `limit_map` is keyed on `du_ue_index`, so a new UE that reuses an index inherits the old command.
  - Fix: clear entries from a small codelet on the existing `rlc_dl_deletion` hook, sharing the map. That is a second
    codelet but no new hook.
- [ ] **10. (P3) No feedback to the xApp.** Applied or clamped values only appear in the gNB log. Add an output channel,
  or expose `cur_*` through telemetry.
- [ ] **11. (P3) Demo control app.**
  - `erctrl_demo.py` targets `upt://…/upt/rlc_ctrl`, which only exists when `upt.yaml` is loaded, not `upt_demo.yaml`.
  - `deployment_ctrl_demo.yaml` loads no codelet set.
  - A comment says it re-sends every tick, but the code sends on change.
  - `buffer_setpoint.txt` is run-time state committed to git.
- [ ] **12. (P3) Legacy `jrtc-apps/containers/Docker/srsRAN_Project`.**
  - Not updated with these fixes. It has already drifted: `>=` vs `>` in the byte check, 16-bit capacity.
  - Decide whether to mirror changes there or mark the tree as legacy.
- [ ] **13. (P3) Duplicated struct.**
  - `jbpf_rlc_ctrl_info` is copied in `rlc_ctrl.cpp`, `rlc_fixed.cpp`, `rlc_perue.cpp` and the gNB.
  - Use a shared header, or add a size assert (20 B full struct, 12 B byte-only prefix).
- [ ] **14. (P3) Bytes-per-SDU constant.**
  - `rlc_perue.cpp` assumes 1500 B per SDU. An RLC SDU is a PDCP PDU (at least 1503 B for a 1500 B IP packet), so the
    byte cap binds a few SDUs early.
  - Use 1507, the basis of OCUDU's `default_rlc_queue_size_bytes`.
- [ ] **15. (P3) Cleanup.**
  - Delete the untracked `jrtc-apps/codelets/upt/rlc_ctrl.cpp.bak.*`.
  - `rlc_ctrl.o` is root-owned and must be rebuilt.
  - Upstream jbpf docs say `jbpf_control_input_receive` returns 0 on success; it actually returns 1.
- [ ] **16. (P3) Release and repo hygiene.**
  - Update `RELEASE-NOTES.md`.
  - The `ocudu-jbpf` commits are not on GitHub yet.
  - Planned split: the SDU limit plus these fixes go to `open-ai-ran-tutorial`; the GTP-U/F1-U hooks go to `scoutv2`.

- [ ] **17. (P3) `scout-control-doc.md` is out of date.**
  - It describes the legacy `jrtc-apps/containers/Docker/srsRAN_Project` tree, the byte-only control struct, and the
    codelet's old built-in sweep. All three have since changed, and it is on the tutorial branch.
  - Decide with its author: update it, or keep it as the original design note and point it at
    `rlc-buffer-control-hook.md` for current behaviour.

- [ ] **18. (P1) Latency tails are lost before they reach the dashboard — keep the real 134 ms buckets.**

  The codelets bucket at `UPT_BUCKET_SHIFT = 27`, i.e. **134.217728 ms**, and the xApp stamps every sample with that
  bucket's own close time (`ts_ns = (bucket_id + 1) * UPT_BUCKET_NS`). VictoriaMetrics runs with
  `-dedup.minScrapeInterval=0s`, so the per-bucket samples are stored at full resolution. The information is lost in
  two places *above* storage, and the tails matter: with a large buffer the distribution is heavily skewed, so a mean
  hides exactly the effect buffer sizing is meant to show.

  1. **The codelet already averages inside the bucket.** `rlc_queueing` emits
     `upt_rlc_queue_latency_ms` as the **mean** over each 134 ms bucket (`{"latency_ms": round(avg_ms, 4)}`), keeping
     only `min` and `max` beside it. Every per-packet tail *within* a bucket is gone before it is stored.
     The per-packet path (`rlc_pkt_record` → `upt_rlc_pkt_lat_p50/p95/p99/max_ms`) does keep percentiles, but they are
     computed per output batch rather than per bucket.
  2. **Grafana then smooths across buckets.** The panels use bare selectors
     (e.g. `upt_rlc_queue_latency_ms{ue=~"..."}`) with no explicit `interval` or `maxDataPoints`, so the step comes from
     panel width and time range — seconds per point over a multi-minute window, averaged within each step. This is the
     "averaged over multiple seconds" effect.

  What to work out:

  - **Store a distribution, not a mean, per bucket.** Either emit per-bucket percentiles from `rlc_queueing` (it already
    computes `min`/`max`; p50/p95/p99 would follow the `rlc_pkt_record` pattern), or emit a histogram
    (fixed log-spaced buckets → `_bucket` series) so any quantile can be recovered at query time.
  - **Stop the query-time averaging.** Set an explicit `interval` of `134ms` (or `maxDataPoints` matched to the window)
    on the latency panels, and use `max_over_time`/`quantile_over_time` rather than the implicit average for tail
    panels. Note `-search.latencyOffset=1s` on VM delays the newest point.
  - **Decide what the dashboard should show.** A mean line plus a p95/p99 band and a max envelope, at bucket
    resolution, over a short window (say 60 s) — plus a way to view the raw per-packet records
    (`rlc_pkt_record` already writes them) for a chosen interval.
  - **Watch the cost.** 134 ms buckets are ~7.5 samples/s per series per bearer; adding percentiles or histogram buckets
    multiplies the series count, so check VM ingestion and the 7-day retention before turning it on for 4 UEs.

## 6. SET / GET / RESET API + a minimal CLI (coded 2026-09-15, testbed run pending)

Goal: change a bearer's buffer size by hand from a terminal, see what is in force, and pay no per-packet cost when not
commanding. The xApp decides and the codelet only executes, which is the same split the later algorithms will use.

**Dropped on 2026-09-15: the bearer-table design.** An earlier version of this section had a second codelet set on
bearer creation and deletion, JSON state files, and a rule that the xApp had to be started before the UEs. None of that
is needed: the control hook already carries each bearer's identity and its current and configured limits, so the xApp
can keep no state at all.

**One codelet set.** `bufctl` = `rlc_ctrl` on `rlc_dl_ctrl`, with `ctrl_in` for commands and an output channel for
replies. Loaded while commanding, unloaded afterwards.

**Three ops.** The message layout stays 20 bytes; `op` takes the place of the unused `is_srb` field.

| Op | Meaning | What the codelet does on that bearer's next DL packet |
|---|---|---|
| `SET` (0) | Apply a byte and/or SDU limit (a 0 field keeps the stored value) | Writes the stored values into `new_byte_limit` / `new_sdu_limit` |
| `RESET` (1) | Return to the configured size | Writes `cfg_byte_limit` / `cfg_sdu_limit` straight back |
| `GET` (2) | Read what is in force | Emits one report (UE, DRB, `cur_*`, `cfg_*`) and changes nothing |

**One gNB change:** append `cfg_byte_limit` and `cfg_sdu_limit` to `jbpf_rlc_ctrl_info`, filled from the queue's
`cfg_byte_limit` and `capacity`. Appending keeps the layout compatible with `rlc_fixed` / `rlc_perue`. This replaces the
earlier plan to add arguments to `rlc_dl_creation`, so no bearer-creation hook change is needed.

### Components and flow

```
  CLI (host terminal)                 xApp (jrtc pod)                    gNB (ocudu)
  ───────────────────                 ───────────────                    ───────────
  prompt: UE, DRB, op, value ──────>  load bufctl if needed
                                      send {ue, op, drb, bytes, sdus} ──> ctrl_in
                                                                            │
                                                              [rlc_ctrl] runs on that
                                                              bearer's next DL packet
                                                                            │
                                            SET  -> new_byte_limit / new_sdu_limit ──> clamp + apply
                                            RESET-> cfg_byte_limit / cfg_sdu_limit ──> clamp + apply
                                            GET  -> no change
                                                                            │
  print result <──────────────────  report: ue, drb, cur_*, cfg_*  <────────┘
  prompt again                        unload bufctl
```

One command, step by step:

1. The CLI prompts for UE → DRB → op → value, and checks the value against that bearer's range.
2. The xApp loads `bufctl` if it isn't loaded and sends `{ue, op, drb, byte_limit, sdu_limit}`.
3. `rlc_ctrl` acts on that bearer's next DL packet: SET writes the requested limits, RESET writes the configured ones,
   GET only reports.
4. The codelet reports UE, DRB, current and configured limits, so the xApp can confirm what actually took effect.
5. The xApp unloads `bufctl`, so there is no per-packet cost between commands. The applied limit stays (decision B1).
6. The CLI prints the result and prompts again, so sizes can be changed repeatedly while watching the dashboard.

The CLI lists bearers by sending `GET` (for a UE/DRB, or for each known index), so neither the xApp nor the CLI keeps
persistent state, and there is no ordering requirement against UE attach. A bearer only answers once it carries
downlink traffic, which is exactly when its buffer size matters.

### Error handling

| Case | Behaviour |
|---|---|
| Unknown op, or a UE / DRB outside the codelet's range | Command ignored; the CLI reports "no reply" after its timeout |
| Value out of range | The CLI shows the range (SDUs 1…`queue-size`, bytes 9007…`queue-bytes`) and asks again; the gNB clamps anything that still gets through |
| UM bearer or SRB | Not controllable: the hook is never called, so no report comes back and the CLI says so |
| No DL traffic on that bearer | No report arrives. `bufctl` stays loaded and the CLI says "not applied yet"; it takes effect when traffic resumes |
| `load` / `unload` fails | Surfaced to the CLI; a failed unload leaves the set loaded rather than in an unknown state |
| gNB restart | Nothing to invalidate, since neither side keeps state; the next `GET` shows the configured values again |
| Clamped value | The report shows `cur_*` different from what was requested, and the gNB logs `requested=` alongside `new=` |

### Files added or changed

```
ocudu-jbpf/  include/srsran/jbpf/jbpf_srsran_contexts.h   + cfg_byte_limit / cfg_sdu_limit (appended)
             lib/rlc/rlc_sdu_queue_lockfree.h             + get_cfg_byte_limit(), get_capacity()
             lib/rlc/rlc_tx_am_entity.cpp                 fill cfg_* in handle_jbpf_rlc_ctrl()
tutorial/    jrtc-apps/codelets/bufctl/rlc_ctrl.cpp       control codelet: SET / GET / RESET + report
             jrtc-apps/codelets/bufctl/rlc_ctrl_report.proto  report message
             jrtc-apps/codelets/bufctl/bufctl.yaml        codeletset (ctrl_in + out_rlc_ctrl)
             jrtc-apps/codelets/bufctl/Makefile           builds the proto + codelet
             (upt/ untouched: upt.yaml, upt/rlc_ctrl.cpp and upt/Makefile stay upstream)
             jrtc-apps/jrtc_apps/bufctl/                  bufctl_app.py, deployment.yaml, bufctl_cli.py
```

No bearer-creation or bearer-deletion codelets, and no persistent state. `commands.jsonl`, `state.json` and
`events.jsonl` under `jrtc_apps/bufctl/` are just the CLI ↔ xApp transport and can be deleted at any time.

The control codelet lives in its own directory so no colleague-authored file is touched: `upt/rlc_ctrl.cpp`,
`upt.yaml` and `erctrl.yaml` are byte-identical to the tutorial branch, and the existing `erctrl` mechanism still
works. Because only one codelet can attach to a control hook, `bufctl` and any set containing `upt/rlc_ctrl.o`
(`upt.yaml`, `erctrl.yaml`, `bufsize*.yaml`) are mutually exclusive at load time.

### How to run it

1. Rebuild the codelets: `jrtc-apps/codelets/make.sh -d upt` (SDK container; also generates the report proto's `.pb`,
   serializer and Python module).
   Then rebuild the gNB. **All of these steps are needed**, because the gNB runs inside an *ephemeral container* that
   cannot be replaced in place — restarting only the gNB process keeps the old binary:
   ```bash
   ocudu-jbpf/build.sh                                          # build ocudu-gnb-jbpf:local
   k3d image import ocudu-gnb-jbpf:local -c janus-cluster       # make it visible to the k3d node
   kubectl delete pod -n ran srs-gnb-du1-0                      # the StatefulSet recreates the pod
   kubectl patch pod -n ran srs-gnb-du1-0 --subresource ephemeralcontainers --type strategic \
     -p "$(cat ocudu-jbpf/deploy/ephem_jbpf.json)"              # re-add the jbpf gNB container
   bash telemetry/fresh_start_demo.sh                           # jrtc -> gNB -> upt_demo -> UE -> tcp_probe
   ```
   Sanity check: `kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- md5sum /usr/local/bin/gnb` must match
   `docker run --rm --entrypoint md5sum ocudu-gnb-jbpf:local /usr/local/bin/gnb`.
   A gNB older than the `cfg_*` fields makes the new codelet fail its context bounds check on every packet.
2. Bring up UEs and traffic as usual. **No ordering requirement**: the xApp can be started at any time.
3. On the host, run `python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py`. Use `GET` to see current and configured sizes,
   `SET` to change one, `RESET` to go back to the configured size.
4. Quit the CLI, which unloads `bufctl`. Limits stay as last applied, so `RESET` before quitting if you want defaults.
5. `bufctl` is mutually exclusive with `upt.yaml`, `erctrl.yaml` and the `bufsize*.yaml` sets, because a control hook
   takes only one codelet. `upt_demo.yaml` (the demo pipeline) contains no control codelet, so it loads alongside.

### Operational gotchas (learned while testing, 2026-09-15)

Not obvious, and each one cost real time:

- **Deleting the gNB pod wipes both ephemeral containers.** The gNB (`ocudujbpf`) and the UE (`durue1`) are *ephemeral*
  containers, so the `kubectl delete pod` needed to pick up a rebuilt image destroys them and everything hand-staged
  inside them:
  - `/codelets` in the gNB container — without it **every** codeletset load fails with **HTTP 500**, because the jbpf
    agent opens the `.o` and `*_serializer.so` paths from its own filesystem (README §6c).
  - the UE's `oaiue_zmq.conf` **and** `uecap_ports1.xml`. With the capability file missing, the OAI UE dies with
    `Assertion (max_mimo_layers > 0) failed!` shortly after RRC setup.
  Re-stage all of it (README §6a–6c) after any pod recreate.
- **ZMQ does not reconnect mid-run.** If the UE dies, restart the gNB *and* the UE together — `fresh_start_demo.sh` does
  exactly that. Restarting only the UE leaves it stuck after "Initializing UE Threads", never decoding SIB1.
- **Dead UE processes linger as zombies.** `setsid … & disown` orphans them onto the container's PID 1 (`tail`), which
  never reaps them, so `pgrep -c nr-uesoftmodem` over-counts and `pkill` cannot clear them. Check `ps -eo pid,stat` for
  `Z` before believing a process count.
- **Stage `/codelets` from a *built* tree.** The jbpf agent opens each codelet's `.o` and `*_serializer.so` by path on
  its own filesystem, so staging from a fresh clone (sources only) makes **every** codeletset load fail with
  **HTTP 500** — identical to the symptom of `/codelets` being missing. Build first, then `kubectl cp`.
- **`fresh_start_demo.sh` prints `PIPELINE UP` even when the codeletset load failed.** It does not check the load
  result. Confirm with `grep -c "Codelet created and loaded" /tmp/gnb.log` in the gNB container instead.
- **Kill by process name, not command line.** `pkill -f nr-uesoftmodem` also matches the `sh -c` wrapper that is running
  the `pkill`, so it self-kills (exit 137). Use `pkill -x`.

### Guarding against a dead or misbehaving xApp — out of scope (2026-09-15)

Decided not to build this for now. For the record, jbpf does not tell the gNB whether an xApp is still connected (peer
tracking is internal to the IPC layer, and there is no agent hook for connect or disconnect), so the two available
signals would be: a per-packet check of `__jbpf_hook_rlc_dl_ctrl.codelets` in the gNB (which contradicts "set and
unload"), or a lease inside the codelet that writes `cfg_*` back once a command ages out. Revisit if a shared testbed
needs it.

### Open risk

It isn't yet known whether the xApp's input stream re-binds after `bufctl` is unloaded and reloaded. If it doesn't, the
first command after a reload fails, and the fallback is to keep `bufctl` loaded with an explicit "unload" command in the
CLI. To be checked on the testbed early, along with a measurement of the real per-packet cost using the existing
`jbpf_stats` set.

---

## 7. Merge into `open-ai-ran-tutorial` (2026-09-16)

Reviewed with the codelet owner and approved. The merge prunes **only** the conflicting
buffer-size control; every other EdgeRIC hook point is untouched.

**Pruned (buffer-size control only):**

| File | Why |
|---|---|
| `codelets/edgeric/erctrl.yaml` | codeletset that re-used `upt/rlc_ctrl.o` on `rlc_dl_ctrl` |
| `jrtc_apps/edgeric/erctrl_app.py` | its xApp |
| `jrtc_apps/edgeric/deployment.yaml` | its deployment |
| `codelets/upt/rlc_ctrl.cpp` | the original byte-limit codelet |
| `rlc_ctrl` entry in `codelets/upt/upt.yaml` | only one codelet may attach to `rlc_dl_ctrl` |

Also removed, ours and superseded by `jrtc_apps/bufctl/bufctl_cli.py`:
`erctrl_demo.py`, `deployment_ctrl_demo.yaml`, `buffer_setpoint.txt`,
`telemetry/buffer_ctrl.py`, and the three `scripts/` harnesses that patched the codelet
source in place (`ascending_buffer_ladder.sh`, `four_ue_varbuf_ctrl_hook.sh`,
`single_ue_buffer_ctrl_test.sh`).

**Explicitly NOT touched** (verified present and byte-identical to the tutorial branch):
`edgeric_mcs.yaml`, `edgeric_sched.yaml`, `dl_mcs.cpp`, `dl_sched.cpp`,
`dl_mcs_app.py`, `dl_sched_app.py`, `dl_mcs_deployment.yaml`, `dl_sched_deployment.yaml`,
`codelets/edgeric/Makefile`, and the `codel` / `l4span` / `bufsize` codeletsets.

`erctrl.yaml` owned no source of its own (it re-used `${JBPF_CODELETS}/upt/rlc_ctrl.o`),
so the prune leaves no orphaned `.cpp` and `codelets/edgeric/Makefile` needed no edit.

### Dashboard: there was no conflict

`jrtc_apps/dashboard/dashboard.json` is an **Azure Monitor workbook**
(`version: "Notebook/1.0"`, `KqlParameterItem/1.0` items, `fallbackResourceIds`) — not a
Grafana dashboard. Commit `82e8cf6` had overwritten it with an early copy of our Grafana
JSON; it is now restored byte-identical to the tutorial version.

Our dashboard was always separate: `telemetry/upt-dashboard.json`
(title "RAN User-Plane Telemetry (per-packet)", `uid: upt-userplane`, 8 panels),
provisioned into Grafana through the `grafana-upt-dashboard` ConfigMap by
`telemetry/telemetry-stack.yaml`. The two artifacts target different tools and cannot
collide. To add further Grafana dashboards later, drop extra JSON keys into that same
ConfigMap — the provider watches the directory `/var/lib/grafana/dashboards`, so it picks
up every file without further config.

### Post-merge sanity check (2026-09-16)

Run against the merged branch itself: `/apps` and `/codelets` in `jrtc-0` are hostPath
mounts onto `jrtc-apps/jrtc_apps` and `jrtc-apps/codelets`, so checking out
`open-ai-ran-tutorial` in the working repo makes the shipped branch content what the
testbed actually executes. No staging step is involved.

| Phase | Result |
|---|---|
| Merge content live on the testbed | `upt/rlc_ctrl.cpp` gone, `erctrl.yaml` gone, `bufctl` present, `dl_mcs`/`dl_sched` present, no `rlc_ctrl` in `upt.yaml` |
| Codelet build from the branch | `bufctl`, `upt`, `edgeric` all build; verifier: `Program terminates within 892 instructions` |
| Bring-up (`telemetry/fresh_start_demo.sh`) | PIPELINE UP, UE attached, 5 `upt` codelets registered, DRB at `queue_size=8192 queue_size_bytes=8388608` |
| SET sdu 512 | `cur_sdus=512`; gNB: `rlc_dl_ctrl changed sdu_limit. old=8192 new=512 requested=512` |
| SET byte 200000 | `cur_bytes=200000`; gNB: `rlc_dl_ctrl changed byte_limit. old=8388608 new=200000 requested=200000` |
| Clamp low (`byte 100`) | clamped to `9007` (9000 + 3 + 4), as designed |
| Clamp high (`sdu 999999`) | clamped to `8192` (queue capacity) |
| RESET | back to `8388608 B / 8192 SDU`, report `reason=reset` |
| GET | `cur == cfg == 8388608 B / 8192 SDU`, report `reason=get` |
| Regression: her codeletsets | `dl_mcs` and `dl_sched` both load and unload cleanly |
| Dashboard | Grafana healthy; `upt-userplane` served; queue/buffer/p95 series all populated |

#### Bug found and fixed: a silently dropped command

The first GET of the run was dropped. Root cause: the xApp seeds
`state.offset = os.path.getsize(CMD_FILE)` at startup so it does not replay history, and
`_poll_commands()` returned early on `size == state.offset`. A truncate-and-append that
lands on **exactly** that byte count is therefore indistinguishable from "nothing new" --
and `: > file` keeps the inode, so inode tracking alone does not catch it. The command was
consumed by neither path and no event was logged.

Fixed by comparing the full file signature `(st_ino, st_size, st_mtime_ns)`: if the file
changed while `size <= offset`, the reader restarts from 0 and says so on stdout. Verified
by reproducing the exact failure (rewrite to precisely the seeded offset), which now
yields `sent get` followed by a `reason=get` report.

**`commands.jsonl` is append-only.** Truncating or rewriting it while the codeletset is
loaded now replays the whole file rather than dropping commands, so delete it only while
bufctl is unloaded.

#### Operational note

`/tmp/gnb.log` in the gNB container sits on a 1.1 TB overlay, not the host's root
filesystem, and grows at roughly 1.6 GB/h with PHY logging at `info`. It had reached 12 GB
before this run. Truncate it between experiments; it does not consume host `/`.

#### CLI fixes from the first real user session (2026-09-17)

Two bugs, found by driving `bufctl_cli.py` by hand:

1. `help` crashed with `AttributeError: 'NoneType' object has no attribute 'split'`.
   `_help()` parsed `__doc__`, but the file's header is `#` comments, not a docstring, so
   `__doc__` is `None` and the help text was unreachable. Help is now a literal `HELP`
   string, parameterised by `SCAN_UES`/`SCAN_DRBS`/`MIN_BYTES`.
2. That traceback exited the process **without unloading**, leaving the codelet attached
   and running on every downlink packet; the next launch then failed its load with
   `400 Bad Request`. `main()` now wraps the session in `try/finally` and unloads on every
   exit path, exceptions included, tracking `_LOADED` so a manual `unload` does not cause
   a double unload. A failed load also explains what a 400 means instead of leaving a raw
   `jrtc-ctl` fatal dump on screen.

Also: the `set` usage hint now shows worked examples, since `set 0 1 2048` (no unit) is the
natural first guess.
