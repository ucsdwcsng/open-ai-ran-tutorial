#!/usr/bin/env python3
"""Train the EdgeRIC-RT PPO scheduler online, against the live gNB (default: 4 UEs, full-buffer DL traffic).

    python3 muapps/train_ppo.py --ues 4 --iters 100 --steps 5000 --tag ppo_4ue

Each iteration:
  1. collect `--steps` samples with the stochastic policy. A sample is an action that the gNB applied in exactly the
     TTI it targeted (the report of that TTI echoes its seq with status HIT); the reward is the DRB bytes scheduled
     in that TTI, read from the same report;
  2. stop acting and update (the gNB runs its own scheduler meanwhile: a deliberate Lazy RIC);
  3. every --eval-every iterations, run the mean policy for --eval-steps applied TTIs and measure throughput.

--action single (default): the policy picks the one UE that gets the whole slot (categorical over the UEs).
--action shared: the policy outputs RB shares for every UE in the slot (Gaussian logits -> softmax).
--arch set (default): one network shared by all UE positions; --arch mlp: the original whole-state network.

Convergence is tracked with two channel-independent measures besides throughput, since every batch sees a different
stretch of the channel: best_pct, the share of applied actions that gave the slot to a UE with the highest CQI, and
eff, the spectral efficiency of the chosen UE's CQI over that of the best CQI (1.0 = always the best).

Output in runs/<tag>/: train.csv (one row per iteration), last.npz and best.npz (NumpyPolicy, for
sched_muapp.py --scheduler rl --model ...), ckpt.pt (torch state, for --resume).
"""

import argparse
import csv
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from edgeric_rt import rte2  # noqa: E402
from edgeric_rt.policies import CQI_EFF  # noqa: E402
from edgeric_rt.rl.ppo import PPO, R_SCALE, NumpyPolicy, Observer, softmax  # noqa: E402
from edgeric_rt.ue_map import UeMap  # noqa: E402

W = rte2.W
HOME = os.environ.get("EDGERIC_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ues", type=int, default=4)
    ap.add_argument("--action", choices=("single", "shared"), default="single",
                    help="single: one UE per slot (categorical); shared: RB shares per UE (Gaussian + softmax)")
    ap.add_argument("--arch", choices=("set", "mlp"), default="set",
                    help="set: one network shared by all UE positions; mlp: one network over the whole state")
    ap.add_argument("--iters", type=int, default=100)
    ap.add_argument("--steps", type=int, default=5000, help="applied-action samples per iteration")
    ap.add_argument("--eval-every", type=int, default=5)
    ap.add_argument("--eval-steps", type=int, default=3000)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--gamma", type=float, default=0.9)
    ap.add_argument("--lam", type=float, default=0.95)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--minibatch", type=int, default=256)
    ap.add_argument("--ent-coef", type=float, default=1e-3)
    ap.add_argument("--init-log-std", type=float, default=-0.5)
    ap.add_argument("--min-share", type=float, default=0.02)
    ap.add_argument("--lead", type=int, default=1)
    ap.add_argument("--work-conserving", action="store_true")
    ap.add_argument("--ue-map", default=os.path.join(rte2.APP_DIR, "run", "ue_map.txt"))
    ap.add_argument("--tag", default=None)
    ap.add_argument("--runs-dir", default=os.path.join(HOME, "runs"))
    ap.add_argument("--resume", action="store_true", help="continue from runs/<tag>/ckpt.pt")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cpus", default="", help="pin the trainer, e.g. 29")
    ap.add_argument("--fifo", type=int, default=0)
    return ap.parse_args(argv)


