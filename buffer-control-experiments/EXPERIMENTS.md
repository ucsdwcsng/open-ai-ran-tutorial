# Control-hook experiments: proposal

Two hands-on experiments for the Open AI-RAN tutorial. Students **load and unload control
codelets** on a running gNB and watch the effect in Grafana within seconds. Each experiment
also has a scripted version that runs the same steps on a timer and produces the plots.

Target: the ZMQ testbed (OCUDU gNB + OAI Duranta UE), the same setup students get.

`results/...` paths below refer to run outputs on the testbed host; they are not committed
(see `.gitignore`). How to run everything: [README.md](README.md).

Status: **draft for discussion** · baseline measured · **A built and run** (interactive + scripted) · **B built and checked** (sustained drop + fading channel via `radio.sh`); adaptive codelet optional, not built

---

## The two experiments at a glance

| | **A. One knob for bufferbloat** | **B. The right buffer depends on the channel** |
|---|---|---|
| Question | What does the RLC buffer size do to latency and throughput? | Why must the buffer be controlled *at runtime*, not set in a config file? |
| UEs | 1 | 1 |
| Traffic | Bulk CUBIC download + a paced "video call" TCP flow on the same UE | Same |
| Codelets students load/unload | `bufcap_*` (fixed buffer caps) | Students: `bufcap_*`. Instructor: `radio.sh` → `badchan` (sustained MCS drop, or periodic fading dips) |
| Headline | Loading a cap cuts video-call latency from ~700 ms to under ~100 ms at the same throughput; too small a cap costs throughput | A sustained drop needs a **smaller** cap (the parked data becomes a standing queue); a fading channel needs a **bigger** one (the buffer rides through the dips: 256k gives 2× the throughput of 64k). The same knob moves in opposite directions, so it has to be controlled at runtime |
| Folds in earlier ideas | #1 (same throughput, different latency), #2 (CUBIC vs BBR), #3 (download lags the call), #5 (sweep) | #4 (capacity change), #6 (student controller challenge) |
| Build effort | Small: one parameterized codelet + scripts | Small: one MCS-ceiling codelet with an optional fade period (built) |

A comes first and teaches the mechanism. B makes the case for a runtime hook. B reuses A's
`bufcap` codelet, so students go straight from one to the other.

---

## Testbed facts (measured 2026-09-22)

| Quantity | Value | Notes |
|---|---|---|
| DL capacity, 1 UE | **~29–30 Mbit/s** | iperf3 receiver, 90 s |
| Idle RTT (ping UE→UPF) | ~35 ms (25–60) | Overstates TCP's base RTT: an idle UE pays the SR → UL-grant cycle that a busy flow's ACK stream avoids (see "Measuring latency") |
| BDP, 1 UE | ≈ 30 Mbit/s × ~35 ms ≈ **~130 kB** | Refine once the base RTT under load is measured |
| Config RLC DRB queue | 8192 SDU / 8 MB | `fresh_start_demo.sh` |
| CUBIC standing queue, uncapped | **~2.6 MB, ~700 ms** | Bounded by the UPF's `tcp_wmem` max (4 MB), not by the RLC queue. **Zero losses in 90 s.** |
| BBR, uncapped | 30.4 Mbit/s, ~190 kB queue, ~72 ms srtt | BBR self-limits to ~1.5 BDP |

Baseline data and plots: `results/baseline_{cubic,bbr}/`.

---

## Measuring latency

Ping is **not** used as the latency metric. A sparse ping (e.g. every 200 ms) arrives at an
idle uplink and pays the scheduling-request → UL-grant cycle each time. So its RTT follows a
stop-and-start pattern, not what a full-buffer TCP flow sees. That flow's ACK stream keeps the
UL busy and its BSRs flowing.

Instead:

| What | How | Why |
|---|---|---|
| **Bulk flow latency** | TCP srtt of the CUBIC flow (iperf3 sender on UPF; `tcp_rtt_ms` in Grafana via `tcp_probe`) | The latency the congestion controller acts on |
| **"Video call" latency** (the one students care about) | A second TCP flow on the same UE, **paced** at ~2 Mbit/s (`iperf3 -b 2M`), measuring its srtt | Continuous, like a real call: it keeps UL grants active and sits in the same DL RLC queue as the bulk flow. Paced, so it never fills the queue itself |
| **Queuing delay at the gNB** | jbpf `upt` telemetry: RLC queue bytes and SDU sojourn time | Ground truth for where the delay is. Separates RLC queuing from everything else |
| Base RTT | srtt of the paced flow alone, before the bulk flow starts | Same UL conditions as the rest of the run, unlike an idle ping |

---

## A. One knob for bufferbloat

### Story
"Your video call lags whenever someone on the same phone downloads a file. The download isn't
using more than its share. It is filling a buffer in the base station, and your call waits
behind it. The gNB can decide how big that buffer is."

### Setup
- 1 UE: `scripts/setup_bufctl_1ue_demo.sh` (fresh gNB + UE + `upt` telemetry, ~5 min)
- Traffic, both DL on the same UE/DRB, sender on UPF:
  - **bulk**: CUBIC, unlimited, long-running
  - **video call**: CUBIC, paced at 2 Mbit/s, long-running
- Grafana `upt-userplane`: throughput, TCP RTT, RLC queue bytes, RLC queue latency

### How it works

**One queue, two flows.**
- The UE has two downlink TCP flows: a 2 Mbit/s paced "video call" and an unlimited CUBIC
  download.
- Both travel on the UE's one data bearer (DRB1), so they share **one RLC queue in the gNB**.
  The gNB keeps one such queue per UE per bearer; packets wait there until the MAC scheduler
  sends them over the radio.

**Without a cap.**
- CUBIC grows its window until it sees a loss.
- The RLC queue is configured for 8 MB, so nothing is dropped. What finally stops CUBIC is the
  sender's socket buffer on the UPF (`tcp_wmem` max = 4 MB).
- About **2.7 MB** ends up standing in the RLC queue. Draining it takes
  2.7 MB × 8 / 35 Mbit/s ≈ **620 ms**.
- Every video packet joins the back of that queue and waits just as long. The video RTT,
  download RTT and RLC queuing delay therefore all lie on top of each other at 600–800 ms.
  The flows aren't different; **the queue is the latency.**

**With `bufcap` loaded.**
- The codelet runs on the `rlc_dl_ctrl` hook for every downlink packet, just before the gNB
  adds it to the queue. It sets `new_byte_limit = CAP`.
- The gNB tail-drops once the queue holds CAP bytes. CUBIC sees the losses and backs off, so
  the queue stays under the cap.
