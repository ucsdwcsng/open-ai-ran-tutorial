#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
#
# Extract the CC x buffer-size campaign from VictoriaMetrics and plot.
#
# Reads telemetry/cc_manifest.json {cc: [t_start_s, t_end_s]} and queries VM for
#   upt_goodput_mbps{ue,cc}         (per-UE QUIC goodput, from cc_campaign.py)
#   upt_rlc_queue_latency_ms{ue}    (RAN RLC queuing latency ~ RTT)
#   upt_ctrl_byte_limit             (the buffer setpoint the codelet is applying)
#
# Bins each sample by (cc from the manifest time window, buffer size from the
# setpoint), discarding TRANSITION_S after every setpoint change. Emits:
#   plots/throughput_vs_buffer.png, plots/latency_vs_buffer.png,
#   plots/tradeoff.png  and  campaign_results.json (the table).

import json, os, urllib.request, urllib.parse
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
VM = os.environ.get("UPT_VM_URL", "http://localhost:30491")
MANIFEST = os.path.join(HERE, "cc_manifest.json")
PLOTS = os.path.join(HERE, "plots"); os.makedirs(PLOTS, exist_ok=True)
TRANSITION_S = 30                       # drop this long after each setpoint change
SIZES = [100000, 200000, 400000, 700000, 1200000, 2000000, 3500000, 6172672]
CC_COLORS = {"newreno": "#4c72b0", "cubic": "#dd8452", "bbr": "#55a868", "prague": "#c44e52"}


def qrange(expr, start, end, step=2):
    u = f"{VM}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": int(start), "end": int(end), "step": step})
    return json.load(urllib.request.urlopen(u, timeout=30))["data"]["result"]


def snap(sp):
    return min(SIZES, key=lambda s: abs(s - sp))


def main():
    man = json.load(open(MANIFEST))
    ccs = list(man.keys())
    t0 = min(v[0] for v in man.values()) - 5
    t1 = max(v[1] for v in man.values()) + 5

    def cc_at(ts):
        for cc, (a, b) in man.items():
            if a <= ts <= b:
                return cc
        return None

    # setpoint timeline + change times
    sp_series = []
    for r in qrange("upt_ctrl_byte_limit", t0, t1):
        for ts, v in r["values"]:
            sp_series.append((float(ts), snap(float(v))))
    sp_series.sort()
    changes = [sp_series[0][0]] if sp_series else []
    for i in range(1, len(sp_series)):
        if sp_series[i][1] != sp_series[i - 1][1]:
            changes.append(sp_series[i][0])

    def sp_at(ts):
        best = None
        for s, v in sp_series:
            if s <= ts:
                best = v
            else:
                break
        return best

    def in_transition(ts):
        prev = [c for c in changes if c <= ts]
        return bool(prev) and (ts - prev[-1] < TRANSITION_S)

    # accumulate: (cc,size) -> {goodput_sum_by_ue, latency}
    from collections import defaultdict
    gp = defaultdict(lambda: defaultdict(list))   # (cc,size) -> ue -> [mbps]
    lat = defaultdict(list)                        # (cc,size) -> [ms]

    for r in qrange("upt_goodput_mbps", t0, t1):
        ue = r["metric"].get("ue")
        for ts, v in r["values"]:
            ts = float(ts); cc = cc_at(ts)
            if not cc or in_transition(ts):
                continue
            size = sp_at(ts)
            if size is None:
                continue
            gp[(cc, size)][ue].append(float(v))

    for r in qrange("upt_rlc_queue_latency_ms", t0, t1):
        for ts, v in r["values"]:
            ts = float(ts); cc = cc_at(ts)
            if not cc or in_transition(ts):
                continue
            size = sp_at(ts)
            if size is None:
                continue
            lat[(cc, size)].append(float(v))

    def mean(x):
        return sum(x) / len(x) if x else float("nan")

    # results table: total goodput (sum of per-UE means) + mean latency
    results = {}
    for cc in ccs:
        results[cc] = {}
        for s in SIZES:
            ues = gp[(cc, s)]
            total = sum(mean(ues[u]) for u in ues) if ues else float("nan")
            results[cc][s] = {
                "goodput_total_mbps": round(total, 3),
                "goodput_per_ue_mbps": {u: round(mean(v), 3) for u, v in ues.items()},
                "rlc_latency_ms": round(mean(lat[(cc, s)]), 1),
                "n_gp": sum(len(v) for v in ues.values()),
            }
    json.dump(results, open(os.path.join(HERE, "campaign_results.json"), "w"), indent=2)

    sizes_kb = [s / 1000 for s in SIZES]

    # 1) throughput vs buffer
    plt.figure(figsize=(7, 4.5))
    for cc in ccs:
        y = [results[cc][s]["goodput_total_mbps"] for s in SIZES]
        plt.plot(sizes_kb, y, "-o", label=cc, color=CC_COLORS.get(cc))
    plt.xscale("log"); plt.xlabel("RLC buffer limit (KB, log)"); plt.ylabel("Total goodput, 2 UEs (Mbps)")
    plt.title("Throughput vs RLC buffer size, by congestion control (2 UEs, DL)")
    plt.grid(True, which="both", alpha=0.3); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(PLOTS, "throughput_vs_buffer.png"), dpi=130); plt.close()

    # 2) latency vs buffer
    plt.figure(figsize=(7, 4.5))
    for cc in ccs:
        y = [results[cc][s]["rlc_latency_ms"] for s in SIZES]
        plt.plot(sizes_kb, y, "-o", label=cc, color=CC_COLORS.get(cc))
    plt.xscale("log"); plt.yscale("log")
    plt.xlabel("RLC buffer limit (KB, log)"); plt.ylabel("RLC queuing latency ~ RTT (ms, log)")
    plt.title("Latency vs RLC buffer size, by congestion control (2 UEs, DL)")
    plt.grid(True, which="both", alpha=0.3); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(PLOTS, "latency_vs_buffer.png"), dpi=130); plt.close()

    # 3) latency-throughput tradeoff (each point a buffer size)
    plt.figure(figsize=(7, 4.5))
    for cc in ccs:
        x = [results[cc][s]["rlc_latency_ms"] for s in SIZES]
        y = [results[cc][s]["goodput_total_mbps"] for s in SIZES]
        plt.plot(x, y, "-o", label=cc, color=CC_COLORS.get(cc))
    plt.xscale("log"); plt.xlabel("RLC queuing latency ~ RTT (ms, log)")
    plt.ylabel("Total goodput (Mbps)")
    plt.title("Latency-throughput tradeoff across buffer sizes (2 UEs, DL)")
    plt.grid(True, which="both", alpha=0.3); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(PLOTS, "tradeoff.png"), dpi=130); plt.close()

    # console table
    print(f"{'CC':>8} {'buf(KB)':>8} {'goodput':>8} {'lat(ms)':>8} {'n':>5}")
    for cc in ccs:
        for s in SIZES:
            r = results[cc][s]
            print(f"{cc:>8} {s/1000:>8.0f} {r['goodput_total_mbps']:>8.2f} {r['rlc_latency_ms']:>8.0f} {r['n_gp']:>5}")
    print(f"\nplots -> {PLOTS}")


if __name__ == "__main__":
    main()
