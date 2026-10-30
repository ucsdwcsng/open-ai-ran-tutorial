#!/usr/bin/env python3
"""Measure the DL SNR -> reported CQI map of this cell and write duranta-oai-ue/traces/cqi_calib.csv.

  1. host:      bash scripts/trace_ctl.sh apply duranta-oai-ue/ue_traces_cal.conf
                (ue1 sweeps DL SNR 0 -> 24 dB in 1 dB steps of 2 s, once; keep the other UEs idle or static)
  2. container: python3 muapps/sched_muapp.py --scheduler stock --duration 70 --tag cal
                (stock scheduler, just records every report; start it right after step 1)
  3. container: python3 tools/calibrate_cqi.py runs/cal/ttis.npz --rnti 0x4601 [--write]

The staircase timing is known (dwell_s per step) but its start is not, in RANtime. The gNB's slot clock and the
UE's air time both follow the ZMQ sample stream, so the steps are dwell_s / 0.5 ms slots apart: the tool finds the
start by fitting the step grid to the CQI series (least within-step spread), then takes the median CQI per step.
Each CQI's SNR is the middle of the SNRs that report it; CQIs the sweep never reports keep their old value.
"""

import argparse
import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from edgeric_rt.rte2 import SLOT_S  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("npz", help="ttis.npz of a run recorded during the staircase")
    ap.add_argument("--rnti", type=lambda s: int(s, 0), required=True, help="C-RNTI of the sweeping UE (ue_map.txt)")
    ap.add_argument("--snr-lo", type=float, default=0.0)
    ap.add_argument("--snr-hi", type=float, default=24.0)
    ap.add_argument("--snr-step", type=float, default=1.0)
    ap.add_argument("--dwell", type=float, default=2.0)
    ap.add_argument("--calib", default=os.environ.get("EDGERIC_CQI_CALIB", ""),
                    help="cqi_calib.csv to update (with --write); default: print only")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)

    d = np.load(a.npz)
    tti = d["ran_tti"].astype(np.int64)
    tti = np.cumsum(np.concatenate([[0], np.diff(tti) & 0xFFFFFFFF]))
    rn = d["ue_rnti"]
    col = np.argmax((rn == a.rnti).any(axis=0))
    if not (rn[:, col] == a.rnti).any():
        sys.exit(f"rnti {a.rnti:#x} not in {a.npz}")
    mask = rn[:, col] == a.rnti
    t = tti[mask] * SLOT_S
    cqi = d["ue_cqi"][mask, col].astype(float)
    eff = d["ue_eff_cqi_x100"][mask, col].astype(float) / 100
    mcs = d["ue_dl_mcs"][mask, col].astype(float)

    snrs = np.arange(a.snr_lo, a.snr_hi + 1e-9, a.snr_step)
    nsteps = len(snrs)
    span = nsteps * a.dwell
    if t[-1] - t[0] < span:
        sys.exit(f"record covers {t[-1] - t[0]:.1f} s, the staircase needs {span:.1f} s")

    # Fit the start: within-step spread of the CQI, over candidate starts at 10 ms resolution.
    best = None
    for t0 in np.arange(t[0], t[-1] - span, 0.01):
        k = np.floor((t - t0) / a.dwell).astype(int)
        ok = (k >= 0) & (k < nsteps)
        if ok.sum() < nsteps * 10:
            continue
        spread = sum(np.var(cqi[ok][k[ok] == s]) for s in range(nsteps) if (k[ok] == s).sum() > 5)
        if best is None or spread < best[0]:
            best = (spread, t0)
    if best is None:
        sys.exit("could not fit the staircase")
    t0 = best[1]
    k = np.floor((t - t0) / a.dwell).astype(int)

    print(f"staircase start at RANtime {t0 / SLOT_S:.0f} (fit spread {best[0]:.2f})")
    print("snr_db  cqi(median)  eff_cqi  dl_mcs")
    step_cqi = {}
    for s, snr in enumerate(snrs):
        # skip the first 20% of each step: CQI follows the SNR at the next CSI report
        sel = (k == s) & ((t - t0) - s * a.dwell > 0.2 * a.dwell)
        if sel.sum() == 0:
            continue
        c = float(np.median(cqi[sel]))
        step_cqi[snr] = c
        m = mcs[sel][mcs[sel] > 0]
        print(f"{snr:6.1f}  {c:11.1f}  {np.median(eff[sel]):7.2f}  {np.median(m) if m.size else float('nan'):6.1f}")

    calib = {}
    for c in range(1, 16):
        s = [snr for snr, v in step_cqi.items() if round(v) == c]
        if s:
            calib[c] = (min(s) + max(s)) / 2
    print("cqi -> snr center:", {c: round(v, 2) for c, v in sorted(calib.items())})

    if a.write:
        if not a.calib or not os.path.exists(a.calib):
            sys.exit("--write needs --calib PATH to an existing cqi_calib.csv")
        shutil.copy(a.calib, a.calib + ".bak")
        lines = open(a.calib).read().splitlines()
        out = []
        for line in lines:
            parts = line.split(",")
            if len(parts) >= 2 and parts[0].strip().isdigit() and int(parts[0]) in calib:
                out.append(f"{int(parts[0])},{calib[int(parts[0])]:.1f}")
            else:
                out.append(line)
        with open(a.calib, "w") as f:
            f.write("\n".join(out) + "\n")
        print(f"updated {a.calib} (previous in {a.calib}.bak); regenerate the traces: gen_traces.py --all")


if __name__ == "__main__":
    main()
