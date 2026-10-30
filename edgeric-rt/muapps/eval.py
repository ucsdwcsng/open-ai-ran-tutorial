#!/usr/bin/env python3
"""Compare EdgeRIC-RT scheduler runs (runs/<tag>/ttis.npz written by sched_muapp.py).

    python3 muapps/eval.py eval_maxweight eval_rl eval_pf ...          (tags under runs/)
    python3 muapps/eval.py --glob 'eval-*' --skip-s 5 --csv runs/eval.csv

Per run: throughput per air time (DRB bytes scheduled x 8 / slots x 0.5 ms, total and per UE), Jain fairness over the
per-UE throughputs, mean DL backlog, the share of DL slots whose action was applied in the exact TTI (HIT), and the
report -> action latency at the bridge for the applied actions.
"""

import argparse
import csv
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from edgeric_rt.rte2 import SLOT_S  # noqa: E402

HOME = os.environ.get("EDGERIC_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def summarize(path, skip_s=0.0, ue_order=None):
    d = np.load(path)
    tti = d["ran_tti"].astype(np.int64)
    if len(tti) < 2:
        return None
    # unwrap RANtime and drop the first skip_s seconds of air time
    tti = np.cumsum(np.concatenate([[0], np.diff(tti) & 0xFFFFFFFF])) + tti[0]
    keep = tti >= tti[0] + int(skip_s / SLOT_S)
    if keep.sum() < 2:
        return None
    sl = slice(np.argmax(keep), None)
    tti = tti[sl]
    slots = int(tti[-1] - tti[0] + 1)
    idx = d["ue_du_ue_index"][sl]
    rnti = d["ue_rnti"][sl]
    cnt = d["ue_count"][sl].astype(int)
    drb = d["ue_drb_bytes"][sl].astype(np.float64)
    backlog = d["ue_dl_pending_bytes"][sl].astype(np.float64)
    is_dl = d["is_dl"][sl].astype(bool)
    status = d["act_status"][sl]
    n = int(cnt.max())
    per_ue = {}
    for k in range(n):
        valid = cnt > k
        r = int(rnti[valid, k][-1]) if valid.any() else 0
        per_ue[r] = dict(du=int(idx[valid, k][-1]) if valid.any() else k,
                         mbps=drb[valid, k].sum() * 8 / (slots * SLOT_S) / 1e6,
                         backlog=float(backlog[valid, k].mean()) if valid.any() else 0.0)
    tputs = np.array([v["mbps"] for v in per_ue.values()])
    jain = float(tputs.sum() ** 2 / (len(tputs) * (tputs ** 2).sum())) if tputs.sum() > 0 else float("nan")
    hit = status[is_dl] == 1
    t_src, t_sent = d["act_t_src_report_ns"][sl].astype(np.float64), d["act_t_sent_ns"][sl].astype(np.float64)
    lat = ((t_sent - t_src) / 1e3)[(status == 1) & (t_src > 0)]
    t_rep = d["t_report_ns"][sl].astype(np.float64)
    wall_s = (t_rep[-1] - t_rep[0]) / 1e9 if t_rep[-1] > t_rep[0] else float("nan")
    return dict(path=path, slots=slots, seconds=slots * SLOT_S, total_mbps=float(tputs.sum()), jain=jain,
                wall_s=wall_s, rt_ratio=slots * SLOT_S / wall_s,
                wall_mbps=float(drb.sum() * 8 / wall_s / 1e6) if wall_s > 0 else float("nan"),
                per_ue=per_ue, dl_hit_pct=100.0 * hit.mean() if hit.size else float("nan"),
                lat_p50_us=float(np.percentile(lat, 50)) if lat.size else float("nan"),
                lat_p99_us=float(np.percentile(lat, 99)) if lat.size else float("nan"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tags", nargs="*", help="run tags under runs/ (or paths to ttis.npz)")
    ap.add_argument("--glob", help="glob of run tags under runs/, e.g. 'eval-*'")
    ap.add_argument("--runs-dir", default=os.path.join(HOME, "runs"))
    ap.add_argument("--skip-s", type=float, default=2.0, help="air-time seconds to drop at the start of each run")
    ap.add_argument("--csv", help="also write the table as CSV")
    ap.add_argument("--udp", help="file of '<tag> <Mbit/s>' lines: UDP delivered at the UEs over each run")
    ap.add_argument("--ue-map", default=os.path.join(os.environ.get("EDGERIC_RT_APP_DIR", "/apps/edgeric_rt"),
                                                    "run", "ue_map.txt"))
    args = ap.parse_args(argv)

    tags = list(args.tags)
    if args.glob:
        tags += sorted(os.path.basename(p) for p in glob.glob(os.path.join(args.runs_dir, args.glob)))
    if not tags:
        ap.error("no runs given")

    names = {}
    if os.path.exists(args.ue_map):
        for line in open(args.ue_map):
            p = line.split("#", 1)[0].split()
            if len(p) >= 2:
                names[int(p[1], 0)] = p[0]

    rows = []
    for t in tags:
        path = t if t.endswith(".npz") else os.path.join(args.runs_dir, t, "ttis.npz")
        if not os.path.exists(path):
            print(f"skip {t}: no {path}", file=sys.stderr)
            continue
        s = summarize(path, args.skip_s)
        if s:
            s["tag"] = t
            rows.append(s)
    if not rows:
        sys.exit("no usable runs")

    udp = {}
    if args.udp and os.path.exists(args.udp):
        for line in open(args.udp):
            p = line.split()
            if len(p) >= 2:
                udp[p[0]] = float(p[1])

    ues = sorted({r for s in rows for r in s["per_ue"]}, key=lambda r: names.get(r, f"z{r}"))
    label = {r: names.get(r, hex(r)) for r in ues}
    hdr = (["run", "seconds", "gNB Mbps (air time)", "gNB Mbps (wall clock)", "real-time ratio"]
           + (["UDP delivered Mbps"] if udp else [])
           + [f"{label[r]} Mbps" for r in ues] + ["Jain", "DL HIT %", "lat p50 us", "lat p99 us"])
    print("| " + " | ".join(hdr) + " |")
    print("|" + "---|" * len(hdr))
    table = []
    for s in sorted(rows, key=lambda s: -s["total_mbps"]):
        row = [s["tag"], f"{s['seconds']:.0f}", f"{s['total_mbps']:.2f}", f"{s['wall_mbps']:.2f}",
               f"{s['rt_ratio']:.2f}"]
        if udp:
            row.append(f"{udp[s['tag']]:.2f}" if s["tag"] in udp else "-")
        row += [f"{s['per_ue'][r]['mbps']:.2f}" if r in s["per_ue"] else "-" for r in ues]
        row += [f"{s['jain']:.3f}", f"{s['dl_hit_pct']:.1f}", f"{s['lat_p50_us']:.0f}", f"{s['lat_p99_us']:.0f}"]
        table.append(row)
        print("| " + " | ".join(row) + " |")
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(hdr)
            w.writerows(table)
        print(f"wrote {args.csv}")


if __name__ == "__main__":
    main()