- Latency becomes ≈ base RTT + queue ÷ link rate.

### The BDP, and how it was measured

BDP (bandwidth-delay product) = link rate × base RTT: the data that has to be in flight to
keep the link busy. Queue beyond that adds delay, not throughput.

| Input | Value | How measured |
|---|---|---|
| Link rate | ~35 Mbit/s (29 in the 90 s baseline, 33–36 in the experiment A runs) | iperf3 receiver in the UE |
| Base RTT | **29 ms** | srtt of the video flow alone, before the download starts |
| **BDP** | 35 Mbit/s × 29 ms ≈ **127 kB** ≈ 85 full-size packets | derived |

Base RTT is **not** the idle ping (~35 ms). An idle UE waits for an uplink grant on every ping,
which a busy flow's ACK stream avoids. The first ping-based estimate (30 Mbit/s × 35 ms ≈
130 kB) happened to land close.

### How the caps were chosen, and what they do

The first caps were ×4 steps on a log scale, bracketing the ~130 kB BDP estimate from the
baseline (following the old `rlc_fixed` sizes). 128k was added after the first run, once it
was clear the throughput knee sits between 64k and 256k. The sizes were **not** derived from
theory.

| Cap | vs BDP | Max added delay (cap ÷ 35 Mbit/s) | Measured video RTT − 29 ms base | Measured download | Mean RLC queue |
|---|---|---|---|---|---|
| none (≈2.7 MB, set by the sender) | ~21× | ~620 ms | 570–660 ms | 33–36 Mbit/s | 2.4–2.8 MB |
| 1m | ~8× | 240 ms | 165–195 ms | 30–34 | ~760 kB |
| 256k | ~2× | 60 ms | 35–40 ms | 29–31 | ~110 kB |
| 128k | ~1× | 30 ms | 10–13 ms | 22–28 | ~26 kB |
| 64k | ~0.5× | 15 ms | 1–5 ms | 14–19 | ~6 kB |
| 16k | ~0.13× | 4 ms | 0–3 ms (p95 spikes 100+ ms) | 1–9 | ~2 kB |

(Measured ranges: the stepped-down and stepped-up CUBIC runs plus the interactive session.
Details in "First results" below.)

**Why the measured delay is below cap ÷ rate.** CUBIC's window saws up and down:
- it fills the queue to the cap and loses a packet;
- it cuts the window to 70%, and the queue drains;
- the window grows again, and the cycle repeats.

So the *average* queue sits well below the cap: 256k gives a ~110 kB mean (43%), 1m ~760 kB
(74%). Cap ÷ rate is the ceiling, not the average.

**Why throughput falls below ~1 BDP.**
- After a loss, CUBIC's window drops to 0.7 × (BDP + cap). The link stays busy only if that
  is still ≥ BDP, which needs a cap ≥ ~0.43 × BDP ≈ 55 kB.
- The measurement is worse than that: throughput already halves at 64k.
- Interpretation (not yet tested): this radio delivers in bursts (TDD slots, scheduling,
  HARQ), and each RLC drop costs extra here, through retransmissions and sometimes the UE's
  220 ms reordering wait. So TCP needs more headroom than the textbook number.

**Practical sweet spot on this testbed: ~1–2 BDP (128k–256k):** 40–65 ms video RTT at 80–85%
of full throughput.

### Numbers to try

Build any size with `bufcap.sh build <size>`, then `bufcap.sh load <size>`. The predicted
columns use the same rules as above: mean queue ≈ 40–75% of the cap, throughput full above
~2 BDP and falling below ~1 BDP. The middle rows fill gaps between the measured points.

| Cap | Why try it | Predicted video RTT | Predicted download |
|---|---|---|---|
| 16k | Far below BDP: link starves, loss-recovery spikes | ~30 ms + spikes | ~5–10 Mbit/s |
| 32k | ~¼ BDP | ~30–35 ms | ~10–15 |
| 64k | ~½ BDP: the textbook CUBIC minimum, fails here | ~30–35 ms | ~15–19 |
| 96k | ¾ BDP: is the knee here or higher? | ~35–40 ms | ~20–25 |
| 128k | ~1 BDP | ~40 ms | ~22–28 |
| 192k | ~1.5 BDP | ~50 ms | ~27–30 |
| 256k | ~2 BDP: sweet spot | ~60–70 ms | ~29–31 |
| 512k | ~4 BDP | ~100–130 ms | ~31–34 |
| 1m | ~8 BDP | ~200–220 ms | ~30–34 |
| 2m | ~16 BDP: close to uncapped | ~450–550 ms | ~33–35 |

Prediction exercise for students: for a cap C (bytes), the worst-case added delay is
C × 8 / rate, and the expected added delay is about half of that. Compare with the "Latency"
panel after loading.

### What to point out while it runs

1. **The throughput dip right after the first cap loads.** Going from a 2.7 MB queue to 256k
   in one step makes the gNB drop every arriving packet until the queue drains below the cap.
   CUBIC takes a burst of losses and needs ~10 s to recover. The runbook's "step limits
   ascending" note describes the same effect. Going straight from uncapped to 16k is the
   extreme case: the download collapsed to 1.1 Mbit/s for 30 s.
2. **RTT spikes with an empty queue (16k, 64k).** The download RTT hits 200–600 ms while the
   RLC queuing-delay line sits at 0. That delay isn't queuing. It's loss recovery:
   retransmissions plus the UE holding packets while it waits for a missing one (up to
   `t-reordering` = 220 ms). A buffer that is too small costs both throughput *and* latency
   spikes.
3. **BBR doesn't need the cap, and is hurt by one.** With `traffic_A.sh start --cc bbr`, the
   uncapped video RTT is already ~77 ms, because BBR keeps its own queue near ~190 kB. Caps at
   or below that make things worse (see "First results").

### Codelets
`bufcap` is **one codelet source** (`jrtc-apps/codelets/bufcap/bufcap.cpp`), compiled once per
cap with `-DCAP_BYTES=<bytes>`. `bufcap.sh build <size>` compiles it in the jbpf SDK container,
runs the verifier (prints `verifier: OK` or `FAILED`), and writes the codeletset and deployment
YAMLs.

While loaded, it sets the byte limit on every downlink packet of every DRB of every UE.
`bufcap_off` (CAP_BYTES=0) writes back the configured limits instead. Prebuilt: `off`, `16k`,
`64k`, `128k`, `256k`, `1m`.

Students can build their own value. Editing, compiling and loading one codelet is itself a
learning goal.

### Student steps (interactive, ~20 min)
Dashboard: `http://localhost:30490/d/expA-bufcap`.

