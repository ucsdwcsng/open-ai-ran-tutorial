#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
#
# Run 3: single-UE dynamic RLC buffer control. The rlc_ctrl codelet sweeps the
# DL byte-limit for du_ue_index 0 (ue1) ONLY; du_ue_index 1 (ue2) is left at
# srsRAN's default (uncontrolled baseline). Both UEs run a bbr QUIC download.
#
# Plots each metric per-UE vs the swept buffer size: the CONTROLLED UE (ue0)
# traces the bufferbloat curve, the BASELINE UE (ue1) is ~flat (its buffer never
# changes; "buffer size" on its x is just the concurrent setpoint).
#   plots3/*.png + campaign3_results.json

import json, os, urllib.request, urllib.parse
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
VM = os.environ.get("UPT_VM_URL", "http://localhost:30491")
MANIFEST = os.path.join(HERE, "cc_manifest3.json")
PLOTS = os.path.join(HERE, "plots3"); os.makedirs(PLOTS, exist_ok=True)
TRANSITION_S = 35
SIZES = [10000, 20000, 40000, 80000, 160000, 320000, 640000, 1280000]
UE_LABEL = {"0": "ue1 (swept)", "1": "ue2 (baseline)"}
UE_COLOR = {"0": "#c44e52", "1": "#4c72b0"}


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
    t0 = min(v[0] for v in man.values()) - 5
    t1 = max(v[1] for v in man.values()) + 5

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

    # metric -> {ue -> {size -> [values]}}
    def bin_metric(expr):
        out = defaultdict(lambda: defaultdict(list))
        for r in qrange(expr, t0, t1):
            ue = r["metric"].get("ue")
            if ue is None:
                continue
            for ts, v in r["values"]:
                ts = float(ts)
                if in_transition(ts):
                    continue
                size = sp_at(ts)
                if size is None:
                    continue
                out[ue][size].append(float(v))
        return out

    gp = bin_metric("upt_goodput_mbps")
    srtt = bin_metric("quic_smoothed_rtt_ms")
    minrtt = bin_metric("quic_min_rtt_ms")
    rlclat = bin_metric("upt_rlc_queue_latency_ms")
    occ = bin_metric("upt_rlc_buffer_bytes_max")

    ues = sorted(set(gp) | set(srtt) | set(occ))
    results = {}
    for ue in ues:
        results[ue] = {}
        for s in SIZES:
            results[ue][s] = {
                "goodput_mbps": round(mean(gp[ue][s]), 3),
                "quic_srtt_ms": round(mean(srtt[ue][s]), 2),
                "quic_min_rtt_ms": round(mean(minrtt[ue][s]), 2),
                "rlc_latency_ms": round(mean(rlclat[ue][s]), 2),
                "rlc_occ_kb": round(mean(occ[ue][s]) / 1000, 1),
                "n": len(gp[ue][s]),
            }
    json.dump(results, open(os.path.join(HERE, "campaign3_results.json"), "w"), indent=2)

    kb = [s / 1000 for s in SIZES]

    def lineplot(fname, key, ylabel, title, ylog=False, ident=False):
        plt.figure(figsize=(7, 4.5))
        for ue in ues:
            y = [results[ue][s][key] for s in SIZES]
            plt.plot(kb, y, "-o", label=UE_LABEL.get(ue, f"ue{ue}"), color=UE_COLOR.get(ue))
        if ident:   # y=x reference: occupancy == setpoint (perfect clipping)
            plt.plot(kb, kb, "--", color="gray", alpha=0.6, label="occ = limit")
        plt.xscale("log")
        if ylog:
            plt.yscale("log")
        plt.xlabel("RLC DL byte-limit swept on ue1 (KB, log)"); plt.ylabel(ylabel)
        plt.title(title); plt.grid(True, which="both", alpha=0.3); plt.legend()
        plt.tight_layout(); plt.savefig(os.path.join(PLOTS, fname), dpi=130); plt.close()

    lineplot("goodput_vs_buffer.png", "goodput_mbps", "DL goodput (Mbps)",
             "Per-UE DL goodput vs ue1 buffer limit (single-UE control)")
    lineplot("quic_rtt_vs_buffer.png", "quic_srtt_ms", "QUIC smoothed RTT (ms)",
             "Per-UE QUIC RTT vs ue1 buffer limit", ylog=True)
    lineplot("rlc_latency_vs_buffer.png", "rlc_latency_ms", "RLC queuing latency (ms)",
             "Per-UE RLC queuing latency vs ue1 buffer limit", ylog=True)
    lineplot("rlc_occupancy_vs_buffer.png", "rlc_occ_kb", "RLC queue occupancy (KB)",
             "Per-UE RLC occupancy vs ue1 buffer limit (proof of control)", ident=True)

    print(f"{'ue':>14} {'buf(KB)':>8} {'goodput':>8} {'sRTT':>7} {'rlcLat':>7} {'occ(KB)':>8} {'n':>4}")
    for ue in ues:
        for s in SIZES:
            r = results[ue][s]
            print(f"{UE_LABEL.get(ue,ue):>14} {s/1000:>8.0f} {r['goodput_mbps']:>8.2f} "
                  f"{r['quic_srtt_ms']:>7.1f} {r['rlc_latency_ms']:>7.1f} {r['rlc_occ_kb']:>8.1f} {r['n']:>4}")
    print(f"\nplots -> {PLOTS}")


if __name__ == "__main__":
    main()
