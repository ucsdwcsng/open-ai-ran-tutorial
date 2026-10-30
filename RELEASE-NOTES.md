# Release notes

## Unreleased — RLC DL buffer-size control (`rlc_dl_ctrl`)

Lets an xApp change a DRB's **downlink RLC buffer size at run time**, with no gNB restart
and no config reload. API reference and xApp guide:
[`rlc-buffer-control-hook.md`](rlc-buffer-control-hook.md); development log, decisions and
open items: [`RLC-CTRL-CHANGES.md`](RLC-CTRL-CHANGES.md).

**It changes a threshold, not an allocation.** The hook writes two variables —
`byte_limit` and `sdu_limit`, atomics in `rlc_sdu_queue_lockfree` — which `write()` tests
when a packet arrives. Queue storage is allocated once at bearer creation from
`queue-size` and is never resized, so a new limit applies to the arriving packet onwards,
never to what is already queued, and worst-case memory is unchanged.

### gNB (`ocudu-jbpf`, submodule)

- `rlc_dl_ctrl` fires once per DL SDU in `rlc_tx_am_entity::handle_sdu`, before the
  enqueue/tail-drop decision, and **only for DRBs** — SRBs always keep their configured
  limits.
- `rlc_sdu_queue_lockfree` gains an atomic SDU-count ceiling alongside the byte limit;
  `write()` tail-drops once either is exceeded.
- Requested values are **clamped**: SDU count to `[1, capacity]`, bytes to
  `[9007, queue-bytes]` (9007 = one maximum-size PDCP PDU). A limit can therefore never
  switch a bearer off, and can only tighten relative to configuration.
- `jbpf_rlc_ctrl_info` carries the bearer identity, the limits in force and the
  **configured** limits (`cfg_byte_limit` / `cfg_sdu_limit`), so a codelet can restore the
  configured size and report it without the xApp knowing any defaults.
- Each applied change is logged with the requested value, so clamping is visible:
  `rlc_dl_ctrl changed byte_limit. old=… new=… requested=…`.

### Codelet, xApp and CLI (`jrtc-apps`)

- **`codelets/bufctl/rlc_ctrl.cpp`** — the control codelet, in its own directory, acting per **(UE, DRB)**.
  It started as a rewrite of `upt/rlc_ctrl.cpp`, whose key (`((ue << 4) | rb) % 16`) discarded the
  UE, so a command aimed at one UE changed that DRB on *every* UE. Ops: `SET` / `GET` / `RESET`;
  a bearer with no command is left untouched (no built-in policy). The original codelet stays in
  place untouched for `erctrl`.
- **Report channel** (`rlc_ctrl_report.proto`) — the codelet emits
  `{ue, drb, cur_*, cfg_*}` on `GET`, on `RESET`, and whenever the limits in force change,
  which is how a `SET` is confirmed and how clamping becomes visible.
- **`codelets/bufctl/`** — new directory holding the codelet, its report proto, `bufctl.yaml` and a Makefile.
  Build it with `make.sh -d bufctl`. Nothing here is shared with the `upt` telemetry codelets.
- **`jrtc_apps/bufctl/`** — `bufctl_app.py` (relays commands, records reports) and
  `bufctl_cli.py` (terminal front end: `scan`, `list`, `set <ue> <drb> sdu|byte <n>`,
  `reset`, `load`, `unload`). The CLI loads the codeletset for a session and unloads it on
  exit, because **while it is loaded the hook runs on every downlink packet**. Unloading
  leaves applied limits in force, so `RESET` before quitting to restore defaults.
- `upt.yaml` is **unchanged**: it still loads the original `upt/rlc_ctrl` codelet. Only one codelet
  may attach to a control hook, so `bufctl` and any set containing `upt/rlc_ctrl.o` (`upt.yaml`,
  `bufctl.yaml`) cannot be loaded at the same time — load whichever you need.

### Verified on the testbed

Rebuilt gNB, UE attached with a 5QI 9 AM DRB (8192 SDU / 8,388,608 B), ~40 Mbit/s
downlink; 17 commands sent, 0 failed, 8 reports received.

| Command | gNB log | Report |
|---|---|---|
| `GET` | — | `cur` = `cfg` = 8,388,608 B / 8192 SDU |
| `sdus=256` | `old=8192 new=256 requested=256` | `cur_sdus=256` |
| `bytes=200000` | `old=8388608 new=200000 requested=200000` | `cur_bytes=200000` |
| `sdus=1` | `old=256 new=1 requested=1` | `cur_sdus=1` |
| `bytes=100` | `new=9007 requested=100` (clamped up) | `cur_bytes=9007` |
| `RESET` | restored 8,388,608 B / 8192 SDU | `cur == cfg` |

Only the bearer carrying downlink traffic answered, which is correct: the hook runs only
when a packet arrives. With no traffic there are no reports and no changes — check that
`TX SDU` is climbing in the gNB log before concluding anything about the control path.