class Loop:
    """Acts on the newest report every TTI and turns applied actions into samples."""

    def __init__(self, args):
        self.args = args
        self.rt = rte2.RtE2(lead=args.lead)
        self.ue_map = UeMap(args.ue_map)
        self.obs = Observer(args.ues, self.ue_map)
        self.flags = W.FLAG_WORK_CONSERVING if args.work_conserving else 0

    def resync(self):
        """Drop the reports queued while we were not acting (the update pause). One non-blocking drain: new reports
        arrive every slot, so waiting for the socket to go quiet would never end."""
        self.rt.drain()

    def run(self, pol, n_samples, stochastic, timeout_s=120.0):
        """Collect n_samples applied actions. Returns (samples, stats)."""
        ring = rte2.SampleRing()
        samples = []
        bytes_total = 0
        first_tti = last_tti = None
        dl_slots = dl_hit = 0
        t0 = time.monotonic()
        self.resync()
        while len(samples) < n_samples:
            if time.monotonic() - t0 > timeout_s:
                print(f"[train] timeout: {len(samples)}/{n_samples} samples in {timeout_s:.0f}s", flush=True)
                break
            reps = self.rt.poll(timeout=1.0)
            if not reps:
                continue
            for r in reps:
                tti = int(r["ran_tti"])
                first_tti = tti if first_tti is None else first_tti
                last_tti = tti
                ues = rte2.ues_of(r)
                b = int(ues["drb_bytes"].sum()) if len(ues) else 0
                bytes_total += b
                if r["is_dl"]:
                    dl_slots += 1
                    dl_hit += int(r["act_status"]) == W.STATUS_HIT
                res = ring.resolve(r)
                if res is not None:
                    (x, act, logp, mask), rep = res
                    samples.append((x, act, logp, b / R_SCALE, mask))
            newest = reps[-1]
            self.ue_map.reload()
            x, idx = self.obs.encode(newest)
            mask = np.array([i is not None for i in idx])
            if not mask.any():
                continue
            if pol.kind == "cat":
                # one UE for the whole slot
                if stochastic:
                    act, logp, _ = pol.sample_cat(x, mask, with_value=False)  # critic: in batch, after
                else:
                    act, logp = pol.argmax(x, mask), 0.0
                iw = {idx[act]: 1}
            else:
                if stochastic:
                    act, logp, _ = pol.sample(x, mask, with_value=False)
                else:
                    act, logp = pol.mean(x, mask), 0.0
                w = softmax(act)
                iw = rte2.to_int_weights({i: float(w[k]) for k, i in enumerate(idx) if i is not None},
                                         self.args.min_share)
            seq, target = self.rt.send(newest, iw, flags=self.flags)
            if seq is not None:
                ring.add(seq, target, (x, act, logp, mask))
        slots = ((last_tti - first_tti) & 0xFFFFFFFF) + 1 if first_tti is not None else 0
        mbps = bytes_total * 8 / (slots * rte2.SLOT_S) / 1e6 if slots else 0.0
        best, eff = pick_quality_batch(samples, pol.kind)
        stats = dict(mbps=mbps, dl_hit_pct=100.0 * dl_hit / dl_slots if dl_slots else 0.0, slots=slots,
                     wall_s=time.monotonic() - t0, samples=len(samples), best_pct=100.0 * best, eff=eff)
        return samples, stats


def pick_quality_batch(samples, kind):
    """Mean pick_quality over (x, act, logp, reward, mask) samples, computed after collection."""
    q = [pick_quality(s[0], s[1], s[4], kind) for s in samples]
    q = np.array([v for v in q if v is not None])
    return (float(q[:, 0].mean()), float(q[:, 1].mean())) if len(q) else (float("nan"), float("nan"))


def pick_quality(x, act, mask, kind):
    """(share of the slot given to a UE with the highest CQI, CQI efficiency of the choice / that of the best CQI).
    For 'gauss' the share and efficiency are those of the RB split softmax(z)."""
    c = np.clip(np.rint(np.asarray(x[0::2]) * 15).astype(int), 0, 15)
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return None
    cmax = int(c[m].max())
    if CQI_EFF[cmax] <= 0:
        return None
    if kind == "cat":
        return float(c[act] == cmax), float(CQI_EFF[c[act]] / CQI_EFF[cmax])
    w = softmax(np.where(m, act, -1e9))
    return float(w[(c == cmax) & m].sum()), float((w * CQI_EFF[c]).sum() / CQI_EFF[cmax])


