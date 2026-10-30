#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
#
# Plot Run 2 (small binding buffers + QUIC endpoint telemetry).
#
# Reads telemetry/cc_manifest2.json {cc:[t0,t1]} and queries VM for, per (cc,size):
#   upt_goodput_mbps{ue,cc}      per-UE goodput
#   quic_smoothed_rtt_ms{ue,cc}  QUIC's OWN smoothed RTT   (the real transport RTT)
#   quic_min_rtt_ms{ue,cc}       QUIC min RTT (~ base RTT)
#   quic_cwnd_bytes{ue,cc}       congestion window
#   quic_bytes_in_flight{ue,cc}  bytes in flight
#   quic_lost_total{ue,cc}       cumulative loss counter (per qlog) -> loss RATE
#   upt_rlc_buffer_bytes_max     actual RLC queue occupancy (sanity: is it binding?)
# binned by the live setpoint upt_ctrl_byte_limit, dropping TRANSITION_S per step.
#
# Emits plots2/*.png + campaign2_results.json.

import json, os, urllib.request, urllib.parse
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
VM = os.environ.get("UPT_VM_URL", "http://localhost:30491")
MANIFEST = os.path.join(HERE, "cc_manifest2.json")
PLOTS = os.path.join(HERE, "plots2"); os.makedirs(PLOTS, exist_ok=True)
TRANSITION_S = 30
SIZES = [5000, 10000, 20000, 40000, 60000, 80000, 150000, 400000]
CC_COLORS = {"newreno": "#4c72b0", "cubic": "#dd8452", "bbr": "#55a868", "prague": "#c44e52"}


def qrange(expr, start, end, step=2):
    u = f"{VM}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": int(start), "end": int(end), "step": step})
    return json.load(urllib.request.urlopen(u, timeout=60))["data"]["result"]


def snap(sp):
    return min(SIZES, key=lambda s: abs(s - sp))