### TODO — move the drop decision up to PDCP

RLC drops a packet **after** PDCP has already given it a sequence number, leaving a gap, so
the UE's PDCP holds every later packet until `t_reordering` expires (220 ms for 5QI 9,
which has `out_of_order_delivery: false`). A drop meant as a congestion signal therefore
becomes a latency spike. PDCP already has a clean drop point *before* numbering
(`check_early_drop()` in `lib/pdcp/pdcp_entity_tx.cpp`), but it compares against a
**static** `desired_buffer_size` that RLC reports from configuration, so it never learns
the dynamic limit. Reporting the live byte limit there would move the drop ahead of
numbering. Two caveats: PDCP counts bytes not yet *delivered* (including packets RLC has
sent but the UE has not acknowledged), so it trips earlier than RLC would; and PDCP has no
SDU-count equivalent. Tracked as item 2 in `RLC-CTRL-CHANGES.md`.

### Known consequences

- **No colleague-authored file is modified.** `upt/rlc_ctrl.cpp`, `upt.yaml`, `upt/Makefile`,
  `erctrl.yaml`, `erctrl_app.py`, `ue_contexts/*`, `dashboard.py`, `libs/ue_contexts_map.py`,
  `upt_app.py` and `deployment_fixed.yaml` are byte-identical to `open-ai-ran-tutorial`, so the
  existing `erctrl` mechanism keeps working exactly as before. The new control work lives entirely
  in `codelets/bufctl/` and `jrtc_apps/bufctl/`.
- The one-way-delay codelets (`owd_pdcp`, `owd_rlc`) are **not** on this branch: nothing consumed
  their metrics, and keeping their sources without the matching proto entry broke `make -C upt`.
- GTP-U arrival and F1-U bearer hooks are deliberately **not** part of this branch.
- `fresh_start.sh` / `fresh_start_demo.sh` now count `rlc_dl_ctrl` for their gNB hook
  sanity line; they previously counted the removed GTP-U/F1-U hooks and so always printed
  `hooks=0` on this branch.

---

## Unreleased — clean-clone buildability

Fixes a set of defects that made a **fresh clone unbuildable** while every existing
working tree kept building. Found by bringing the testbed up from scratch on a second
server; full analysis in [`BUILD-ISSUES.md`](BUILD-ISSUES.md).

### Fixed

- **jbpf: `.gitignore` no longer hides Concurrency Kit's build templates.**
  `external/jbpf/.gitignore` had an unanchored `build` rule. Because `.gitignore`
  patterns match at every level, it excluded `3p/ck/build/` — ck's 15 `configure`
  input templates, which are source, not output. Without them `configure` silently
  wrote a zero-byte `ck.build`, `ALL_LIBS` came out empty, and the build failed
  minutes later in an unrelated place with `cp: cannot stat 'src/libck.so'`.
  Rule anchored to `/build`; the 15 templates are now tracked. ck's own `.gitignore`
  already ignores the 5 generated files, so no new ignore entries were needed.

- **codelets: added the missing `bufsize/Makefile`.** (Historical: the `bufsize`
  codeletsets were later retired in favour of the `bufctl` control hook.)
  The directory shipped three codeletset yamls referencing `rlc_fixed16.o`,
  `rlc_fixed64.o` and `rlc_fixed256.o`, but no Makefile — and the shared
  `Makefile.common` cannot produce three sized objects from one source, since it
  derives objects by wildcard. Adds explicit per-variant rules passing
  `-DFIXED_LIMIT`. Sizes follow the source default (64 → 65536): 16384 / 65536 /
  262144. All three pass the verifier at 36 instructions.

- **README: completed the §5d codelet build list.**
  It omitted `fapi_dl_conf`, `fapi_ul_conf`, `fapi_ul_crc`, `fapi_rach`,
  `xran_packets` and `upt`. The dashboard xApp imports the FAPI/xran Python modules
  even though its OCUDU deployment drops their codeletsets, so an unbuilt directory
  killed the app at import — while `jrtc-ctl load` still printed `loaded app`, making
  it look like a successful load with no telemetry.

### Added

- **`tools/check-clean-clone.sh`** — static pre-release check for this whole class of
  defect. No build, no cluster, no submodule init; safe in CI.

### Known issues

- `slice_mgmt` does not compile against the pinned `jbpf_protobuf` (`bd009ef`):
  `use of undeclared identifier 'JBPF_MAX_SLICES'`. Needs an SDK bump or a local
  definition. Because `jrtc_apps/slice_mgmt/deployment.yaml` imports the module the
  codelet would generate, **the shipped `slice_mgmt` xApp cannot load at all** — and
  it fails the same silent way described above.
