"""PPO scheduler for EdgeRIC-RT (EdgeRIC NSDI'24 §6, the throughput use case).

  state   per UE: CQI / 15 and log(1 + DL backlog) / log(1 + 8 MB), UEs in ue1..ueN order
  action  kind 'cat' (default, --action single): one UE for the slot, a ~ Categorical(softmax(logits(s)));
          kind 'gauss' (--action shared): per-UE logits z ~ N(mu(s), sigma), RB shares = softmax(z)
  reward  DRB bytes scheduled in the TTI the action was applied in (all UEs), / R_SCALE

Two network shapes (--arch):
  set  (default) one small network scores each UE from its own features and the mean and max of all UEs' features;
       the critic averages per-UE embeddings. The same weights serve every UE position, so the policy cannot learn a
       preference for a position, a sample from any UE trains all of them, and a model runs with any number of UEs.
  mlp  one network over the whole state vector (the original EdgeRIC shape). A 5000-TTI batch covers a short,
       slowly changing stretch of the channels, and this shape tends to learn "pick UE k" from it, then forget.

Samples come from the live loop: an action counts only if the report of its target TTI echoes its seq with status
HIT, and the reward is read from that same report, so every (state, action, reward) belongs to one TTI.

Torch is used only for the updates. Collection and deployment run NumpyPolicy: the same networks in numpy.
"""

import math

import numpy as np

B_NORM = float(np.log1p(8e6))
R_SCALE = 6000.0  # bytes: about one full DL slot at 51 PRBs, MCS 27
HIDDEN = 64
FEATS = 2  # per UE: CQI, backlog


def softmax(z):
    e = np.exp(z - np.max(z))
    return e / e.sum()


class Observer:
    """Report -> observation vector [cqi/15, log1p(backlog)/B_NORM] x n_ues, and the du_ue_index of each slot."""

    def __init__(self, n_ues, ue_map):
        self.n = n_ues
        self.ue_map = ue_map

    def encode(self, rep):
        ues = rep["ue"][: min(int(rep["ue_count"]), 16)]
        pos = self.ue_map.positions(ues)[: self.n]
        x = np.zeros(FEATS * self.n, dtype=np.float32)
        idx = [None] * self.n
        k = len(pos)
        if k:
            sel = ues[pos]
            x[0:FEATS * k:FEATS] = sel["cqi"] / 15.0
            x[1:FEATS * k:FEATS] = np.log1p(sel["dl_pending_bytes"].astype(np.float64)) / B_NORM
            idx[:k] = sel["du_ue_index"].tolist()
        return x, idx


