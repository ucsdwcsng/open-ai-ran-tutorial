#!/usr/bin/env python3
"""Slot-level model of the testbed under finite, unequal per-UE loads: pick an operating point, try PPO settings.

    python3 tools/load_sim.py --inputs runs/loadsim/inputs.json compare --loads 7,7,5,5
    python3 tools/load_sim.py --inputs runs/loadsim/inputs.json train --loads 7,7,5,5 --gamma 0.98 --iters 60

inputs.json: {"cap": {cqi: bytes a one-UE slot carries at that CQI, measured on the testbed},
              "walks": [[CQI per 100 ms row] per UE]} (the cqi column of duranta-oai-ue/traces/lib/cqi_walk_k.csv).

Model: 0.5 ms slots in the 7D/1S/2U pattern; each UE's CQI as the gNB sees it (trace row of the last 20 ms CSI
instant); arrivals as iperf3 sends them, 1,400-byte datagrams at a constant rate; backlog capped at 6.2 MB (the RLC
queue; the excess is dropped); schedulers see the backlog of the previous slot's report; one UE per DL slot, carrying
min(backlog, cap[CQI]). It leaves out HARQ, OLLA and the PDCCH, so absolute numbers are indicative;
the point is the ordering of schedulers and the margins between them.

compare: the schedulers of edgeric_rt/policies.py (maxcqi, maxweight, rr, pf) and two reference rules: maxbytes
(the UE that can carry the most bytes now) and fullfirst (UEs whose backlog fills the slot first, best CQI among them).
train: PPO from edgeric_rt/rl/ppo.py on the model (same state, action and reward as train_ppo.py), then its top-UE
policy on the comparison window.
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from edgeric_rt.rl.ppo import B_NORM, R_SCALE  # noqa: E402

QMAX = 6.2e6
SLOT = 5e-4
PKT = 1400.0


class Model:
    def __init__(self, inputs, loads, start_s=1.0, seed=0):
        d = json.load(open(inputs))
        self.capv = np.array([float(d["cap"].get(str(c), 0)) for c in range(16)])
        self.walks = [np.array(w) for w in d["walks"]][: len(loads)]
        self.n = len(loads)
        self.arr = np.array(loads, dtype=float) * 1e6 / 8 * SLOT
        self.rng = np.random.default_rng(seed)
        self.reset(start_s)

    def reset(self, start_s):
        self.t = int(start_s / SLOT)
        self.q = np.zeros(self.n)
        self.qrep = np.zeros(self.n)  # backlog in the last report, what a scheduler sees
        self.credit = np.zeros(self.n)
        self.last = np.full(self.n, -1.0)
        self.avg = self.cqi().astype(float)
        self.served = np.zeros(self.n)
        self.dropped = np.zeros(self.n)
        self.slots = 0

    def cqi(self):
        t_csi = np.floor(self.t * SLOT / 0.02) * 0.02
        row = int(np.floor(t_csi / 0.1 + 1e-9))
        return np.array([w[row % len(w)] for w in self.walks])

    def advance_to_dl(self):
        """Arrivals slot by slot until the next DL slot; returns that slot's CQI."""
        while True:
            self.qrep = self.q.copy()
            self.credit += self.arr
            pk = np.floor(self.credit / PKT)
            self.credit -= pk * PKT
            self.q += pk * PKT
            over = self.q - QMAX
            if (over > 0).any():
                self.dropped += np.maximum(over, 0)
                np.minimum(self.q, QMAX, out=self.q)
            c = self.cqi()
            self.avg += 0.01 * (c - self.avg)
            self.slots += 1
            if self.t % 10 < 7:
                return c
            self.t += 1

    def serve(self, u, c):
        b = min(self.q[u], self.capv[c[u]]) if u is not None else 0.0
        if u is not None:
            self.q[u] -= b
            self.served[u] += b
            self.last[u] = self.t
        self.t += 1
        return b

    def obs(self, c):
        x = np.zeros(2 * self.n, dtype=np.float32)
        x[0::2] = c / 15.0
        x[1::2] = np.log1p(self.qrep) / B_NORM
        return x


