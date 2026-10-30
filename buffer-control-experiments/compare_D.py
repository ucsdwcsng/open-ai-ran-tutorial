#!/usr/bin/env python3
"""Static vs dynamic RLC cap: summary over repeats (results/expD_<scenario>_<policy>_r<k>).

    python3 buffer-control-experiments/compare_D.py

Per scenario: a table per policy (mean +- sd over repeats) of download throughput, TCP RTT
p50/p95, RLC queuing delay p50; and results/expD_<scenario>_tradeoff.png, throughput vs RTT
with one point per policy (error bars = sd over repeats). Up and to the left is better.
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
SDU = {"735k": "512 SDU", "1470k": "1024 SDU", "2940k": "2048 SDU", "5880k": "4096 SDU"}


def order(p):
    return 1e9 if p == "dyn" else float(p[:-1])


def label(p):
    return "dynamic" if p == "dyn" else (SDU[p] + f" ({p})" if p in SDU else p)


def qdelay(run, a, b):
    with open(os.path.join(run, "timeseries.csv")) as f:
        v = [float(r["value"]) for r in csv.DictReader(f) if r["series"] == "rlc_qdelay_ms" and a <= float(r["t"]) < b]
    return float(np.median(v)) if v else np.nan


def main():
    data = {}
    for d in glob.glob(os.path.join(RES, "expD_*_r*")):
        m = re.match(r"expD_([a-z]+)_([0-9a-z]+)_r(\d+)$", os.path.basename(d))
        if not m or not os.path.exists(os.path.join(d, "summary.json")):
            continue
        s = json.load(open(os.path.join(d, "summary.json")))
        w = s.get("window")
        if not w:
            continue
        b = w["flows"]["bulk"]
        data.setdefault(m.group(1), {}).setdefault(m.group(2), []).append(
            (b["tput_mbps"], b["srtt_p50_ms"], b["srtt_p95_ms"], qdelay(d, *w["t"]), b["retx"]))
    plotlib.style()
    for scen, pols in sorted(data.items()):
        print(f"\n== {scen} ==   (mean ± sd over repeats)")
        print(f"  {'policy':<22}{'n':>3}{'download Mb/s':>17}{'TCP RTT p50 ms':>18}{'p95 ms':>9}{'RLC qdelay p50':>16}{'retx':>7}")
        pts = []
        for p in sorted(pols, key=order):
            a = np.array([[np.nan if x is None else x for x in r] for r in pols[p]], dtype=float)
            mu, sd = np.nanmean(a, 0), np.nanstd(a, 0)
            pts.append((p, mu, sd))
            print(f"  {label(p):<22}{len(a):>3}{f'{mu[0]:.2f} ± {sd[0]:.2f}':>17}{f'{mu[1]:.0f} ± {sd[1]:.0f}':>18}"
                  f"{mu[2]:>9.0f}{mu[3]:>16.0f}{mu[4]:>7.0f}")
        best_static = max((x for x in pts if x[0] != "dyn"), key=lambda x: x[1][0], default=None)
        dyn = next((x for x in pts if x[0] == "dyn"), None)
        if dyn and best_static:
            print(f"  dynamic vs best static ({label(best_static[0])}): download "
                  f"{100 * (dyn[1][0] - best_static[1][0]) / best_static[1][0]:+.1f}%, "
                  f"RTT p50 {dyn[1][1]:.0f} vs {best_static[1][1]:.0f} ms")

        fig, ax = plt.subplots(figsize=(8.5, 5.5))
        for p, mu, sd in pts:
            dynp = p == "dyn"
            ax.errorbar(mu[1], mu[0], xerr=sd[1], yerr=sd[0], fmt="D" if dynp else "o", ms=10 if dynp else 8,
                        color=plotlib.C_BULK if dynp else plotlib.C_VIDEO, ecolor=plotlib.INK3, capsize=3,
                        mec="white", mew=1.5, zorder=3)
            ax.annotate(label(p), (mu[1], mu[0]), xytext=(8, 6), textcoords="offset points",
                        fontsize=9.5, color=plotlib.INK, fontweight="bold" if dynp else "normal")
        ax.set_xscale("log")
        ax.set_xlabel("TCP RTT p50 (ms, log)")
        ax.set_ylabel("Download throughput (Mbit/s)")
        ax.set_ylim(bottom=0)
        ax.set_title(f"'{scen}': static caps (blue) vs dynamic controller (orange); up-left is better",
                     loc="left", color=plotlib.INK, fontsize=11)
        fig.tight_layout()
        out = os.path.join(RES, f"expD_{scen}_tradeoff.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"  plot: {out}")


if __name__ == "__main__":
    main()