class NumpyPolicy:
    """Actor (and critic) forward passes in numpy, for the real-time loop."""

    MLP_KEYS = ("pW1", "pb1", "pW2", "pb2", "pW3", "pb3", "log_std", "vW1", "vb1", "vW2", "vb2", "vW3", "vb3")
    SET_KEYS = ("aW1", "ab1", "aW2", "ab2", "aW3", "ab3", "log_std", "cW1", "cb1", "cW2", "cb2", "cW3", "cb3")
    KEYS = MLP_KEYS

    def __init__(self, params, n_ues, kind="gauss", arch="mlp"):
        keys = self.SET_KEYS if arch == "set" else self.MLP_KEYS
        self.p = {k: np.asarray(params[k], dtype=np.float64) for k in keys}
        self.n_ues = int(n_ues)
        self.kind = kind
        self.arch = arch
        self.rng = np.random.default_rng()
        if arch == "set":
            # The first actor layer acts on [own features, mean, max]: split it so a forward pass needs no concatenation.
            p = self.p
            w1 = p["aW1"]
            self._a1 = [np.ascontiguousarray(w1[:, k * FEATS:(k + 1) * FEATS].T) for k in range(3)]
            self._a2 = np.ascontiguousarray(p["aW2"].T)
            self._a3 = np.ascontiguousarray(p["aW3"][0])
            self._ab3 = float(p["ab3"][0])
            self._c1 = np.ascontiguousarray(p["cW1"].T)
            self._c2 = np.ascontiguousarray(p["cW2"].T)
            self._c3 = np.ascontiguousarray(p["cW3"][0])
            self._cb3 = float(p["cb3"][0])

    @staticmethod
    def _masked(logits, mask):
        if mask is None:
            return logits
        return np.where(np.asarray(mask, dtype=bool), logits, -1e9)

    @staticmethod
    def _present(mask):
        """None when every UE is present (the common case: no masking work), else the bool mask."""
        if mask is None:
            return None
        m = np.asarray(mask, dtype=bool)
        return None if m.all() else m

    def mean(self, x, mask=None):
        """Logits per UE."""
        p = self.p
        if self.arch == "mlp":
            h = np.tanh(p["pW1"] @ x + p["pb1"])
            h = np.tanh(p["pW2"] @ h + p["pb2"])
            return p["pW3"] @ h + p["pb3"]
        f = np.asarray(x, dtype=np.float64).reshape(-1, FEATS)
        m = self._present(mask)
        if m is None:
            avg, mx = f.mean(0), f.max(0)
        elif m.any():
            avg, mx = f[m].mean(0), f[m].max(0)
        else:
            avg, mx = np.zeros(FEATS), np.full(FEATS, -1.0)
        a1, a1avg, a1max = self._a1
        h = np.tanh(f @ a1 + (avg @ a1avg + mx @ a1max + p["ab1"]))
        h = np.tanh(h @ self._a2 + p["ab2"])
        return h @ self._a3 + self._ab3

    def value(self, x, mask=None):
        p = self.p
        if self.arch == "mlp":
            h = np.tanh(p["vW1"] @ x + p["vb1"])
            h = np.tanh(p["vW2"] @ h + p["vb2"])
            return float((p["vW3"] @ h + p["vb3"])[0])
        f = np.asarray(x, dtype=np.float64).reshape(-1, FEATS)
        h = np.tanh(f @ self._c1 + p["cb1"])
        h = np.tanh(h @ self._c2 + p["cb2"])
        m = self._present(mask)
        if m is None:
            pooled = h.mean(0)
        else:
            pooled = h[m].mean(0) if m.any() else np.zeros(h.shape[1])
        return float(pooled @ self._c3 + self._cb3)

    def argmax(self, x, mask=None):
        """The top UE (deterministic 'cat' action)."""
        return int(np.argmax(self._masked(self.mean(x, mask), mask)))

    def probs(self, x, mask=None, stochastic=False):
        """Per-UE probabilities: the slot (cat) or RB (gauss) shares. stochastic: one sampled action instead."""
        if self.kind == "cat":
            p = softmax(self._masked(self.mean(x, mask), mask))
            if stochastic:
                a = self.rng.choice(len(p), p=p)
                p = np.eye(len(p))[a]
            return p
        z = self.sample(x, mask)[0] if stochastic else self.mean(x, mask)
        return softmax(self._masked(z, mask))

    def sample_cat(self, x, mask=None, with_value=True):
        """(ue position, logp, value) for training a 'cat' policy. with_value=False skips the critic (0.0): the
        trainer evaluates it for the whole batch after collection, off the per-TTI path."""
        p = softmax(self._masked(self.mean(x, mask), mask))
        cs = np.cumsum(p)
        a = min(int(np.searchsorted(cs, self.rng.random() * cs[-1], side="right")), len(p) - 1)
        return a, math.log(max(float(p[a]), 1e-12)), (self.value(x, mask) if with_value else 0.0)

    def sample(self, x, mask=None, with_value=True):
        """(z, logp, value) for training: z ~ N(mu, sigma)."""
        mu = self.mean(x, mask)
        ls = np.broadcast_to(self.p["log_std"], mu.shape)
        std = np.exp(ls)
        z = mu + std * self.rng.standard_normal(mu.shape)
        logp = float(-0.5 * np.sum(((z - mu) / std) ** 2) - np.sum(ls) - 0.5 * len(z) * np.log(2 * np.pi))
        return z, logp, (self.value(x, mask) if with_value else 0.0)

    def act(self, x, stochastic=False, mask=None):
        z = self.sample(x, mask)[0] if stochastic else self.mean(x, mask)
        return softmax(z)

    def save(self, path, meta=None):
        np.savez(path, n_ues=self.n_ues, kind=np.array(self.kind), arch=np.array(self.arch),
                 meta=np.array(repr(meta or {})), **self.p)

    @classmethod
    def load(cls, path):
        d = np.load(path, allow_pickle=False)
        kind = str(d["kind"]) if "kind" in d.files else "gauss"
        arch = str(d["arch"]) if "arch" in d.files else "mlp"
        keys = cls.SET_KEYS if arch == "set" else cls.MLP_KEYS
        return cls({k: d[k] for k in keys}, int(d["n_ues"]), kind, arch)