def rule(name, m, c):
    q = m.qrep
    have = q > 0
    if not have.any():
        return None
    cap = m.capv[c]
    if name == "maxcqi":
        w = np.where(have, c, -1.0)
    elif name == "maxweight":
        w = np.where(have, c * q, -1.0)
    elif name == "rr":
        w = np.where(have, 1.0, -1.0)
    elif name == "pf":
        w = np.where(have, c / np.maximum(m.avg, 1e-3), -1.0)
    elif name == "maxbytes":
        w = np.where(have, np.minimum(q, cap), -1.0)
    elif name == "fullfirst":
        full = have & (q >= cap)
        w = np.where(full, 1e9 + cap, -1.0) if full.any() else np.where(have, np.minimum(q, cap), -1.0)
    else:
        raise ValueError(name)
    top = w.max()
    cands = np.flatnonzero(w >= top - 1e-9)
    return int(cands[np.argmin(m.last[cands])])


def evaluate(choose, inputs, loads, seconds=60.0, start_s=3.0, warm_s=2.0):
    m = Model(inputs, loads, start_s)
    end = m.t + int(seconds / SLOT)
    warm = m.t + int(warm_s / SLOT)
    served0 = None
    while m.t < end:
        if served0 is None and m.t >= warm:
            served0 = m.served.copy()
        c = m.advance_to_dl()
        m.serve(choose(m, c), c)
    secs = seconds - warm_s
    return (m.served - served0) * 8 / secs / 1e6


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inputs", required=True)
    ap.add_argument("mode", choices=("compare", "train"))
    ap.add_argument("--loads", required=True, help="Mbit/s per UE, e.g. 7,7,5,5")
    ap.add_argument("--gamma", type=float, default=0.9)
    ap.add_argument("--lam", type=float, default=0.95)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--iters", type=int, default=60)
    ap.add_argument("--steps", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    loads = [float(x) for x in args.loads.split(",")]
    rules = ["maxcqi", "maxweight", "rr", "pf", "maxbytes", "fullfirst"]
    res = {r: evaluate(lambda m, c, r=r: rule(r, m, c), args.inputs, loads) for r in rules}
    for r, v in res.items():
        print(f"{r:10s} total {v.sum():6.2f} Mbit/s  per UE {np.round(v, 2)}", flush=True)
    if args.mode == "compare":
        return

    from edgeric_rt.rl.ppo import PPO

    import torch

    ppo = PPO(len(loads), lr=args.lr, gamma=args.gamma, lam=args.lam, kind="cat", arch="set", seed=args.seed)
    rng = np.random.default_rng(args.seed)
    env = Model(args.inputs, loads, start_s=rng.uniform(0, 300))
    mask = np.ones(len(loads), dtype=bool)
    t0 = time.time()
    for it in range(args.iters):
        pol = ppo.export()
        O, A, LP, R = [], [], [], []
        for _ in range(args.steps):
            c = env.advance_to_dl()
            x = env.obs(c)
            a, lp, _ = pol.sample_cat(x, mask, with_value=False)
            O.append(x), A.append(a), LP.append(lp)
            R.append(env.serve(a, c) / R_SCALE)
        obs = np.stack(O)
        with torch.no_grad():
            val = ppo.values(torch.as_tensor(obs)).numpy().astype(np.float64)
        st = ppo.update(obs, np.array(A), np.array(LP), val, np.array(R), last_value=val[-1],
                        mask=np.ones((len(O), len(loads)), dtype=bool))
        if it % 10 == 9 or it == args.iters - 1:
            pol = ppo.export()
            v = evaluate(lambda m, c: pol.argmax(m.obs(c)), args.inputs, loads)
            print(f"it {it + 1:3d}  PPO (top UE) total {v.sum():6.2f}  per UE {np.round(v, 2)}  entropy {st['entropy']:.2f}"
                  f"  ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
