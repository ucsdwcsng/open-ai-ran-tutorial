#!/usr/bin/env python3
"""Compare static RLC caps under the replayed mobility traces.

    python3 buffer-control-experiments/compare_T.py     # all results/expT_<trace>_<N>sdu*

Per trace: a table (download, data, TCP RTT p50/p95, mean RLC queue, retransmits) per cap,
and results/expT_<trace>_compare.png: the trace's MCS, download throughput and TCP RTT over
time, one line per cap.
"""
import csv
import glob
import json
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import plotlib

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
# sequential single hue, light -> dark = small -> large buffer
SEQ = ["#9ec5f0", "#5a9ae3", "#2a78d6", "#154a8f"]


def series(run, name):
    with open(os.path.join(run, "timeseries.csv")) as f:
        return [(float(r["t"]), float(r["value"])) for r in csv.DictReader(f) if r["series"] == name]


def smooth(s, k=4):
    if len(s) < k:
        return s
    v = np.convolve([y for _, y in s], np.ones(k) / k, mode="valid")
    return list(zip([x for x, _ in s][k - 1:], v))


def main():
    runs = {}
    for d in glob.glob(os.path.join(RES, "expT_*sdu*")):
        m = re.match(r"expT_([a-z]+)_(\d+)sdu", os.path.basename(d))
        if m and os.path.exists(os.path.join(d, "summary.json")):
            runs.setdefault(m.group(1), []).append((int(m.group(2)), d))
    plotlib.style()
    for trace, lst in sorted(runs.items()):
        lst.sort()
        print(f"\n== trace {trace} ==")
        print(f"  {'cap':<18}{'download':>10}{'MB':>7}{'TCP RTT p50/p95':>18}{'RLC q kB':>10}{'qdelay p50':>11}{'retx':>6}")
        for sdus, d in lst:
            s = json.load(open(os.path.join(d, "summary.json")))
            w = s["window"]["flows"]["bulk"]
            ev = json.load(open(os.path.join(d, "events.json")))
            t0, (wa, wb) = ev["t0"], s["window"]["t"]
            q = [v for t, v in series(d, "rlc_queue_bytes") if wa <= t < wb]
            ql = [v for t, v in series(d, "rlc_qdelay_ms") if wa <= t < wb]
            cap = f"{sdus} SDU ({sdus * 1470 // 1024}k)"
            print(f"  {cap:<18}{w['tput_mbps']:>10}{w['mbytes']:>7}"
                  f"{str(w['srtt_p50_ms']) + ' / ' + str(w['srtt_p95_ms']):>18}"
                  f"{np.mean(q) / 1e3 if q else float('nan'):>10.0f}{np.median(ql) if ql else float('nan'):>11.0f}{w['retx']:>6}")

        trace_csv = os.path.join(HERE, "traces", f"{trace}.csv")
        mcs = [int(r["mcs"]) for r in csv.DictReader(open(trace_csv))]
        # the trace starts when the radio event completes
        ev = json.load(open(os.path.join(lst[0][1], "events.json")))
        tstart = next(a["t_actual"] for a in ev["applied"] if a["kind"] == "radio")
        fig, ax = plt.subplots(3, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [0.7, 1, 1]})
        ax[0].step([tstart + i for i in range(len(mcs))], mcs, where="post", color=plotlib.INK2, lw=1.8)
        ax[0].set_ylabel("Trace DL MCS")
        for k, (sdus, d) in enumerate(lst):
            lab = f"{sdus} SDU ({sdus * 1470 // 1024} kB)"
            tp = smooth(series(d, "bulk_mbps"))
            rt = series(d, "bulk_srtt")
            ax[1].plot([t for t, _ in tp], [v for _, v in tp], color=SEQ[k % 4], lw=1.8, label=lab)
            ax[2].plot([t for t, _ in rt], [v / 1000 for _, v in rt], color=SEQ[k % 4], lw=1.8, label=lab)
        ax[1].set_ylabel("Download (Mbit/s)")
        ax[2].set_ylabel("TCP RTT (s)")
        ax[2].set_xlabel("Time (s)")
        for a_ in ax[1:]:
            a_.set_ylim(bottom=0)
            a_.legend(loc="upper right", frameon=False, fontsize=9)
        fig.suptitle(f"Mobility trace '{trace}' replayed on the gNB: static RLC cap per run (CUBIC)",
                     color=plotlib.INK, fontsize=12, x=0.01, ha="left", y=0.995)
        fig.tight_layout(rect=(0, 0, 1, 0.97))
        out = os.path.join(RES, f"expT_{trace}_compare.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"  plot: {out}")


if __name__ == "__main__":
    main()
