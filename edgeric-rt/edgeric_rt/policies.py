"""EdgeRIC-RT schedulers: each turns a per-TTI report into an action for the next DL slot.

Two ways to turn a policy's per-UE weights into the slot's allocation (sched_muapp.py --alloc):

  single (default)  one UE gets the whole slot (all R_total RBs), as the stock scheduler does with full buffers.
                    "metric" policies give it to the UE with the highest metric (ties: the UE served least
                    recently); "share" policies hand out whole slots in proportion to their weights (smooth
                    weighted round robin), so the long-run slot shares equal the weights.
  shared            every weighted UE gets R_i = w_i / sum(w) * R_total RBs in the same slot (largest-remainder
                    rounding; strict mode leaves a share a UE cannot use idle). With several UEs per slot every
                    OAI UE decodes a PDSCH every slot, which slows the ZMQ emulation below real time.

  stock      no action: the slot runs the gNB's own scheduler (time_qos PF)
  fixed      share: static weights, e.g. ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4
  rr         metric: every UE with data equal, so the least recently served goes next (round robin)
  pf         metric: CQI / EWMA(CQI) (the paper's PF); --pf-mode rate: CQI spectral efficiency / EWMA(served rate)
  maxcqi     metric: CQI (in shared mode: RBs proportional to CQI, the paper's "CQI-fair")
  bestcqi    metric: CQI, always one UE per slot (the full-buffer throughput optimum)
  maxweight  metric: CQI x backlog
  rl         PPO policy: a 'cat' model gives the slot to its top UE (argmax); a 'gauss' model's probabilities are slot
             shares (share). --rl-pick overrides either way.
"""

import numpy as np

# Spectral efficiency (bits/symbol) of CQI 0..15, TS 38.214 Table 5.2.2.1-2 (64QAM table): a rate proxy for PF.
CQI_EFF = np.array([0.0, 0.1523, 0.2344, 0.3770, 0.6016, 0.8770, 1.1758, 1.4766, 1.9141, 2.4063, 2.7305,
                    3.3223, 3.9023, 4.5234, 5.1152, 5.5547])


def _ues(rep):
    n = min(int(rep["ue_count"]), 16)
    return rep["ue"][:n]


def _with_data(rep):
    return [u for u in _ues(rep) if u["dl_pending_bytes"] > 0]


class Policy:
    name = "base"
    # How --alloc single reduces the weights to one UE: "metric" (highest weight) or "share" (slot shares).
    single_mode = "metric"
    # One UE per slot whatever --alloc says.
    always_single = False

    def weights(self, rep):
        """{du_ue_index: weight >= 0}, or None to send no action (stock scheduler)."""
        raise NotImplementedError

    def observe(self, rep):
        """Every report, acted on or not (for policies with state, e.g. PF averages)."""

    def describe(self):
        return self.name


class Stock(Policy):
    name = "stock"

    def weights(self, rep):
        return None


class Fixed(Policy):
    """Static weights by UE name (ue1..ueN, resolved through the UE map) or by position."""

    name = "fixed"
    single_mode = "share"

    def __init__(self, spec, ue_map):
        self.spec = spec
        self.ue_map = ue_map
        self.by_name, self.by_pos = parse_weights(spec)

    def weights(self, rep):
        ues = _ues(rep)
        out = {}
        if self.by_name:
            for name, w in self.by_name.items():
                idx = self.ue_map.du_index(name, ues)
                if idx is not None:
                    out[idx] = w
        else:
            order = self.ue_map.ordered(ues)
            for pos, idx in enumerate(order):
                if pos < len(self.by_pos):
                    out[idx] = self.by_pos[pos]
        return out

    def describe(self):
        return f"fixed {self.spec}"


class RoundRobin(Policy):
    """One UE per DL slot, in turn: every UE with data has the same metric, and the selector breaks the tie in favour
    of the UE served least recently."""

    name = "rr"
    always_single = True

    def weights(self, rep):
        return {int(u["du_ue_index"]): 1.0 for u in (_with_data(rep) or list(_ues(rep)))}


class PF(Policy):
    """Proportional fair. mode 'cqi': CQI / EWMA(CQI) (EdgeRIC paper). mode 'rate': spectral efficiency of the CQI /
    EWMA of the rate actually served (classic PF), with alpha per TTI."""

    name = "pf"

    def __init__(self, mode="cqi", alpha=0.01):
        self.mode = mode
        self.alpha = alpha
        self.avg = {}

    def observe(self, rep):
        a = self.alpha
        for u in _ues(rep):
            i = int(u["du_ue_index"])
            x = float(u["cqi"]) if self.mode == "cqi" else float(u["drb_bytes"])
            prev = self.avg.get(i)
            self.avg[i] = x if prev is None else (1 - a) * prev + a * x

    def weights(self, rep):
        out = {}
        for u in _with_data(rep):
            i = int(u["du_ue_index"])
            cqi = int(u["cqi"])
            if self.mode == "cqi":
                out[i] = cqi / max(self.avg.get(i, cqi), 1e-3)
            else:
                out[i] = CQI_EFF[min(cqi, 15)] / max(self.avg.get(i, 0.0), 1.0)
        return out

    def describe(self):
        return f"pf ({self.mode})"