def gae(rewards, values, last_value, gamma, lam):
    adv = np.zeros_like(rewards)
    run = 0.0
    for t in range(len(rewards) - 1, -1, -1):
        nv = values[t + 1] if t + 1 < len(values) else last_value
        delta = rewards[t] + gamma * nv - values[t]
        run = delta + gamma * lam * run
        adv[t] = run
    return adv, adv + values


def _set_nets(torch, nn, hidden):
    """Permutation-equivariant actor (a logit per UE) and permutation-invariant critic, over [B, n * FEATS]."""

    class Actor(nn.Module):
        def __init__(self):
            super().__init__()
            self.l1 = nn.Linear(3 * FEATS, hidden)
            self.l2 = nn.Linear(hidden, hidden)
            self.l3 = nn.Linear(hidden, 1)

        def forward(self, obs, mask):
            f = obs.view(obs.shape[0], -1, FEATS)
            m = mask.unsqueeze(-1)
            mf = m.to(f.dtype)
            avg = (f * mf).sum(1, keepdim=True) / mf.sum(1, keepdim=True).clamp(min=1.0)
            mx = f.masked_fill(~m, -1.0).max(1, keepdim=True).values
            u = torch.cat([f, avg.expand_as(f), mx.expand_as(f)], -1)
            h = torch.tanh(self.l2(torch.tanh(self.l1(u))))
            return self.l3(h).squeeze(-1)

    class Critic(nn.Module):
        def __init__(self):
            super().__init__()
            self.l1 = nn.Linear(FEATS, hidden)
            self.l2 = nn.Linear(hidden, hidden)
            self.l3 = nn.Linear(hidden, 1)

        def forward(self, obs, mask):
            f = obs.view(obs.shape[0], -1, FEATS)
            mf = mask.unsqueeze(-1).to(f.dtype)
            h = torch.tanh(self.l2(torch.tanh(self.l1(f))))
            pooled = (h * mf).sum(1) / mf.sum(1).clamp(min=1.0)
            return self.l3(pooled).squeeze(-1)

    return Actor(), Critic()


