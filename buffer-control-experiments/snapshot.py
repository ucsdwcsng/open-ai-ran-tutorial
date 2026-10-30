#!/usr/bin/env python3
"""snapshot - plot what just happened in an interactive session (students load codelets by hand).

    python3 buffer-control-experiments/snapshot.py                 # last 10 minutes
    python3 buffer-control-experiments/snapshot.py --minutes 5 --title "my run"

Reads from VictoriaMetrics: per-flow RTT and throughput (flowmon.py), RLC queue bytes and
queuing delay (upt codelets). Reads cap changes from the event log that bufcap.sh writes.
Writes results/session_<time>/{timeline.png, summary.json} and prints a per-cap table.
"""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request

import numpy as np

import plotlib

HERE = os.path.dirname(os.path.abspath(__file__))
VM = "http://localhost:30491"
EVENTS = os.path.join(HERE, ".bufcap_events.jsonl")
LABELS = {"video": "video call (2 Mbit/s)", "bulk": "download"}


def q(expr, t0, t1, step=0.5):
    u = f"{VM}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": t0, "end": t1, "step": f"{step}s"})
    return json.load(urllib.request.urlopen(u, timeout=30))["data"]["result"]


def series(res, **match):
    for r in res:
        if all(r["metric"].get(k) == v for k, v in match.items()):
            return [(float(t), float(v)) for t, v in r["values"]]
    return []


def cap_label(text):
    return "no cap" if text.startswith("no cap") else text.replace("cap ", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--title", default="Demo 3: buffer caps")
    ap.add_argument("--ue", default=None,
                    help="DU UE index for the RLC metrics (default: the max over UEs, i.e. the one with traffic)")
    a = ap.parse_args()
    t1 = time.time()
    t0 = t1 - a.minutes * 60

    rtt = q('flow_rtt_ms', t0, t1)
    tput = q('avg_over_time(flow_tput_mbps[2s])', t0, t1)
    rlc = (lambda m: f'{m}{{bearer="DRB1",ue="{a.ue}"}}') if a.ue is not None else \
        (lambda m: f'max without (ue) ({m}{{bearer="DRB1"}})')
    qb = series(q(rlc("upt_rlc_buffer_bytes"), t0, t1))
    ql = series(q(rlc("upt_rlc_queue_latency_ms"), t0, t1))
    flows = {}
    for name in ("video", "bulk"):
        r, tp = series(rtt, flow=name), series(tput, flow=name)
        if r or tp:
            flows[name] = {"label": LABELS[name], "rtt": r, "tput": tp}
    if not flows:
        raise SystemExit("no flow metrics in this window -- is flowmon running? (bash flowmon.sh start)")

    # cap in force over the window, from bufcap.sh's event log
    evs = []
    if os.path.exists(EVENTS):
        evs = [json.loads(l) for l in open(EVENTS) if l.strip()]
    evs = [e for e in evs if e.get("limit_kb") is not None]
    before = [e for e in evs if e["t"] < t0]
    caps = [(t0, cap_label(before[-1]["text"]), before[-1]["limit_kb"]) if before else (t0, "no cap", 6028)]   # OCUDU default queue
    caps += [(e["t"], cap_label(e["text"]), e["limit_kb"]) for e in evs if t0 <= e["t"] <= t1]

    # shift to seconds from the start of the window
    sh = lambda s: [(t - t0, v) for t, v in s]
    for f in flows.values():
        f["rtt"], f["tput"] = sh(f["rtt"]), sh(f["tput"])
    caps_rel = [(t - t0, lab, lim) for t, lab, lim in caps]

    out = os.path.join(HERE, "results", time.strftime("session_%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    plotlib.timeline(f"{out}/timeline.png", a.title, flows, sh(qb), sh(ql), caps_rel, t1 - t0,
                     note="Both flows share one RLC queue on the UE's bearer, so the video call "
                          "waits behind whatever the download has queued.")

    # per-cap summary (first 10 s after each change skipped)
    rows = []
    for k, (t, lab, lim) in enumerate(caps_rel):
        te = caps_rel[k + 1][0] if k + 1 < len(caps_rel) else t1 - t0
        w = lambda s: np.array([v for x, v in s if t + 10 <= x < te])
        row = {"cap": lab.replace("\n", " / "), "t": [round(t), round(te)]}
        for n, f in flows.items():
            r, tp = w(f["rtt"]), w(f["tput"])
            row[n] = {"rtt_p50_ms": round(float(np.median(r)), 1) if len(r) else None,
                      "rtt_p95_ms": round(float(np.percentile(r, 95)), 1) if len(r) else None,
                      "tput_mbps": round(float(np.mean(tp)), 2) if len(tp) else None}
        rows.append(row)
    json.dump(rows, open(f"{out}/summary.json", "w"), indent=1)
    wcap = max(9, max(len(r["cap"]) for r in rows) + 2)
    print(f"  {'time (s)':<12}{'cap':<{wcap}}" + "".join(f"{n + ' RTT p50/p95':>22}{n + ' Mb/s':>12}" for n in flows))
    for r in rows:
        print(f"  {r['t'][0]:>4}-{r['t'][1]:<7}{r['cap']:<{wcap}}" + "".join(
            f"{str(r[n]['rtt_p50_ms']) + '/' + str(r[n]['rtt_p95_ms']):>22}{str(r[n]['tput_mbps']):>12}"
            for n in flows))
    print(f"  plot: {out}/timeline.png")


if __name__ == "__main__":
    main()