| Step | Action | What they should see |
|---|---|---|
| 1 | `traffic_A.sh video-only` | Video RTT ≈ 29 ms (the base RTT), queue ≈ 0 |
| 2 | `traffic_A.sh bulk-only` | Within ~20 s the queue climbs to ~2.7 MB and video RTT to ~600–700 ms. Download ~33–35 Mbit/s |
| 3 | **Predict** the video RTT for 256k (see "Numbers to try") | ~60–70 ms |
| 4 | `bufcap.sh load 256k` | Within seconds: queue under 256 kB, video RTT ~65 ms, download ~30 Mbit/s (after a ~10 s dip) |
| 5 | `load 128k`, then `load 64k`, then `load 16k` | Latency keeps falling a little, but throughput falls a lot below ~1 BDP. RTT spikes appear with an empty queue |
| 6 | `bufcap.sh unload`, then `bufcap.sh off` | Unload alone leaves the cap in force (see "Prerequisite" below). `off` brings the bloat back |
| 7 | `traffic_A.sh stop`, then `start --cc bbr` | ~77 ms with no cap: the endpoint fix. Then load 256k: BBR gets *worse*. Discuss: an operator can't choose servers' CC, but can size the buffer |
| 8 | Build own caps from "Numbers to try", find the knee | Best trade-off near 1–2 BDP |
| 9 | `snapshot.py --minutes N` | Their session as a plot plus a per-cap table |

### Scripted run (for the report and the instructor)
Timed load/unload of the four caps, 30 s each, **ascending and descending** passes, CUBIC then
BBR. Outputs:
1. **Timeline**: throughput, both srtts, RLC queue, with the loaded codelet marked.
2. **Latency-throughput curve**: one point per cap, p50 and p95 srtt vs throughput. This is
   #1's "same throughput, different latency", done on one UE, so it avoids the 4-UE problems
   below.
3. **Predicted vs measured** delay table.

### First results (2026-09-22, `results/expA_*`)

30 s per cap, first 10 s of each phase skipped; video RTT is p50 / p95.

| Cap | CUBIC, stepped down: video RTT | download | CUBIC, stepped up: video RTT | download | BBR, stepped down: video RTT | download |
|---|---|---|---|---|---|---|
| none | 598 / 680 ms | 35.9 | 615 / 726 ms | 33.3 | 77 / 104 ms | 35.1 |
| 1m | 194 / 226 | 33.7 | 224 / 273 | 29.6 | 79 / 113 | 34.6 |
| 256k | 63 / 169 | 29.3 | 66 / 78 | 30.5 | 157 / 205 | 15.8 |
| 128k | 40 / 85 | 27.6 | 42 / 69 | 22.4 | 190 / 240 | 11.2 |
| 64k | 30 / 102 | 18.6 | 34 / 120 | 14.1 | 167 / 266 | 9.8 |
| 16k | 30 / 110 | 9.4 | **266 / 359** | **1.1** | 171 / 307 | 14.5 |

(Throughput in Mbit/s. The video flow alone: srtt ≈ 29 ms.)

What this shows:
- **The knob works, and the result is repeatable.** With CUBIC, 256k–128k caps give ~40–65 ms
  video RTT at 80–85% of full throughput. The knee is near 1 BDP (~125 kB). Up and down passes
  agree, except at 16k.
- **The queue sits well below the cap.** CUBIC's sawtooth keeps the mean queue at ~40–75% of
  the cap (256k → 110 kB). So cap ÷ rate over-predicts the delay, which makes a good
  prediction exercise for students.
- **A big cut from a bloated queue straight to 16k collapses the download** (1.1 Mbit/s,
  2,922 retransmits in 30 s). Video RTT rises to ~265 ms *with an empty RLC queue*. 265 ≈ 29 ms
  base + 220 ms `t-reordering`: RLC drops packets PDCP has already numbered, and the UE waits
  for the gap. **This is the stall predicted earlier, now measured.**
- **Capping BBR backfires.** BBR alone keeps ~190 kB queued (77 ms). A cap at or below that
  turns BBR's probing into a stream of drops, because BBR ignores loss. Retransmits reach
  1,300–2,400 per phase, the video call's RTT gets *worse* (157–190 ms), and in two phases the
  CUBIC video flow is starved (0.15 Mbit/s). Lesson: the right buffer depends on the sender,
  and a cap is not free.

### What could go wrong
| Risk | Check / mitigation |
|---|---|
| Each tail drop after PDCP numbering makes the UE wait up to `t-reordering` (220 ms) | First scripted run: look for ~220 ms srtt spikes right after drops in the capped phases. If present, lower `t-reordering` in the gNB PDCP config for the tutorial |
| A large downward step makes CUBIC collapse briefly (runbook note) | The descending pass shows whether it matters. It may be worth keeping as a teaching point |
| The video flow's own queue share | It is paced at 2 Mbit/s, under 10% of capacity, so the bulk flow sets the queue |

---

## B. The right buffer depends on the channel

Status: **built and measured** (`badchan` + the channel × cap grid). The adaptive `bufdelay` codelet is an optional extension, not built.

### Story
"You sized the buffer perfectly, then the user walked behind a building. The link rate fell
~3×, and the same buffer now holds ~3× the delay. A number in a config file can't track that.
A codelet in the gNB can."

### Setup
**Same scenario as A, no new bring-up:** the same UE, the same two flows (`traffic_A.sh`) and
the same dashboard. B adds a second knob on a **different hook**: `badchan` sits on
`mac_sched_dl_mcs_ctrl` (the PDSCH MCS-selection point), while `bufcap` sits on `rlc_dl_ctrl`.
So both can be loaded at the same time.

### Codelets
| Codelet | Hook | Effect while loaded | On unload |
|---|---|---|---|
| `bufcap_*` (from A) | `rlc_dl_ctrl` | Fixed RLC byte cap | Cap stays until `bufcap.sh off` (see "Prerequisite") |
| **`badchan_mcsN`** ✅ | `mac_sched_dl_mcs_ctrl` | Caps the DL MCS at N: a controllable "channel fade". It only ever lowers the MCS that link adaptation picked | **Reverts on the next grant**: the override is per grant, and the gNB falls back to link adaptation |
| **`bufdelay`** (to build) | `rlc_dl_ctrl` + a rate estimator hook | cap = measured dequeue rate × target delay, recomputed continuously | Same as bufcap |