def main(argv=None):
    args = build_args(argv)
    for m in rte2.Pacer.apply(rte2.parse_cpus(args.cpus), args.fifo):
        print(f"[train] {m}", flush=True)
    tag = args.tag or f"ppo_{args.ues}ue-{time.strftime('%Y%m%d-%H%M%S')}"
    out = os.path.join(args.runs_dir, tag)
    os.makedirs(out, exist_ok=True)
    try:
        os.chmod(out, 0o777)  # runs/ is a host directory: let the host user add plots next to the run
    except OSError:
        pass

    import torch

    ppo = PPO(args.ues, lr=args.lr, gamma=args.gamma, lam=args.lam, epochs=args.epochs, minibatch=args.minibatch,
              ent_coef=args.ent_coef, init_log_std=args.init_log_std, seed=args.seed,
              kind="cat" if args.action == "single" else "gauss", arch=args.arch)
    start_it = 0
    best = -1.0
    ckpt_path = os.path.join(out, "ckpt.pt")
    if args.resume and os.path.exists(ckpt_path):
        ck = torch.load(ckpt_path, weights_only=False)
        ppo.load_state_dict(ck["ppo"])
        start_it, best = ck["it"] + 1, ck["best"]
        print(f"[train] resumed {tag} at iteration {start_it} (best eval {best:.2f} Mbps)", flush=True)

    loop = Loop(args)
    csv_path = os.path.join(out, "train.csv")
    new_csv = not os.path.exists(csv_path) or not args.resume
    fh = open(csv_path, "w" if new_csv else "a", newline="")
    wr = csv.writer(fh)
    if new_csv:
        wr.writerow(["iter", "train_mbps", "train_dl_hit_pct", "mean_reward", "eval_mbps", "eval_dl_hit_pct",
                     "pi_loss", "v_loss", "entropy", "approx_kl", "clip_frac", "std", "collect_s", "update_s",
                     "train_best_pct", "train_eff", "eval_best_pct", "eval_eff"])
    print(f"[train] {tag}: {args.ues} UEs, action {args.action}, arch {args.arch}, {args.iters} iterations x {args.steps} applied TTIs, lead {args.lead}, "
          f"{'work-conserving' if args.work_conserving else 'strict'}", flush=True)

    meta = dict(tag=tag, ues=args.ues, args=vars(args))
    try:
        for it in range(start_it, args.iters):
            pol = ppo.export()
            samples, st = loop.run(pol, args.steps, stochastic=True)
            if len(samples) < args.minibatch:
                print(f"[train] it {it}: only {len(samples)} samples (is traffic running?)", flush=True)
                continue
            obs = np.stack([s[0] for s in samples])
            z = np.array([s[1] for s in samples]) if ppo.kind == "cat" else np.stack([s[1] for s in samples])
            logp = np.array([s[2] for s in samples])
            rew = np.array([s[3] for s in samples])
            mask = np.stack([s[4] for s in samples])
            with torch.no_grad():  # the critic of the policy that collected the batch, in one pass
                val = ppo.values(torch.as_tensor(obs), torch.as_tensor(mask)).numpy().astype(np.float64)
            t_up = time.monotonic()
            ls = ppo.update(obs, z, logp, val, rew, last_value=val[-1], mask=mask)
            up_s = time.monotonic() - t_up

            ev = dict(mbps=float("nan"), dl_hit_pct=float("nan"), best_pct=float("nan"), eff=float("nan"))
            pol = ppo.export()
            if it % args.eval_every == 0 or it == args.iters - 1:
                _, ev = loop.run(pol, args.eval_steps, stochastic=False)
                if ev["mbps"] > best:
                    best = ev["mbps"]
                    pol.save(os.path.join(out, "best.npz"), meta={**meta, "iter": it, "eval_mbps": best})
            pol.save(os.path.join(out, "last.npz"), meta={**meta, "iter": it})
            torch.save(dict(ppo=ppo.state_dict(), it=it, best=best), ckpt_path)
            # spread of the policy: action std (shared) or the mean probability of its top UE (single)
            std = (float(np.exp(pol.p["log_std"]).mean()) if pol.kind == "gauss"
                   else float(np.mean([pol.probs(o).max() for o in obs[:: max(1, len(obs) // 200)]])))
            wr.writerow([it, f"{st['mbps']:.3f}", f"{st['dl_hit_pct']:.2f}", f"{rew.mean():.4f}",
                         f"{ev['mbps']:.3f}", f"{ev['dl_hit_pct']:.2f}", f"{ls['pi_loss']:.4f}",
                         f"{ls['v_loss']:.4f}", f"{ls['entropy']:.3f}", f"{ls['approx_kl']:.4f}",
                         f"{ls['clip_frac']:.3f}", f"{std:.3f}", f"{st['wall_s']:.1f}", f"{up_s:.1f}",
                         f"{st['best_pct']:.2f}", f"{st['eff']:.4f}", f"{ev['best_pct']:.2f}", f"{ev['eff']:.4f}"])
            fh.flush()
            print(f"[train] it {it:3d} | train {st['mbps']:5.1f} Mbps (HIT {st['dl_hit_pct']:5.1f}%) | "
                  f"best {st['best_pct']:5.1f}% eff {st['eff']:.3f} | "
                  f"eval {ev['mbps']:5.1f} Mbps best {ev['best_pct']:5.1f}% | reward {rew.mean():.3f} | {'std' if pol.kind == 'gauss' else 'p_top'} {std:.2f} | kl {ls['approx_kl']:.4f} | "
                  f"collect {st['wall_s']:.1f}s update {up_s:.1f}s", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
        loop.rt.close()
        print(f"[train] done: best eval {best:.2f} Mbps -> {os.path.join(out, 'best.npz')}", flush=True)


if __name__ == "__main__":
    main()