class PPO:
    """Actor-critic and PPO-clip updates (torch, CPU)."""

    def __init__(self, n_ues, lr=3e-4, gamma=0.9, lam=0.95, clip=0.2, epochs=10, minibatch=256, ent_coef=1e-3,
                 vf_coef=0.5, max_grad_norm=0.5, init_log_std=-0.5, threads=1, seed=0, kind="cat", arch="set"):
        import torch
        import torch.nn as nn

        torch.set_num_threads(threads)
        torch.manual_seed(seed)
        self.torch = torch
        self.kind = kind
        self.arch = arch
        self.n = n_ues
        d = FEATS * n_ues

        if arch == "set":
            self.pi, self.v = _set_nets(torch, nn, HIDDEN)
            last = self.pi.l3
            self.log_std = nn.Parameter(torch.full((1,), float(init_log_std)))
        else:
            def mlp(out):
                return nn.Sequential(nn.Linear(d, HIDDEN), nn.Tanh(), nn.Linear(HIDDEN, HIDDEN), nn.Tanh(),
                                     nn.Linear(HIDDEN, out))

            self.pi = mlp(n_ues)
            self.v = mlp(1)
            last = self.pi[-1]
            self.log_std = nn.Parameter(torch.full((n_ues,), float(init_log_std)))
        # Small last layer: start close to equal weights.
        with torch.no_grad():
            last.weight.mul_(0.01)
            last.bias.zero_()
        self.params = list(self.pi.parameters()) + list(self.v.parameters()) + [self.log_std]
        self.opt = torch.optim.Adam(self.params, lr=lr)
        self.gamma, self.lam, self.clip = gamma, lam, clip
        self.epochs, self.minibatch = epochs, minibatch
        self.ent_coef, self.vf_coef, self.max_grad_norm = ent_coef, vf_coef, max_grad_norm

    def logits(self, obs, mask=None):
        if self.arch == "set":
            if mask is None:
                mask = self.torch.ones(obs.shape[0], self.n, dtype=self.torch.bool)
            return self.pi(obs, mask)
        return self.pi(obs)

    def values(self, obs, mask=None):
        if self.arch == "set":
            if mask is None:
                mask = self.torch.ones(obs.shape[0], self.n, dtype=self.torch.bool)
            return self.v(obs, mask)
        return self.v(obs).squeeze(-1)

    def export(self):
        sd = {}
        if self.arch == "set":
            for pre, net in (("a", self.pi), ("c", self.v)):
                for k, layer in enumerate((net.l1, net.l2, net.l3), 1):
                    sd[f"{pre}W{k}"], sd[f"{pre}b{k}"] = layer.weight.detach().numpy(), layer.bias.detach().numpy()
        else:
            for pre, net in (("p", self.pi), ("v", self.v)):
                for k, i in enumerate((0, 2, 4), 1):
                    sd[f"{pre}W{k}"], sd[f"{pre}b{k}"] = net[i].weight.detach().numpy(), net[i].bias.detach().numpy()
        sd["log_std"] = self.log_std.detach().numpy()
        return NumpyPolicy(sd, self.n, self.kind, self.arch)

    def _dist(self, obs, mask):
        torch = self.torch
        mu = self.logits(obs, mask)
        if self.kind == "cat":
            if mask is not None:
                mu = mu.masked_fill(~mask, -1e9)
            return torch.distributions.Categorical(logits=mu), lambda d, a: d.log_prob(a), lambda d: d.entropy()
        dist = torch.distributions.Normal(mu, self.log_std.exp())
        return dist, lambda d, a: d.log_prob(a).sum(-1), lambda d: d.entropy().sum(-1)

    def update(self, obs, z, logp_old, values, rewards, last_value, mask=None):
        """z: the actions taken (UE positions for 'cat', logits for 'gauss'); mask: [T, n] UEs present."""
        torch = self.torch
        adv, ret = gae(rewards, values, last_value, self.gamma, self.lam)
        adv = (adv - adv.mean()) / (adv.std() + 1e-8)
        O = torch.as_tensor(obs, dtype=torch.float32)
        Z = torch.as_tensor(z, dtype=torch.long if self.kind == "cat" else torch.float32)
        M = torch.as_tensor(mask, dtype=torch.bool) if mask is not None else None
        LP = torch.as_tensor(logp_old, dtype=torch.float32)
        A = torch.as_tensor(adv, dtype=torch.float32)
        R = torch.as_tensor(ret, dtype=torch.float32)
        n = len(O)
        stats = dict(pi_loss=0.0, v_loss=0.0, entropy=0.0, approx_kl=0.0, clip_frac=0.0, updates=0)
        for _ in range(self.epochs):
            perm = torch.randperm(n)
            for s in range(0, n, self.minibatch):
                b = perm[s: s + self.minibatch]
                mb = M[b] if M is not None else None
                dist, log_prob, entropy = self._dist(O[b], mb)
                lp = log_prob(dist, Z[b])
                ratio = torch.exp(lp - LP[b])
                s1 = ratio * A[b]
                s2 = torch.clamp(ratio, 1 - self.clip, 1 + self.clip) * A[b]
                pi_loss = -torch.min(s1, s2).mean()
                v_loss = ((self.values(O[b], mb) - R[b]) ** 2).mean()
                ent = entropy(dist).mean()
                loss = pi_loss + self.vf_coef * v_loss - self.ent_coef * ent
                self.opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.params, self.max_grad_norm)
                self.opt.step()
                with torch.no_grad():
                    stats["pi_loss"] += float(pi_loss)
                    stats["v_loss"] += float(v_loss)
                    stats["entropy"] += float(ent)
                    stats["approx_kl"] += float((LP[b] - lp).mean())
                    stats["clip_frac"] += float(((ratio - 1).abs() > self.clip).float().mean())
                    stats["updates"] += 1
        k = max(stats.pop("updates"), 1)
        return {key: val / k for key, val in stats.items()}

    def state_dict(self):
        return dict(pi=self.pi.state_dict(), v=self.v.state_dict(), log_std=self.log_std.detach(),
                    opt=self.opt.state_dict(), kind=self.kind, arch=self.arch)

    def load_state_dict(self, sd):
        self.pi.load_state_dict(sd["pi"])
        self.v.load_state_dict(sd["v"])
        with self.torch.no_grad():
            self.log_std.copy_(sd["log_std"])
        self.opt.load_state_dict(sd["opt"])