`badchan` is one source (`jrtc-apps/codelets/badchan/badchan.cpp`), built per ceiling:
`badchan.sh build mcs10`. Prebuilt: `mcs5`, `mcs10`, `mcs16`, `mcs20`. `badchan.sh` has the same
load / unload / status / list commands as `bufcap.sh`. It marks every change on the dashboard
(a red annotation plus the "Channel: DL MCS ceiling" tile).

### Calibration: MCS ceiling → rate (2026-09-22)
The ZMQ channel is perfect (the UE reports CQI 15), so link adaptation normally picks the top
MCS. CUBIC download plus video, no buffer cap, 25 s per step:

| Channel | Download | vs normal | BDP at 29 ms | 1–2 BDP (sweet spot) |
|---|---|---|---|---|
| normal | 32–33 Mbit/s | 1× | ~120 kB | 128k–256k |
| MCS ≤ 20 | 19.9 | 0.6× | ~72 kB | 64k–128k |
| MCS ≤ 16 | 16.5 | 0.5× | ~60 kB | 64k–128k |
| **MCS ≤ 10** | **9.5** | **0.3×** | **~35 kB** | **~48k–96k** |
| MCS ≤ 5 | 4.6 | 0.14× | ~17 kB | ~24k–48k |

`mcs10` is the default "bad channel": about a 3.4× fade. Unloading brought the rate back to
33 Mbit/s at once, and the UE stayed attached throughout. With **no** buffer cap, a fade makes
bufferbloat much worse: the same ~3 MB queue drains 3.4× slower, so video RTT went from
~730 ms to ~2 s.

### Result: one fixed cap cannot fit both channels
`results/session_20260922_224931/`, 45 s per step:

| Step | Cap | Channel | Video RTT p50 / p95 | Download |
|---|---|---|---|---|
| 1 | 256k | normal | 59 / 84 ms | 31.3 Mbit/s |
| 2 | 256k | **bad (MCS ≤ 10)** | **147 / 197 ms** | 9.9 |
| 3 | **64k** (re-sized for bad) | bad | 53 / 95 ms | 6.7 |
| 4 | 64k | **normal again** | 33 / 112 ms | **16.9** |
| 5 | 256k | normal | 64 / 130 ms | 29.8 |

- **Step 2:** the buffer didn't change, the channel did, and the video call went from fine
  (59 ms) to laggy (147 ms, p95 near 200). The cap is now ~7 BDP instead of 2.
- **Step 3:** re-sizing to 64k fixes latency on the bad channel, at some cost in throughput
  (6.7 vs 9.9 Mbit/s: 64k is only ~1.8 BDP here, the low end of the sweet spot).
- **Step 4:** when the channel recovers, the small cap now **halves the download** (16.9 vs
  ~31 Mbit/s).

So each fixed cap is right for only one channel. Something has to re-size the buffer when the
channel changes, and that needs a runtime hook. That is what `bufdelay` does.

### How B is run: continue from A, then change the radio

B is not a separate scenario. It continues A's session. Students keep playing with
`bufcap.sh`; the instructor changes the channel with `radio.sh`. The two codelet sets load
side by side: `bufcap` on `rlc_dl_ctrl`, `badchan` on `mac_sched_dl_mcs_ctrl`.

```bash
bash buffer-control-experiments/radio.sh 10                  # sustained drop: DL MCS <= 10 (~9.5 Mbit/s)
bash buffer-control-experiments/radio.sh 10 fade             # fading: dips to MCS <= 10 for 100 ms every 400 ms
bash buffer-control-experiments/radio.sh 5 fade 50 200       # fading: dips to MCS <= 5 for 50 ms every 200 ms
bash buffer-control-experiments/radio.sh good                # normal channel (unload)
```

(`radio.sh` wraps `badchan.sh` and builds any new variant on first use. Every change shows
on the dashboard as a red annotation.)

1. **Part 1 = experiment A as is.** Students settle on their best cap for the normal channel,
   against a latency goal they state first (e.g. "video call under 60 ms").
2. **Part 2, sustained drop:** instructor runs `radio.sh 10`. Students re-tune. Finding: a
   **smaller** cap restores the latency goal (see "What students should find").
3. **Part 3, fading channel:** instructor runs `radio.sh good`, then `radio.sh 10 fade`.
   Students re-tune. Finding: now a **bigger** cap is what helps, because the buffer rides
   through the dips (see "Part 3").
4. Discussion: the same knob has to move in opposite directions depending on *how* the channel
   changes. A config file can't know which; a controller in the gNB can.

### Why the buffer's job depends on the timescale

The buffer's purpose is to absorb channel variability, so TCP doesn't have to back off.

TCP is ACK-clocked. When the channel slows, ACKs slow and the sender slows with them, without
a loss, as long as its in-flight data fits somewhere. When the rate drops, the BDP shrinks and
the excess in-flight data moves into the RLC queue. If the cap can hold it, TCP doesn't back
off, and when the channel recovers the queued data fills the link at once.

- **Example, the MCS ≤ 10 drop.** Before it, TCP had ~150 kB in flight: 127 kB BDP plus ~25 kB
  queued. The new BDP is ~35 kB, so ~117 kB has to be parked.
  - A 128k cap holds that: no loss needed, 92% of the faded link used.
  - A 64k cap can't: losses, CUBIC backs off, 67%.
- **Short, frequent dips** (fading, HARQ, scheduling gaps): the parked data drains as soon as
  the channel comes back. A **bigger** buffer keeps utilization high, at a brief latency cost.
  This is probably also why even the "perfect" ZMQ/TDD channel needs ~2× the textbook cap.
- **A sustained drop:** the parked data never drains. It becomes a standing queue, and
  loss-based CUBIC won't shrink its window without a loss. A **smaller** cap forces CUBIC down
  to the new BDP, trading some utilization for latency.
- **What no buffer can do** is keep throughput above what the channel carries.

### Answer key for Part 2 (instructor reference, optional): measured grid (2026-09-22, `results/grid_B_20260922_225600.csv`)
Not needed to run the lab; it's there so you know what students should find. Script: `grid_B.sh`. CUBIC download + 2 Mbit/s video, 30 s per cell (first 10 s skipped), caps
stepped up within each channel level. "% of channel" = download ÷ that channel's uncapped rate
(32.5 / 16.5 / 9.5 / 4.6 Mbit/s).

