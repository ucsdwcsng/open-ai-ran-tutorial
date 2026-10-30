#!/usr/bin/env python3
"""Training curve and convergence check of a PPO run (runs/<tag>/train.csv written by train_ppo.py).

    python3 tools/plot_training.py runs/ppo4_walk [--ref bestcqi=41.2 --ref rr=24.0]

Writes runs/<tag>/train_curve.png and .svg (needs matplotlib, e.g. on the host) and runs/<tag>/convergence.json,
and prints the convergence summary. Panels:
  1. throughput per air-second: every iteration (thin), its 10-iteration mean, deterministic evaluations (dots),
     and optional reference lines (e.g. the best-CQI scheduler measured on the same channel);
  2. how often the slot went to a UE with the highest CQI (training samples are stochastic, evaluations take the
     policy's top UE), and the CQI efficiency of the choice relative to the best CQI;
  3. policy entropy (nats) and the mean probability of its top UE.

Convergence: the iteration at which the 10-iteration mean of the CQI efficiency of the chosen UE (relative to the best
UE in the slot) first reaches 0.97, and how well the run held that level afterwards (convergence.json).
"""

import argparse
import csv
import json
import math
import os
import sys


def col(rows, name):
    out = []
    for r in rows:
        try:
            out.append(float(r.get(name, "nan")))
        except ValueError:
            out.append(float("nan"))
    return out


def moving(v, k=10):
    out = []
    for i in range(len(v)):
        w = [x for x in v[max(0, i - k + 1): i + 1] if not math.isnan(x)]
        out.append(sum(w) / len(w) if w else float("nan"))
    return out


def convergence(rows, eff_min=0.97, tail=20):
    """When the policy reached its plateau and how well it held it.

    reached_at_iter: the first iteration (with 10 behind it) at which the 10-iteration mean of train_eff, the CQI
    efficiency of the chosen UE over the best UE's, is >= eff_min (within 3% of always picking the best UE).
    After it: the lowest 10-iteration means of train_eff and train_best_pct, and the share of single iterations at
    or above eff_min. held_from_iter: the first iteration after which the 10-iteration mean never drops below eff_min.
    """
    it = [int(r["iter"]) for r in rows]
    best, eff = col(rows, "train_best_pct"), col(rows, "train_eff")
    if not eff or all(math.isnan(x) for x in eff):
        return None
    ma_eff, ma_best = moving(eff), moving(best)
    k = next((i for i in range(min(9, len(rows) - 1), len(rows)) if ma_eff[i] >= eff_min), None)
    held = next((i for i in range(len(rows)) if all(m >= eff_min for m in ma_eff[i:] if not math.isnan(m))), None)
    out = dict(iterations=len(rows), eff_min=eff_min, reached_at_iter=it[k] if k is not None else None,
               held_from_iter=it[held] if held is not None else None)
    if k is not None:
        after_e, after_b = eff[k:], best[k:]
        out.update(ma10_eff_min_after=round(min(ma_eff[k:]), 4), ma10_best_min_after=round(min(ma_best[k:]), 2),
                   share_iters_eff_ok_after=round(sum(x >= eff_min for x in after_e) / len(after_e), 3),
                   mean_eff_after=round(sum(after_e) / len(after_e), 4),
                   mean_best_pct_after=round(sum(after_b) / len(after_b), 2))
    ev = [b for b in col(rows, "eval_best_pct") if not math.isnan(b)]
    out.update(final_train_best_pct=round(sum(best[-tail:]) / len(best[-tail:]), 2),
               final_train_eff=round(sum(eff[-tail:]) / len(eff[-tail:]), 4),
               eval_best_pct_min=min(ev) if ev else None, final_entropy=col(rows, "entropy")[-1])
    return out