- A dashboard xApp that dies during startup is still reported as `loaded app` by
  `jrtc-ctl`. Root cause of the confusing symptom above; see BUILD-ISSUES.md §5 for a
  proposed fix.
- `open5gs-dbctl add` is not idempotent — re-running the README's subscriber step
  creates duplicate IMSI records rather than a no-op.

### Companion change (separate repo)

`ocudu-jbpf` carried the **same** `.gitignore` defect in
`external/jbpf/.gitignore`, and its `3p/ck/build/` was missing entirely, so `./build.sh`
failed identically. Fixed there in commit `c3bf07b21d`, which applies
`tools/patches/ocudu-jbpf-gitignore.patch`.

---

## Pre-release check

Run before tagging a release, merging to the default branch, or asking anyone to set
the testbed up on a new machine:

```bash
tools/check-clean-clone.sh
```

It verifies:

| # | Check | Catches |
|---|---|---|
| 1 | No `.gitignore` shadows a vendored `build/` holding `*.in` templates | the ck defect, in any vendored 3p library |
| 2 | ck's 15 templates are present **and tracked** | the ck defect, specifically |
| 3 | Every codelet dir with a yaml has a Makefile | the `bufsize` defect |
| 4 | Every referenced `.o` has a source file or explicit Makefile rule | sized-variant objects with no rule |
| 5 | Every xApp module import comes from a dir the README builds | incomplete build lists |

Exit code is 0 when clean and 1 on any blocker, so it drops into CI unchanged.

**On this branch it still exits 1**, reporting exactly the two issues that are not yet
resolved: the `ocudu-jbpf` `.gitignore` (fix belongs in that repo — patch provided) and
`slice_mgmt` (needs the SDK decision). That is the check doing its job; it goes green
once both land. Treat those two failures as the remaining work, not as noise to
suppress.

### Why a static check

Each of these is invisible to a normal build: the developer's tree has the ignored
artifacts already, so the build passes locally and keeps passing. The defect only
appears on a machine that has never built before — which in practice means a new lab
member, a CI runner, or a reviewer. The check inspects what is **tracked in git**
rather than what is present on disk, which is the only way to see the difference.

Where the check cannot reach, verify by hand:

- [ ] `git clone --recursive` into an empty directory, then build the gNB and UE
      images end to end — no files carried over from an existing tree.
- [ ] Load every deployment under `jrtc-apps/jrtc_apps/` and confirm each app is
      still alive afterwards, not merely reported as loaded.
- [ ] Attach the documented number of UEs and confirm each gets a distinct IP.

## Merged into `open-ai-ran-tutorial`

The RLC buffer-control work above is now on the tutorial branch. The merge removed the
earlier, conflicting buffer-size control (`erctrl` codeletset + xApp, and the `rlc_ctrl`
codelet/entry under `codelets/upt/`), because the `rlc_dl_ctrl` hook accepts exactly one
codelet. All other EdgeRIC hook points (DL MCS, DL scheduler) are unchanged, as is the
Azure workbook at `jrtc_apps/dashboard/dashboard.json`.

Runtime control is now: `codelets/bufctl/` (codelet) + `jrtc_apps/bufctl/` (xApp + CLI).
See `rlc-buffer-control-hook.md` for the hook API and `RLC-CTRL-CHANGES.md` §7 for the
merge record.

## Known issue: the upstream `slice_mgmt` codeletset does not build

`jrtc-apps/codelets/slice_mgmt` arrived with the tutorial-branch import (`0424c84`) and is
upstream -- it is not part of the RLC buffer-control work. It does not compile against the
public `srs-jbpf-sdk` image:

```
slice_mgmt.cpp:119:75: error: use of undeclared identifier 'JBPF_MAX_SLICES'
... 20 errors generated.
make: *** [../Makefile.common:15: slice_mgmt.o] Error 1
```

So `slice_mgmt.o` has never existed, `codelets/slice_mgmt/slice_mgmt.yaml` (which attaches
it on the `mac_sched_slice_mgmt` hook) cannot load, and `jrtc_apps/slice_mgmt`, which loads
that codeletset, cannot run here either.

It is **deliberately excluded from the README section 5d build list**. Including it makes
the documented build step fail for every user; excluding it costs nothing, because nothing
else depends on the object. The protobuf half of its Makefile does succeed, so
`slice_mgmt.py` and both serializers exist and are importable.

`tools/check-clean-clone.sh` therefore reports
`xApp imports slice_mgmt/* but README build list omits 'slice_mgmt'`. **That failure is
expected and pre-existing** -- it predates this branch and was previously hidden by a
parsing bug in the checker itself, now fixed. It is left visible rather than papered over,
because the codeletset genuinely cannot load and silencing that would be worse.

Nothing in the buffer-control path (`codelets/bufctl`, `jrtc_apps/bufctl`, `scripts/`) uses
or depends on `slice_mgmt`.