| Channel | Cap | Download | % of channel | Video RTT p50 / p95 | Mean RLC queue |
|---|---|---|---|---|---|
| normal | 32k | 15.2 Mbit/s | 47% | 24 / 103 ms | 2 kB |
| normal | 64k | 20.8 | 64% | 29 / 118 | 4 |
| normal | 96k | 23.3 | 72% | 33 / 68 | 12 |
| normal | 128k | 23.9 | 74% | 39 / 128 | 25 |
| normal | 256k | 28.9 | 89% | 62 / 113 | 109 |
| MCS ≤ 16 | 32k | 7.3 | 44% | 37 / 122 | 2 |
| MCS ≤ 16 | 64k | 10.3 | 62% | 41 / 115 | 7 |
| MCS ≤ 16 | 96k | 12.7 | 77% | 43 / 120 | 11 |
| MCS ≤ 16 | 128k | 14.6 | 88% | 57 / 99 | 44 |
| MCS ≤ 16 | 256k | 15.3 | 93% | 103 / 141 | 135 |
| MCS ≤ 10 | 32k | 4.4 | 46% | 40 / 88 | 4 |
| MCS ≤ 10 | 64k | 6.4 | 67% | 52 / 142 | 11 |
| MCS ≤ 10 | 96k | 6.9 | 73% | 75 / 133 | 25 |
| MCS ≤ 10 | 128k | 8.7 | 92% | 85 / 117 | 57 |
| MCS ≤ 10 | 256k | 9.3 | 98% | 157 / 254 | 165 |
| MCS ≤ 5 | 32k | 2.5 | 54% | 55 / 141 | 5 |
| MCS ≤ 5 | 64k | 3.4 | 74% | 83 / 136 | 18 |
| MCS ≤ 5 | 96k | 3.8 | 83% | 112 / 185 | 28 |
| MCS ≤ 5 | 128k | 4.1 | 89% | 139 / 202 | 60 |
| MCS ≤ 5 | 256k | 4.2 | 91% | 288 / 440 | 211 |

One run per cell. p95s are noisy (loss-recovery spikes); trust p50 and throughput.

### What students should find

**1. Right after the fade, with the cap unchanged (e.g. 128k).**
- The download drops 23.9 → 8.7 Mbit/s. That is the radio: it is still 92% of what the faded
  channel can carry.
- The video call goes 39 → 85 ms. The same 128 kB now drains 3.4× slower: 128 kB is ~33 ms of
  queue at 32 Mbit/s but ~110 ms at 9.5 Mbit/s.

**2. On a sustained drop, a cap big enough to hold TCP's in-flight excess is enough; more
only adds delay.**
- At MCS ≤ 10, 128k already holds the ~117 kB of excess: 8.7 Mbit/s, 92% of the channel.
- Going to 256k gains only 8.7 → 9.3 Mbit/s (98%), but the call goes to 157 ms.
- The lost throughput belongs to the channel. No buffer carries more than the channel does.

**3. The new sweet spot is smaller, and worse than before.**
- With a fixed latency goal, the best cap shrinks as the channel gets worse. For example,
  "largest download while the video call stays under X ms":

  | Channel | Best cap, call < 100 ms | Best cap, call < 60 ms |
  |---|---|---|
  | normal | 256k (28.9 Mbit/s, 62 ms)* | 128k (23.9, 39 ms) |
  | MCS ≤ 16 | 128k (14.6, 57 ms) | 128k (14.6, 57 ms) |
  | MCS ≤ 10 | 128k (8.7, 85 ms) | 64k (6.4, 52 ms) |
  | MCS ≤ 5 | 64k (3.4, 83 ms) | 32k (2.5, 55 ms) |

  \*62 ms is just over the 60 ms line, which is why 256k only appears in the 100 ms column.

- On a bad channel, no cap gets back both the old latency *and* ~90% utilization. The
  trade-off curve itself is worse.

**4. The right cap is really a delay budget, not a byte count.**
- A cap of C bytes costs about C × 8 / rate of queuing, so a given byte count costs more
  milliseconds as the rate falls.
- The cap does not shrink in proportion to the rate. The rate fell 7× from normal to MCS ≤ 5,
  but the best cap for "< 100 ms" only fell 4× (256k → 64k).
- Reason: the base RTT also grows at low MCS. With a tiny cap, the video RTT is 24 ms at
  normal, ~40 ms at MCS ≤ 10 and ~55 ms at MCS ≤ 5 (each packet takes longer on air). So the
  BDP (rate × RTT) falls more slowly than the rate.