def mean(x):
    return sum(x) / len(x) if x else float("nan")


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

    # setpoint timeline
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

    # generic binner: metric -> {(cc,size): [values]} (optionally per-ue summed)
    def bin_metric(expr, per_ue=False):
        out = defaultdict(lambda: defaultdict(list)) if per_ue else defaultdict(list)
        for r in qrange(expr, t0, t1):
            ue = r["metric"].get("ue")
            for ts, v in r["values"]:
                ts = float(ts); cc = cc_at(ts)
                if not cc or in_transition(ts):
                    continue
                size = sp_at(ts)
                if size is None:
                    continue
                if per_ue:
                    out[(cc, size)][ue].append(float(v))
                else:
                    out[(cc, size)].append(float(v))
        return out

    gp = bin_metric("upt_goodput_mbps", per_ue=True)
    srtt = bin_metric("quic_smoothed_rtt_ms")
    minrtt = bin_metric("quic_min_rtt_ms")
    cwnd = bin_metric("quic_cwnd_bytes")
    inflight = bin_metric("quic_bytes_in_flight")
    occ = bin_metric("upt_rlc_buffer_bytes_max")
    # quic_lost_total is a per-qlog CUMULATIVE counter, so losses during one
    # setpoint window = (max-min) of that counter within the window, summed over
    # the two UEs (each UE has its own counter).
    lost = bin_metric("quic_lost_total", per_ue=True)

    def loss_in(cc, s):
        cell = lost[(cc, s)]
        return int(sum(max(v) - min(v) for v in cell.values() if v)) if cell else 0

    results = {}
    for cc in ccs:
        results[cc] = {}
        for s in SIZES:
            ues = gp[(cc, s)]
            total = sum(mean(ues[u]) for u in ues) if ues else float("nan")
            results[cc][s] = {
                "goodput_total_mbps": round(total, 3),
                "quic_srtt_ms": round(mean(srtt[(cc, s)]), 2),
                "quic_min_rtt_ms": round(mean(minrtt[(cc, s)]), 2),
                "cwnd_kb": round(mean(cwnd[(cc, s)]) / 1000, 1),
                "inflight_kb": round(mean(inflight[(cc, s)]) / 1000, 1),
                "rlc_occ_kb": round(mean(occ[(cc, s)]) / 1000, 1),
                "loss_events": loss_in(cc, s),
                "n": len(srtt[(cc, s)]),
            }
    json.dump(results, open(os.path.join(HERE, "campaign2_results.json"), "w"), indent=2)

    kb = [s / 1000 for s in SIZES]

    def lineplot(fname, ykey, ylabel, title, ylog=False):
        plt.figure(figsize=(7, 4.5))
        for cc in ccs:
            y = [results[cc][s][ykey] for s in SIZES]
            plt.plot(kb, y, "-o", label=cc, color=CC_COLORS.get(cc))
        plt.xscale("log")
        if ylog:
            plt.yscale("log")
        plt.xlabel("RLC buffer limit (KB, log)"); plt.ylabel(ylabel)
        plt.title(title); plt.grid(True, which="both", alpha=0.3); plt.legend()
        plt.tight_layout(); plt.savefig(os.path.join(PLOTS, fname), dpi=130); plt.close()

    lineplot("throughput_vs_buffer.png", "goodput_total_mbps",
             "Total goodput, 2 UEs (Mbps)",
             "Throughput vs RLC buffer (small binding buffers, 2 UEs DL)")
    lineplot("quic_rtt_vs_buffer.png", "quic_srtt_ms",
             "QUIC smoothed RTT (ms)",
             "QUIC endpoint RTT vs RLC buffer, by CC (2 UEs DL)", ylog=True)
    lineplot("cwnd_vs_buffer.png", "cwnd_kb",
             "QUIC cwnd (KB)", "QUIC congestion window vs RLC buffer, by CC")
    lineplot("loss_vs_buffer.png", "loss_events",
             "QUIC loss events (per flow, per step)",
             "QUIC loss vs RLC buffer, by CC (2 UEs DL)")

    # tradeoff: real QUIC RTT vs goodput
    plt.figure(figsize=(7, 4.5))
    for cc in ccs:
        x = [results[cc][s]["quic_srtt_ms"] for s in SIZES]
        y = [results[cc][s]["goodput_total_mbps"] for s in SIZES]
        plt.plot(x, y, "-o", label=cc, color=CC_COLORS.get(cc))
        for s, xx, yy in zip(SIZES, x, y):
            plt.annotate(f"{s//1000}K", (xx, yy), fontsize=6, alpha=0.6)
    plt.xscale("log"); plt.xlabel("QUIC smoothed RTT (ms, log)")
    plt.ylabel("Total goodput (Mbps)")
    plt.title("QUIC latency-throughput tradeoff across buffer sizes (2 UEs DL)")
    plt.grid(True, which="both", alpha=0.3); plt.legend()
    plt.tight_layout(); plt.savefig(os.path.join(PLOTS, "tradeoff.png"), dpi=130); plt.close()

    # console table
    print(f"{'CC':>8} {'buf(KB)':>8} {'goodput':>8} {'sRTT':>7} {'minRTT':>7} "
          f"{'cwnd(KB)':>9} {'occ(KB)':>8} {'loss':>6} {'n':>5}")
    for cc in ccs:
        for s in SIZES:
            r = results[cc][s]
            print(f"{cc:>8} {s/1000:>8.0f} {r['goodput_total_mbps']:>8.2f} "
                  f"{r['quic_srtt_ms']:>7.1f} {r['quic_min_rtt_ms']:>7.1f} "
                  f"{r['cwnd_kb']:>9.1f} {r['rlc_occ_kb']:>8.1f} {r['loss_events']:>6} {r['n']:>5}")
    print(f"\nplots -> {PLOTS}")


if __name__ == "__main__":
    main()
