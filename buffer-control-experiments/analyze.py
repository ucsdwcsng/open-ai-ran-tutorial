#!/usr/bin/env python3
"""Turn a runner.py result directory into summary.json, timeseries.csv and plots.

    python3 analyze.py results/<name>

Phases: the run is cut at every flow start and every control event, so each phase is one
(cap, set of active flows). The first `settle` seconds of each phase are skipped: TCP needs
time to react to a new limit.

Outputs:
  summary.json     per phase, per flow: throughput, srtt p50/p95, retransmits;
                   per phase: RLC queue bytes and queuing delay
  timeseries.csv   long format: series,t,value
  timeline.png     latency per flow / throughput per flow / RLC buffer vs cap (plotlib.py)
  cap_curve.png    one point per phase with every flow active: srtt of each flow vs cap,
                   and throughput vs cap (the latency-throughput trade-off)
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import plotlib

# categorical slots 1-4, fixed order (validated reference palette)
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def load_json(path):
    """Last JSON object in a file (an appended log can hold several)."""
    try:
        txt = open(path).read()
    except OSError:
        return None
    dec, i, obj = json.JSONDecoder(), 0, None
    while True:
        i = txt.find("{", i)
        if i < 0:
            return obj
        try:
            obj, i = dec.raw_decode(txt, i)
        except ValueError:
            return obj


def iperf_series(j, key):
    """[(epoch_mid, value)] from an iperf3 -J log: mbps, rtt (ms), snd_cwnd (B), retransmits."""
    if not j or "intervals" not in j:
        return []
    ts = j["start"]["timestamp"]["timesecs"]
    out = []
    for iv in j["intervals"]:
        s = iv["sum"]
        mid = ts + (s["start"] + s["end"]) / 2
        if key == "mbps":
            out.append((mid, s["bits_per_second"] / 1e6))
        else:
            st = iv["streams"][0]
            if key in st:
                out.append((mid, st[key] / 1000.0 if key == "rtt" else st[key]))
    return out


def vm_series(vm, metric, du):
    for r in vm.get(metric, []):
        if str(r["metric"].get("ue")) == str(du):
            return [(float(t), float(v)) for t, v in r["values"]]
    return []


def window(series, a, b):
    return np.array([v for t, v in series if a <= t < b])


def pct(x, q, nd=1):
    return round(float(np.percentile(x, q)), nd) if len(x) else None


def mean(x, nd=2):
    return round(float(np.mean(x)), nd) if len(x) else None


def main(out):
    ev = json.load(open(f"{out}/events.json"))
    t0, cfg = ev["t0"], ev["config"]
    raw = f"{out}/raw"
    vm = load_json(f"{raw}/vm.json") or {}
    settle = cfg.get("settle", 10)
    dur = cfg["duration"]
    du = cfg["ues"][0].get("du_index", 0)

    flows = []
    for f in cfg["flows"]:
        p = f["port"]
        tx = load_json(f"{raw}/tx_{p}.json")
        flows.append({
            "name": f["name"], "label": f["label"],
            "mbps": iperf_series(load_json(f"{raw}/rx_{p}.json"), "mbps"),
            "srtt": iperf_series(tx, "rtt"),
            "cwnd": iperf_series(tx, "snd_cwnd"),
            "retx": iperf_series(tx, "retransmits"),
        })
    qbytes = vm_series(vm, "upt_rlc_buffer_bytes", du)
    qlat = vm_series(vm, "upt_rlc_queue_latency_ms", du)

    # ---- phases
    cuts, cap, chan, active = [], "no cap", "", []
    for a in sorted(ev["applied"], key=lambda x: x["t_actual"]):
        if a["kind"] == "flow_start":
            active.append(a["flow"])
        elif a["kind"] == "bufcap":
            cap = "no cap" if a["arg"] == "off" else a["arg"]
        elif a["kind"] == "radio":
            chan = "" if a["arg"] == "good" else f"MCS≤{a['arg']}"
        else:
            cap = a.get("label", "bufctl")
        if cuts and a["t_actual"] - cuts[-1][0] < 6.0 and a["kind"] != "flow_start":
            cuts[-1] = (cuts[-1][0], cap, chan, list(active))   # one step = cap + radio change
        else:
            cuts.append((a["t_actual"], cap, chan, list(active)))
    phases = []
    for k, (ts, cap, chan, act) in enumerate(cuts):
        te = cuts[k + 1][0] if k + 1 < len(cuts) else dur
        a, b = t0 + ts + settle, t0 + te
        ph = {"cap": cap, "channel": chan or "normal", "flows_active": act, "t": [round(ts, 1), round(te, 1)], "flows": {}}
        for f in flows:
            if f["name"] not in act:
                continue
            ph["flows"][f["name"]] = {
                "tput_mbps": mean(window(f["mbps"], a, b)),
                "srtt_p50_ms": pct(window(f["srtt"], a, b), 50),
                "srtt_p95_ms": pct(window(f["srtt"], a, b), 95),
                "retx": int(sum(window(f["retx"], a, b))),
            }
        q = window(qbytes, a, b)
        ph["rlc_queue_kB_mean"] = round(float(np.mean(q)) / 1e3, 1) if len(q) else None
        ph["rlc_qdelay_ms_p50"] = pct(window(qlat, a, b), 50)
        ph["rlc_qdelay_ms_p95"] = pct(window(qlat, a, b), 95)
        phases.append(ph)
    summary = {"name": cfg["name"], "settle_s": settle, "phases": phases}
    if cfg.get("window"):   # totals over a fixed window, e.g. the radio schedule
        wa, wb = t0 + cfg["window"][0], t0 + cfg["window"][1]
        summary["window"] = {"t": cfg["window"], "flows": {f["name"]: {
            "tput_mbps": mean(window(f["mbps"], wa, wb)),
            "mbytes": round(float(np.sum(window(f["mbps"], wa, wb))) * 0.5 / 8, 1),   # 0.5 s intervals
            "srtt_p50_ms": pct(window(f["srtt"], wa, wb), 50),
            "srtt_p95_ms": pct(window(f["srtt"], wa, wb), 95),
            "retx": int(sum(window(f["retx"], wa, wb)))} for f in flows}}
    json.dump(summary, open(f"{out}/summary.json", "w"), indent=1)

    with open(f"{out}/timeseries.csv", "w") as fh:
        fh.write("series,t,value\n")
        for f in flows:
            for m in ("mbps", "srtt", "cwnd", "retx"):
                for t, v in f[m]:
                    fh.write(f"{f['name']}_{m},{t - t0:.2f},{v}\n")
        for name, s in (("rlc_queue_bytes", qbytes), ("rlc_qdelay_ms", qlat)):
            for t, v in s:
                fh.write(f"{name},{t - t0:.2f},{v}\n")

    style()
    rel = lambda ser: [(t - t0, v) for t, v in ser]
    plotlib.timeline(
        f"{out}/timeline.png", cfg.get("title", cfg["name"]),
        {f["name"]: {"label": f["label"], "rtt": rel(f["srtt"]), "tput": rel(f["mbps"])} for f in flows},
        rel(qbytes), rel(qlat),
        [(ts, cap + (f"\n{chan}" if chan else "\nnormal" if any(c[2] for c in cuts) else ""), cap_kb(cap))
         for ts, cap, chan, _ in cuts], dur,
        note="Both flows share one RLC queue on the UE's bearer, so the video call waits behind "
             "whatever the download has queued.")
    plot_cap_curve(out, cfg, phases, [f["name"] for f in flows])
    print_summary(cfg, phases)
    if "window" in summary:
        w = summary["window"]
        print(f"  WINDOW {w['t'][0]}-{w['t'][1]} s: " + "  ".join(
            f"{n}: {v['tput_mbps']} Mb/s ({v['mbytes']} MB) srtt {v['srtt_p50_ms']}/{v['srtt_p95_ms']} ms retx {v['retx']}"
            for n, v in w["flows"].items()))


def cap_kb(label):
    """'256k' -> 256, '1m' -> 1024, 'no cap' -> the configured 8 MB queue."""
    if label == "no cap":
        return 8192
    try:
        return int(label[:-1]) * (1024 if label[-1] == "m" else 1)
    except (ValueError, IndexError):
        return None


def style():
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": INK2, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True,
                         "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
                         "axes.spines.top": False, "axes.spines.right": False})


def plot_cap_curve(out, cfg, phases, names):
    full = [p for p in phases if set(p["flows"]) == set(names)]
    if len(full) < 2:
        return
    x = np.arange(len(full))
    labels = [f"{p['cap']}\n@{p['t'][0]:.0f}s" for p in full]
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.6))
    for i, n in enumerate(names):
        p50 = [p["flows"][n]["srtt_p50_ms"] or 0 for p in full]
        p95 = [p["flows"][n]["srtt_p95_ms"] or 0 for p in full]
        off = (i - (len(names) - 1) / 2) * 0.18
        ax[0].errorbar(x + off, p50, yerr=[np.zeros(len(p50)), np.array(p95) - np.array(p50)],
                       fmt="o", ms=8, color=COLORS[i], capsize=3, lw=1.5, label=f"{n}: p50 (bar to p95)")
        tp = [p["flows"][n]["tput_mbps"] or 0 for p in full]
        ax[1].bar(x + off, tp, width=0.34, color=COLORS[i], label=n)
    ax[0].set_ylabel("TCP srtt (ms)")
    ax[0].set_title("Latency per buffer setting", loc="left", color=INK, fontsize=11)
    ax[1].set_ylabel("Throughput (Mbit/s)")
    ax[1].set_title("Throughput per buffer setting", loc="left", color=INK, fontsize=11)
    for a_ in ax:
        a_.set_xticks(x, labels)
        a_.set_ylim(bottom=0)
        a_.legend(frameon=False, loc="upper right")
    fig.suptitle(cfg.get("title", cfg["name"]), color=INK, fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(f"{out}/cap_curve.png", dpi=130)
    plt.close(fig)


def print_summary(cfg, phases):
    print(f"\n== {cfg['name']} ==")
    print(f"  {'phase':<22}{'cap':<8}{'RLC q kB':>9}{'qdelay p50/p95':>16}   flows")
    for p in phases:
        fl = "  ".join(f"{n}: {v['tput_mbps']} Mb/s srtt {v['srtt_p50_ms']}/{v['srtt_p95_ms']} ms retx {v['retx']}"
                       for n, v in p["flows"].items())
        qd = f"{p['rlc_qdelay_ms_p50']}/{p['rlc_qdelay_ms_p95']}"
        print(f"  {p['t'][0]:>5.0f}-{p['t'][1]:<5.0f}s{'':10}{p['cap']:<8}{str(p['rlc_queue_kB_mean']):>9}{qd:>16}   {fl}")


if __name__ == "__main__":
    main(sys.argv[1])