def load_convergence(rows, frac=0.97, tail=20):
    """Finite loads: the best UE is not the best-CQI one, so judge on throughput. reached_at_iter: first iteration
    (with 10 behind it) whose 10-iteration mean of train_mbps is >= frac x the mean of the last `tail` iterations."""
    it = [int(r["iter"]) for r in rows]
    mb = col(rows, "train_mbps")
    ma = moving(mb)
    final = sum(mb[-tail:]) / len(mb[-tail:])
    k = next((i for i in range(min(9, len(rows) - 1), len(rows)) if ma[i] >= frac * final), None)
    out = dict(objective="load", iterations=len(rows), final_train_mbps=round(final, 2), frac=frac,
               reached_at_iter=it[k] if k is not None else None,
               first10_mean_mbps=round(sum(mb[:10]) / min(10, len(mb)), 2))
    if k is not None:
        out["ma10_min_after"] = round(min(ma[k:]), 2)
        out["ma10_max_after"] = round(max(ma[k:]), 2)
    ev = [v for v in col(rows, "eval_mbps") if not math.isnan(v)]
    out["eval_mbps_last5_mean"] = round(sum(ev[-5:]) / len(ev[-5:]), 2) if ev else None
    out["final_entropy"] = col(rows, "entropy")[-1]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="runs/<tag> (or its train.csv)")
    ap.add_argument("--ref", action="append", default=[], help="reference line name=Mbps, e.g. bestcqi=41.2")
    ap.add_argument("--title", default=None)
    ap.add_argument("--out-dir", default=None, help="where to write the plot and convergence.json (default: the run)")
    ap.add_argument("--objective", choices=("channel", "load"), default="channel",
                    help="channel: full buffer, judged on best-CQI picks; load: finite loads, judged on throughput")
    args = ap.parse_args(argv)

    path = args.run if args.run.endswith(".csv") else os.path.join(args.run, "train.csv")
    out_dir = args.out_dir or os.path.dirname(path)
    os.makedirs(out_dir, exist_ok=True)
    rows = list(csv.DictReader(open(path)))
    if not rows:
        sys.exit(f"no rows in {path}")
    conv = convergence(rows) if args.objective == "channel" else load_convergence(rows)
    if conv:
        with open(os.path.join(out_dir, "convergence.json"), "w") as f:
            json.dump(conv, f, indent=1)
        print(json.dumps(conv))

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available: wrote convergence.json only (plot on the host)", file=sys.stderr)
        return

    it = [int(r["iter"]) for r in rows]
    tr, ev = col(rows, "train_mbps"), col(rows, "eval_mbps")
    fig, ax = plt.subplots(3, 1, figsize=(9, 9), sharex=True, gridspec_kw=dict(height_ratios=[1.3, 1, 0.8]))
    ax[0].plot(it, tr, color="#9aa5b1", lw=0.9, label="training, per iteration")
    ax[0].plot(it, moving(tr), color="#1c5da6", lw=2.2, label="10-iteration mean")
    e = [(i, v) for i, v in zip(it, ev) if not math.isnan(v)]
    if e:
        ax[0].plot([i for i, _ in e], [v for _, v in e], "o", color="#b9470f", ms=4.5, label="evaluation (top UE)")
    for ref in args.ref:
        name, val = ref.split("=")
        ax[0].axhline(float(val), color="#1c7853", ls="--", lw=1.2)
        ax[0].text(it[-1], float(val), f" {name} {float(val):.1f}", color="#1c7853", va="center", fontsize=9)
    ax[0].set_ylabel("Mbit/s per air-second")
    ax[0].set_ylim(bottom=0)
    ax[0].legend(loc="lower right", fontsize=8, frameon=False)

    tb, eb = col(rows, "train_best_pct"), col(rows, "eval_best_pct")
    if args.objective == "load":
        ax[1].plot(it, moving(tr), color="#1c5da6", lw=2.2, label="throughput, 10-iteration mean")
        if conv and conv.get("reached_at_iter") is not None:
            ax[1].axvline(conv["reached_at_iter"], color="#596775", ls=":", lw=1)
            ax[1].axhline(conv["final_train_mbps"], color="#596775", ls="--", lw=1)
            ax[1].text(conv["reached_at_iter"], 2, f" 97% of the final level at iteration {conv['reached_at_iter']}",
                       fontsize=8, color="#596775")
        ax[1].set_ylim(bottom=0)
        ax[1].set_ylabel("Mbit/s, 10-iteration mean")
        ax[1].legend(loc="lower right", fontsize=8, frameon=False)
    elif not all(math.isnan(x) for x in tb):
        ax[1].plot(it, tb, color="#9aa5b1", lw=0.9, label="training (sampled)")
        ax[1].plot(it, moving(tb), color="#1c5da6", lw=2.2, label="10-iteration mean")
        e = [(i, v) for i, v in zip(it, eb) if not math.isnan(v)]
        if e:
            ax[1].plot([i for i, _ in e], [v for _, v in e], "o", color="#b9470f", ms=4.5, label="evaluation")
        eff = col(rows, "train_eff")
        a2 = ax[1].twinx()
        a2.plot(it, [100 * x for x in eff], color="#1c7853", lw=1.2, alpha=0.8)
        a2.set_ylabel("CQI efficiency of the choice, % of best", color="#1c7853", fontsize=9)
        a2.set_ylim(0, 105)
        if conv and conv.get("reached_at_iter") is not None:
            ax[1].axvline(conv["reached_at_iter"], color="#596775", ls=":", lw=1)
            ax[1].text(conv["reached_at_iter"], 5, f" plateau from iteration {conv['reached_at_iter']}", fontsize=8,
                       color="#596775")
    if args.objective == "channel":
        ax[1].set_ylabel("slot to a best-CQI UE, %")
        ax[1].set_ylim(0, 105)
        ax[1].legend(loc="lower right", fontsize=8, frameon=False)

    ax[2].plot(it, col(rows, "entropy"), color="#1c5da6", lw=1.6, label="entropy (nats)")
    ax[2].plot(it, col(rows, "std"), color="#b9470f", lw=1.2, label="p(top UE)")
    ax[2].set_xlabel("iteration (each: 5,000 applied actions, then one PPO update)")
    ax[2].legend(loc="upper right", fontsize=8, frameon=True, framealpha=0.85, ncol=2)
    for a in ax:
        a.grid(alpha=0.25)
    fig.suptitle(args.title or f"PPO training: {os.path.basename(os.path.dirname(os.path.abspath(path)))}", fontsize=11)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(os.path.join(out_dir, f"train_curve.{ext}"), dpi=140)
    print(f"wrote {os.path.join(out_dir, 'train_curve.png')} and .svg")


if __name__ == "__main__":
    main()