**Note for the instructor on Part 1.** In this grid, 128k on the normal channel gave 74% of
capacity at 39 ms. 256k gave 89% at 62 ms. Earlier runs gave 128k 22–28 Mbit/s. So "the ideal
cap" in Part 1 depends on the latency goal students choose: 128k for "under ~50 ms", 256k for
"under 100 ms". Ask them to state the goal first. That makes Part 2 ("re-find it after the
fade") well-defined.

### Part 3: fading channel (`radio.sh 10 fade`)

`badchan_fadeXofY` applies the MCS ceiling for X ms out of every Y ms, using the gNB's slot
counter. Default: MCS ≤ 10 for 100 ms every 400 ms. The average capacity is then
≈ 0.75 × 32.5 + 0.25 × 9.5 ≈ 27 Mbit/s. Built variants: `fade100of400`, `fade50of200`,
`fade200of1000`; any other is built on first use.

First check (`results/session_20260923_001807/`, one run, ~40–50 s per step):

| Cap | Channel | Download | Video RTT p50 / p95 |
|---|---|---|---|
| 64k | normal | 14.2 Mbit/s* | 38 / 103 ms |
| 64k | **fading** | **8.9** | 41 / 126 |
| **256k** | **fading** | **18.5** | 73 / 187 |
| 256k | normal | 32.5 | 61 / 85 |

\*Lower than the grid's 64k/normal (20.8); single short run.

- Under fading, the 4× larger cap **doubles** throughput. The small buffer takes a loss on
  almost every dip; the large one rides through them.
- Even 256k reaches only ~70% of the ~27 Mbit/s average, so students should try 512k and 1m
  here. Watch the latency cost: it shows up mainly *during* the dips.
- Compare with Part 2: a sustained drop wanted a **smaller** cap, fading wants a **bigger** one.

Things to vary: the dip depth (`radio.sh 5 fade`), the dip length and the period.
Short, rare dips should favour bigger buffers most; long dips behave more like Part 2.

### Scripted comparison: static vs dynamic cap under the same radio schedule (2026-09-23)

Scripts: `make_expB.py` (generates both configs), `run.sh`, `compare_B.py`. Plot:
`results/expB_compare.png`.

- **Radio schedule, same in both:** 30 s normal (warm-up), then MCS 16 → 10 → 5 → 10 → 16 →
  normal, 20 s each. Measured window: 120 s.
- **Static:** cap fixed at 128k.
- **Dynamic:** cap = 128k + (R_normal − R_now) × 30 ms, i.e. 187k / 212k / 230k at MCS
  16 / 10 / 5. The cap goes up 4 s *before* a worsening step and comes down *after* an
  improving one. The radio switches at the same times in both runs.
- 2 runs per policy, alternating, each on a fresh bring-up.

| Run | Download (120 s mean) | Data | Video RTT p50 / p95 | Bulk retransmits |
|---|---|---|---|---|
| static r1 | 10.3 Mbit/s | 155 MB | 79 / 149 ms | 119 |
| static r2 | 14.1 | 212 | 64 / 156 | 199 |
| dynamic r1 | 14.0 | 210 | 101 / 312 | 152 |
| dynamic r2 | 13.3 | 199 | 107 / 226 | 131 |
| **mean static** | **12.2** | 183 | **72 / 152** | 159 |
| **mean dynamic** | **13.6** | 204 | **104 / 269** | 142 |

Per radio step (download, Mbit/s):

| Run | MCS 16 | MCS 10 | MCS 5 | 10 (up) | 16 (up) | normal |
|---|---|---|---|---|---|---|
| dynamic r1 | 15.7 | 7.8 | 3.5 | 10.5 | 14.5 | 27.6 |
| dynamic r2 | 13.4 | 10.7 | 5.5 | 8.0 | 13.4 | 26.3 |
| static r1 | 10.6 | 10.0 | 4.7 | 5.7 | 9.3 | 20.7 |
| static r2 | 15.4 | 9.1 | 4.5 | 9.5 | 15.7 | 27.3 |

**Reading, not yet a conclusion:**
- The headline "+11.5% for dynamic" comes almost entirely from **static r1**. That run recovered
  slowly on the way back up (5.7 / 9.3 / 20.7 Mbit/s). static r2 matches dynamic step for step.
- With n = 2 the throughput difference is **within run-to-run noise**.
- The **latency cost of the dynamic cap is consistent**: video RTT p50 +45% (72 → 104 ms), p95
  ~1.8× (152 → 269 ms). At MCS ≤ 5, 230 kB drains in ~400 ms, so the call reaches 200–600 ms.
- Why the gain is small: parking only helps at the moment of a step. With 20 s sustained steps
  most of each step is steady state, where 128k already gets ~90% of the channel (see the grid).
  This test is therefore closer to "sustained drops" than to "temporary dips".

**Next, to settle it:**
1. 5 runs per policy (~65 min), to separate a real gain from noise.
2. Short steps (e.g. 2–5 s) so transitions dominate. That is the "temporary dip" case where
  parking should matter. Faster than ~2 s needs the codelets to switch themselves, because a
  jrtc-ctl load takes 1.5–4 s.

### Student steps (Part 2, ~15 min)
| Step | Who | Action | What they should see |
|---|---|---|---|
| 1 | students | Leave their Part 1 cap loaded (e.g. 128k); note download and video RTT | ~24 Mbit/s, ~40 ms |
| 2 | instructor | `radio.sh 10` | Download ~9 Mbit/s, video RTT ~85 ms (the dashboard's channel tile turns red) |
| 3 | students | **Predict** before touching anything: will a bigger or a smaller cap help? | — |
| 4 | students | Try 256k | +0.6 Mbit/s, call ~157 ms: on a sustained drop, bigger is worse |
| 5 | students | Try 64k, 96k | 52–75 ms, 6.4–6.9 Mbit/s: pick one against their latency goal |
| 6 | instructor | `radio.sh 5`, later `radio.sh good` | Re-tune each time. On unload, the small cap now wastes throughput: go back up |
| 7 | discussion | — | The right cap follows the channel. Doing this by hand every time the channel changes is the case for a controller in the gNB (optional extension below) |

### Optional extension: an adaptive codelet (`bufdelay`, not built)
Replace the hand-tuning with a codelet that sets cap = measured drain rate × delay budget. It
would re-size on every channel change, and students could compare it against their hand-picked
caps under a scripted fade schedule. Build only if wanted.

| Item | Design | Effort |
|---|---|---|
| Rate estimator | Per bearer, bytes leaving the RLC queue per ~100 ms window, on an RLC TX-side hook, into a shared map (pattern: `codel/codel_rate.cpp`) | Medium |
| Control codelet | On `rlc_dl_ctrl`: cap = rate × delay budget, with headroom (the grid shows the cap must be ~2× textbook to keep throughput) and a floor of ~2 packets. Budget set at compile time (`bufdelay_30ms`) or from an xApp | Medium |
| Scripted fade schedule | `badchan_mcs10` loaded/unloaded on a timer (e.g. 45 s good / 45 s bad × 3). `runner.py` needs a `{"badchan": "mcs10"}` event type. Run once with a fixed cap and once with `bufdelay`, then compare p50/p95 video RTT and throughput per channel state | Small |

---

## Prerequisite for both: "unload" should undo the effect

Today an RLC limit **survives the codelet's unload**: the gNB keeps the last value until
something resets it or the gNB restarts. For a load/unload exercise, that breaks the moment
where students unload and see the bloat come back. It also leaves the gNB in a state they
can't see.

Options:
1. **Lease semantics in the gNB** (recommended). A control codelet must assert its limit on
   every call. If no codelet writes a limit for a bearer, the gNB restores the configured
   size. This is a small change in `rlc_tx_am_entity.cpp` and matches how the MCS hook
   already behaves. `bufctl` would then need to re-assert its per-bearer limit every packet;
   it already keeps them in a map.
2. **Convention without a gNB change**: the load/unload helper script sends a reset before
   unloading. It works, but only if students use the script. `jrtc-ctl unload` by hand still
   leaves the limit in place.

Open question: which one, and whether the same rule should apply to all
control hooks.

---

## Considered and not picked (and why)

| Idea | Why not a top-2 | Where it went |
|---|---|---|
| #1 4 UEs, different buffers, same CC | 4 saturated UEs are **not stable** on the ZMQ broker: in the first try 2 of 4 were released within ~60 s (DL HARQ max-retx, ZMQ channel timeouts). The survivors also split capacity unevenly (8.9 vs 5.9 Mbit/s) whatever the buffer, which confounds the result | The "same throughput, different latency" plot comes from A's cap curve on 1 UE |
| #2 CUBIC vs BBR | A good point, but a comparison rather than a knob | A, step 7 |
| #3 download lags the video call | — | The story of A |
| #4 capacity change | — | B |
| #5 sweep | — | A's scripted run |
| #6 student controller | — | B's challenge |
| 2-UE side-by-side (one fixed, one changed live) | Needs the 4-UE broker with 2 idle UEs; stability under 2 saturating flows untested | Stretch goal once A works |

---

## Open questions for discussion
1. Lease semantics in the gNB (option 1) or the unload-script convention (option 2)?
2. For B, is `mac_sched_dl_mcs_ctrl` the right "channel fade"? Alternative: a PRB cap through
   `mac_sched_dl_ctrl`.
3. Should the tutorial lower `t-reordering`, or should the 220 ms stalls stay visible as a
   lesson about dropping after PDCP numbering?
4. Time budget: A ~20 min and B ~25 min interactive, plus the challenge as homework?

---

## Tooling (`buffer-control-experiments/`)
| File | Purpose |
|---|---|
| `runner.py` ✅ | Runs an experiment from JSON: flows per UE, timed events, collects iperf3 + RLC telemetry, then calls `analyze.py`. Needs a "load/unload codelet" event type for A/B |
| `analyze.py` ✅ | `summary.json` per phase, `timeseries.csv`, `timeline.png` |
| `setup_4ue.sh` ✅ | 4-UE broker bring-up (for the stretch goal only) |
| `configs/` ✅ | Baseline configs |

---

## Mobility-trace replay: static caps of 512–4096 SDUs (2026-09-23)

**Setup**
- **Channel:** real per-second MCS traces from the Amarisoft channel-emulator runs (RNTI 4601):
  - pedestrian (`exp053…mob-ped…`, 81 s): MCS 24 → 0 for ~20 s → 9 → 0;
  - vehicular (`exp055…mob-veh…`, 100 s): MCS swinging 0–12 every ~10–15 s.
- **Replay:** extracted to `traces/{ped,veh}.csv` and replayed in the gNB by `badchan`'s trace
  mode (`radio.sh trace ped|veh`), indexed by wall-clock seconds.
- **Caps:** static, 512 / 1024 / 2048 / 4096 SDUs × 1470 B = 735k / 1470k / 2940k / 5880k.
- **Traffic:** one CUBIC download, no video flow (the link carries only 1–10 Mbit/s here).
  Fresh bring-up per run. Scripts: `make_expT.py`, `run.sh`, `compare_T.py`. Plots:
  `results/expT_{ped,veh}_compare.png`.
- **UPF send buffer:** `tcp_wmem` max raised from 4 MB to 16 MB for these runs (pod-local
  sysctl), so the large caps could fill. It resets if the UPF pod restarts.

**Results** (window: whole trace)

| Trace | Cap | Download | TCP RTT p50 / p95 | Mean RLC queue | Retransmits |
|---|---|---|---|---|---|
| ped | 512 SDU | 4.82 Mbit/s | 1.43 / 3.03 s | 528 kB | 590 |
| ped | 1024 | 5.74 | 1.57 / 2.77 | 801 | 2046 |
| ped | 2048 | 5.02 | 2.08 / 3.53 | 1061 | 3439 |
| ped | 4096 | 5.30 | 1.85 / 3.16 | 1222 | 4140 |
| veh | 512 SDU | 3.66 | 1.58 / 3.19 | 548 | 699 |
| veh | 1024 | 4.13 | 1.34 / 2.82 | 587 | 1471 |
| veh | 2048 | 4.33 | 1.04 / 2.65 | 537 | 925 |
| veh | 4096 | 3.92 | 1.36 / 3.07 | 609 | 1416 |

**What it shows**
- **The trace replay works:** throughput follows the MCS trace closely in both runs.
- **All four caps behave almost the same.** Throughput differs by ±10% (single runs, within
  noise), and TCP RTT peaks at ~3–3.5 s with every cap.
- **Why: a *time* limit, not a byte limit, sets the buffer here.**
  - PDCP `discard_timer` = **1500 ms** for 5QI 9 (`configs/qos.yml`), so the gNB drops any
    packet that has waited 1.5 s.
  - At the traces' 1–5 Mbit/s, 1.5 s is only ~190–940 kB. The discard timer therefore acts
    before any cap ≥ 512 SDUs binds (the mean queue of 0.5–1.2 MB stayed below every cap except
    735k).
  - The retransmits that grow with cap size are discard-timer drops, not cap drops.
- **At these rates every tested cap is far above the BDP.** At 2–5 Mbit/s × ~40–60 ms base RTT
  the BDP is only ~10–40 kB, so 735 kB is already ~20–70 BDP. This is the bloated regime for
  all four sizes.

**Testbed timing note (affects earlier fade numbers):** the ZMQ gNB runs its slots at ~0.44×
real time (median ~875 slots/s instead of 2000, varying 560–1300). `badchan` fade and trace
timing was switched from the slot counter to wall-clock time on 2026-09-23. The earlier
"100 ms dips every 400 ms" check ran with slot timing, so its dips were really ~230 ms every
~900 ms.

**Next options**
1. Raise or disable the PDCP discard timer (e.g. 10000 ms), so the byte caps become the limit
   and the 512–4096 SDU comparison is meaningful.
2. Add caps sized for this rate regime (16k–256k), where the throughput/latency trade-off
   actually sits.
3. Check what discard timer the Amarisoft/srsRAN runs used, so the comparison is like for like.

---

## Dynamic buffer controller (`bufcap.sh load dyn`), 2026-09-23

### The math
Notation:
- μ: link service rate, set by the MCS.
- λ: arrival rate, set by TCP.
- Q: bytes queued; W: queuing delay.
- RTT₀: base RTT (30 ms).
- W\*: target queuing delay (50 ms).

1. **Little's law:** Q = λW. In steady state TCP matches arrivals to service (λ = μ), so the
   cap that holds delay at the target is **B_ss = μ · W\***.
2. **Rate per MCS:** μ(MCS) = 5.658 · SE + 1.325 Mbit/s.
   - SE = Qm · R/1024 from 38.214 Table 5.1.3.1-1 (64-QAM table).
   - Fitted to the measured rates, within ~1 Mbit/s at MCS 5/10/16/20/28. MCS 0–4 are
     extrapolated.
   - Table: `mu_table.csv`; generated into the codelet as `mu_table.h`.
3. **When the MCS drops μ₁ → μ₂:** TCP learns of it only through slower ACKs, ~RTT₀ later.
   - Excess to absorb: **E = (μ₁ − μ₂) · RTT₀**.
   - Pad so no loss is needed: **B_pad = μ₁·W\* + E**, held for 60 ms (2·RTT₀).
   - Then a linear ramp over 200 ms down to **μ₂·W\***. The pad is temporary; a permanent pad
     becomes a standing queue.
   - Cost: a brief delay spike of B_pad/μ₂. You can absorb the excess or hold latency flat,
     not both.
4. **When the MCS rises:** cap = μ_new·W\* immediately (nothing to absorb).

Worked example, normal → MCS 10 (4.06 → 1.19 MB/s):

| | Cap | Queuing delay |
|---|---|---|
| Normal channel | 203 kB | 50 ms |
| Just after the drop (E = 86 kB) | pad 289 kB | ~243 ms, briefly |
| MCS 10, steady | 59 kB | 50 ms |

A static 203k cap overflows by ~86 kB at every drop, so the gain from padding should scale
with how often the channel drops.

### Implementation
One codeletset (ID `bufcap`, so it swaps and unloads like a fixed cap):

| Codelet | Hook | Job |
|---|---|---|
| `bufdyn_obs` | `mac_sched_harq_dl` (telemetry) | Records each UE's new-TX DL MCS in `mcs_map` |
| `bufdyn_ctl` | `rlc_dl_ctrl` (control) | Reads `mcs_map` (linked map), applies the rules above per UE, integer math, wall-clock timing |

The controller observes the MCS the scheduler actually used. It therefore follows any channel
change (badchan fade, trace, step, or real link adaptation) without sharing a schedule with it.

Build parameters: `bufcap.sh build dynW80` sets W\* = 80 ms. RTT₀, HOLD and RAMP are
`-D` defines in `bufdyn_ctl.cpp`.

Smoke test (bulk CUBIC):

| Channel | Queue peak | μ·W\* |
|---|---|---|
| normal | 173 kB | 205 kB |
| MCS 10 | 47 kB | ~55 kB |
| back to normal | 161 kB | 205 kB |

### Config change: PDCP discard timer off
`telemetry/fresh_start_demo.sh` now sets 5QI 9 `discard_timer: -1` (infinity), overridable
with `PDCP_DISCARD_MS=1500`. OCUDU accepts only the 3GPP values (10…1500 ms) or -1, so
"10 s" is not possible. Verified in the gNB log: `discard_timer=infinity`.

### Matrix (running): `make_expD.py`, `run_expD.sh`, `compare_D.py`
- **Scenarios:**
  - fade: MCS ≤ 10 for 200 ms every 1 s, 120 s;
  - traces: ped, veh.
- **Policies:**
  - fade: static 64k / 128k / 203k / 289k vs dynamic;
  - traces: static 128k / 512 / 1024 / 2048 / 4096 SDU vs dynamic.
- **Traffic:** one CUBIC download.
- **Repeats:** 3, in shuffled order, with one fresh bring-up per repeat block.

### Results (2026-09-23, 51 runs, `results/expD_*`, plots `results/expD_{fade,ped,veh}_tradeoff.png`)

Mean ± sd over 3 repeats. Download = the CUBIC flow over the whole window. TCP RTT = sender
srtt. PDCP discard timer off.

**Fading** (MCS ≤ 10 for 200 ms every 1 s, 120 s)

| Policy | Download | TCP RTT p50 / p95 | RLC qdelay p50 | Retransmits |
|---|---|---|---|---|
| static 64k | 12.26 ± 0.87 Mbit/s | 33 / 131 ms | 1 ms | 360 |
| static 128k | 18.99 ± 1.25 | 44 / 142 | 5 | 215 |
| **static 203k** | **25.07 ± 1.80** | 81 / 196 | 15 | 145 |
| static 289k | 23.14 ± 1.92 | 70 / 219 | 34 | 245 |
| dynamic | 13.08 ± 1.11 | 37 / 122 | 1 | 478 |

**Pedestrian trace**

| Policy | Download | TCP RTT p50 / p95 | RLC qdelay p50 | Retransmits |
|---|---|---|---|---|
| static 128k | 4.77 ± 0.45 | 282 / 1161 ms | 231 ms | 89 |
| 512 SDU (735k) | 5.27 ± 0.36 | 1454 / 5783 | 1421 | 317 |
| 1024 SDU (1470k) | 5.74 ± 0.57 | 2572 / 11054 | 2429 | 722 |
| 2048 SDU (2940k) | 5.23 ± 1.25 | 4369 / 13308 | 4257 | 884 |
| 4096 SDU (5880k) | 5.77 ± 0.73 | 5323 / 15484 | 5125 | 0 |
| **dynamic** | 4.49 ± 0.10 | **71 / 186** | 21 | 124 |

**Vehicular trace**

| Policy | Download | TCP RTT p50 / p95 | RLC qdelay p50 | Retransmits |
|---|---|---|---|---|
| static 128k | 3.65 ± 0.37 | 273 / 861 ms | 234 ms | 88 |
| 512 SDU (735k) | 3.50 ± 0.41 | 1555 / 3722 | 1484 | 344 |
| 1024 SDU (1470k) | 3.63 ± 0.03 | 2734 / 6331 | 2657 | 975 |
| 2048 SDU (2940k) | 3.40 ± 0.48 | 4981 / 9580 | 4902 | 1490 |
| 4096 SDU (5880k) | 3.78 ± 0.31 | 6706 / 10507 | 6675 | 1 |
| **dynamic** | 2.95 ± 0.21 | **70 / 202** | 20 | 185 |

**What it shows**

1. **The dynamic controller (W\* = 50 ms, pad then shrink) does not raise throughput.** It is
   a latency controller.
   - Traces: ~70 ms RTT against 0.3–6.7 s for every static cap, at 6–22% less throughput.
   - Fading: −48% throughput vs static 203k.
2. **Fading: the ramp-down causes it.**
   - Within each 200 ms dip the controller shrinks toward μ(MCS 10)·W\* = 59 kB while the
     parked data is still queued. That forces drops (478 retransmits vs 145), so CUBIC backs
     off every second; the result looks like static 64k.
   - Static 203k never shrinks: queue + the ~86 kB excess (~146 kB) fits, so it rides through
     the dips with no loss at 81 ms.
   - → Shrinking is right only for **sustained** drops. For short dips the pad should stay
     (this confirms the timescale argument in section B).
3. **Traces: W\* = 50 ms is under one BDP at low MCS.**
   - At 2–5 Mbit/s, μ·50 ms = 12–30 kB, but the base RTT at low MCS is 40–60 ms, and CUBIC
     needs ~2 BDP to keep the link full.
   - The large static caps buy +20% throughput with seconds of delay.
   - Static 4096 SDU never filled (0–1 retransmits); it is limited elsewhere, likely the UE
     receive window.

**Next variants (not run yet)**
- **Asymmetric pad:** raise the cap at once on a drop; shrink only if the lower rate persists
  > ~1 s, then slowly.
- **W\* tied to base RTT:** W\* ≈ 2·RTT₀(MCS), or simply `dynW100`.