class MaxCQI(Policy):
    name = "maxcqi"

    def weights(self, rep):
        return {int(u["du_ue_index"]): float(u["cqi"]) for u in _with_data(rep)}


class BestCQI(Policy):
    """All RBs to the best-CQI UE with data; ties go to the UE served least recently."""

    name = "bestcqi"
    always_single = True

    def weights(self, rep):
        return {int(u["du_ue_index"]): float(u["cqi"]) for u in _with_data(rep)}


class MaxWeight(Policy):
    name = "maxweight"

    def weights(self, rep):
        return {int(u["du_ue_index"]): float(u["cqi"]) * float(u["dl_pending_bytes"]) for u in _with_data(rep)}


class RL(Policy):
    """Trained PPO policy (numpy inference). Its output is a probability per UE: slot shares in single mode (or its
    top UE with pick='argmax'), RB shares in shared mode."""

    name = "rl"

    def __init__(self, model_path, ue_map, stochastic=False, pick=None):
        from .rl.ppo import NumpyPolicy, Observer

        self.pol = NumpyPolicy.load(model_path)
        self.obs = Observer(self.pol.n_ues, ue_map)
        self.stochastic = stochastic
        self.model_path = model_path
        pick = pick or ("argmax" if self.pol.kind == "cat" else "share")
        self.single_mode = "metric" if pick == "argmax" else "share"

    def weights(self, rep):
        x, idx = self.obs.encode(rep)
        p = self.pol.probs(x, mask=[i is not None for i in idx], stochastic=self.stochastic)
        return {i: float(p[k]) for k, i in enumerate(idx) if i is not None}

    def describe(self):
        pick = "argmax" if self.single_mode == "metric" else "share"
        return (f"rl {self.model_path} ({self.pol.kind}/{self.pol.arch}, {pick}"
                f"{', stochastic' if self.stochastic else ''})")


class SlotSelector:
    """One UE per DL slot from per-UE weights.

    The muApp refreshes its action for the same target slot while UL slots go by; only the last refresh is applied, so
    the selector commits one decision per target TTI and recomputes (rather than repeats) a decision for a target it
    has already seen.
    """

    def __init__(self):
        self.credit = {}  # smooth weighted round robin: slots owed to each UE
        self.last = {}  # UE -> target TTI it was last picked for
        self.target = None
        self.saved = ({}, {})

    def pick(self, target, weights, mode):
        w = {u: float(x) for u, x in weights.items() if x > 0 and np.isfinite(x)}
        if not w:
            return None
        if target == self.target:
            self.credit, self.last = dict(self.saved[0]), dict(self.saved[1])
        else:
            self.target = target
            self.saved = (dict(self.credit), dict(self.last))
        if mode == "share":
            tot = sum(w.values())
            self.credit = {u: c for u, c in self.credit.items() if u in w}
            for u, x in w.items():
                self.credit[u] = self.credit.get(u, 0.0) + x / tot
            u = max(w, key=lambda k: (self.credit[k], -self.last.get(k, -1), -k))
            self.credit[u] -= 1.0
        else:
            top = max(w.values())
            cands = [k for k, x in w.items() if x >= top * (1 - 1e-9)]
            u = min(cands, key=lambda k: (self.last.get(k, -1), k))
        self.last[u] = target
        return u


def parse_weights(spec):
    """'ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4' or 'ue1 0.2 ue2 0.2 ...' -> ({name: w}, []);
    '0.2,0.2,0.2,0.4' -> ({}, [w...]) by UE order."""
    s = spec.replace(",", " ").replace("=", " ").split()
    if not s:
        raise ValueError("empty weights")
    if all(_is_num(t) for t in s):
        return {}, [float(t) for t in s]
    if len(s) % 2:
        raise ValueError(f"weights must be name/value pairs: {spec!r}")
    by_name = {}
    for name, val in zip(s[0::2], s[1::2]):
        if not _is_num(val):
            raise ValueError(f"bad weight {val!r} for {name}")
        by_name[name.lower()] = float(val)
    return by_name, []


def _is_num(t):
    try:
        float(t)
        return True
    except ValueError:
        return False


def make(name, args, ue_map):
    name = name.lower()
    if name == "stock":
        return Stock()
    if name == "fixed":
        if not getattr(args, "weights", None):
            raise ValueError("fixed needs --weights, e.g. ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4")
        return Fixed(args.weights, ue_map)
    if name in ("rr", "roundrobin", "round_robin"):
        return RoundRobin()
    if name == "pf":
        return PF(mode=getattr(args, "pf_mode", "cqi"), alpha=getattr(args, "pf_alpha", 0.01))
    if name == "maxcqi":
        return MaxCQI()
    if name == "bestcqi":
        return BestCQI()
    if name in ("maxweight", "mw"):
        return MaxWeight()
    if name == "rl":
        if not getattr(args, "model", None):
            raise ValueError("rl needs --model runs/<run>/best.npz")
        return RL(args.model, ue_map, stochastic=getattr(args, "stochastic", False),
                  pick=getattr(args, "rl_pick", None))
    raise ValueError(f"unknown scheduler {name!r} (stock fixed rr pf maxcqi bestcqi maxweight rl)")
