#!/usr/bin/env python3
"""Compare static vs dynamic buffer runs of experiment B.

    python3 buffer-control-experiments/compare_B.py        # all results/expB_static*, results/expB_dynamic*

Prints the window totals per run and per policy (mean over repeats), and writes
results/expB_compare.png: download throughput and video RTT over time, one line per run,
with the radio schedule shaded.
"""
import csv
import glob
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import plotlib

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
POLICY = {"static": ("static cap 128k", plotlib.C_BULK), "dynamic": ("dynamic cap (follows radio)", plotlib.C_VIDEO)}


def series(run, name):
    out = []
    with open(os.path.join(run, "timeseries.csv")) as f:
        for r in csv.DictReader(f):
            if r["series"] == name:
                out.append((float(r["t"]), float(r["value"])))
    return out


def smooth(s, k=4):
    if len(s) < k:
        return s
    t = [x for x, _ in s][k - 1:]
    v = np.convolve([y for _, y in s], np.ones(k) / k, mode="valid")
    return list(zip(t, v))


def main():
    runs = {p: sorted(glob.glob(os.path.join(RES, f"expB_{p}*"))) for p in POLICY}
    rows = {}
    print(f"  {'run':<22}{'download Mb/s':>14}{'MB':>8}{'video RTT p50/p95':>20}{'bulk retx':>11}")
    for p, dirs in runs.items():
        for d in dirs:
            s = json.load(open(os.path.join(d, "summary.json")))
            w = s.get("window", {}).get("flows", {})
            b, v = w.get("bulk", {}), w.get("video", {})
            rows.setdefault(p, []).append((b.get("tput_mbps"), b.get("mbytes"), v.get("srtt_p50_ms"),
                                           v.get("srtt_p95_ms"), b.get("retx")))
            print(f"  {os.path.basename(d):<22}{b.get('tput_mbps'):>14}{b.get('mbytes'):>8}"
                  f"{str(v.get('srtt_p50_ms')) + ' / ' + str(v.get('srtt_p95_ms')):>20}{b.get('retx'):>11}")
    print()
    for p, r in rows.items():
        a = np.array([[x if x is not None else np.nan for x in row] for row in r], dtype=float)
        m = np.nanmean(a, axis=0)
        print(f"  MEAN {p:<17}{m[0]:>14.2f}{m[1]:>8.1f}{f'{m[2]:.0f} / {m[3]:.0f}':>20}{m[4]:>11.0f}   (n={len(r)})")
    if all(p in rows for p in POLICY):
        s, d = (np.nanmean([x[0] for x in rows[p]]) for p in ("static", "dynamic"))
        print(f"\n  dynamic vs static download: {100 * (d - s) / s:+.1f}%")

    # ---- overlay plot
    ref = (runs.get("static") or runs.get("dynamic"))[0]
    cfg = json.load(open(os.path.join(ref, "events.json")))["config"]
    steps = [(e["t"], e["radio"]) for e in cfg["events"] if "radio" in e]
    plotlib.style()
    fig, ax = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True)
    for p, dirs in runs.items():
        label, color = POLICY[p]
        for i, d in enumerate(dirs):
            kw = dict(color=color, lw=1.6, alpha=0.9 if i == 0 else 0.45, label=label if i == 0 else None)
            tp = smooth(series(d, "bulk_mbps"))
            ax[0].plot([t for t, _ in tp], [v for _, v in tp], **kw)
            rt = smooth(series(d, "video_srtt"))
            ax[1].plot([t for t, _ in rt], [v for _, v in rt], **kw)
    w0, w1 = cfg["window"]
    for k, (t, lvl) in enumerate(steps):
        te = steps[k + 1][0] if k + 1 < len(steps) else w1
        for a_ in ax:
            if k % 2 == 0:
                a_.axvspan(t, te, color=plotlib.BAND, lw=0, zorder=0)
            a_.axvline(t, color=plotlib.INK3, lw=0.8, ls=":")
        ax[0].text((t + te) / 2, 1.02, "normal" if lvl == "good" else f"MCS≤{lvl}",
                   transform=ax[0].get_xaxis_transform(), ha="center", va="bottom",
                   fontsize=10, color=plotlib.INK, fontweight="bold")
    ax[0].set_ylabel("Download throughput (Mbit/s)")
    ax[1].set_ylabel("Video call RTT (ms)")
    ax[1].set_xlabel("Time (s); radio schedule starts at %d s" % w0)
    for a_ in ax:
        a_.set_ylim(bottom=0)
        a_.set_xlim(w0 - 10, w1 + 5)
        a_.legend(loc="upper right", frameon=False)
    fig.suptitle("Experiment B: static vs dynamic buffer under the same radio schedule "
                 "(lighter lines = repeats)", color=plotlib.INK, fontsize=12, x=0.01, ha="left", y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.965))
    out = os.path.join(RES, "expB_compare.png")
    fig.savefig(out, dpi=130)
    print(f"  plot: {out}")


if __name__ == "__main__":
    main()
