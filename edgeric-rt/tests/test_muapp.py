"""Offline tests of the EdgeRIC-RT muApp library (no gNB needed).

    python3 -m unittest discover -s tests -v      (in the edgeric container, from /edgeric)
"""

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from edgeric_rt import policies, rte2  # noqa: E402
from edgeric_rt.ue_map import UeMap  # noqa: E402

W = rte2.W


def make_report(ran_tti, ues, act_status=0, act_seq=0, is_dl=1, dl_mask=0b0001111111):
    """ues: list of dicts with du_ue_index, rnti, cqi, dl_pending_bytes, drb_bytes."""
    buf = bytearray(W.REPORT_SIZE)
    hdr = dict.fromkeys(W.REPORT_FIELDS, 0)
    hdr.update(ran_tti=ran_tti, is_dl=is_dl, dl_mask=dl_mask, act_status=act_status, act_seq=act_seq,
               ue_count=len(ues), t_report_ns=1000 * ran_tti)
    W.REPORT_HDR.pack_into(buf, 0, *[hdr[f] for f in W.REPORT_FIELDS])
    for i, u in enumerate(ues):
        rec = dict.fromkeys(W.UE_FIELDS, 0)
        rec.update(u)
        W.UE_REC.pack_into(buf, W.UE_OFFSET + i * W.UE_REC.size, *[rec[f] for f in W.UE_FIELDS])
    return rte2.decode(bytes(buf))


UES = [dict(du_ue_index=0, rnti=0x4601, cqi=15, dl_pending_bytes=10**6, drb_bytes=3000),
       dict(du_ue_index=1, rnti=0x4602, cqi=7, dl_pending_bytes=10**6, drb_bytes=1000),
       dict(du_ue_index=2, rnti=0x4603, cqi=11, dl_pending_bytes=0, drb_bytes=0),
       dict(du_ue_index=3, rnti=0x4604, cqi=4, dl_pending_bytes=500, drb_bytes=10)]


class WireTest(unittest.TestCase):
    def test_report_roundtrip(self):
        rep = make_report(1234, UES, act_status=1, act_seq=9)
        self.assertEqual(int(rep["ran_tti"]), 1234)
        self.assertEqual(int(rep["act_seq"]), 9)
        self.assertEqual(len(rte2.ues_of(rep)), 4)
        self.assertEqual(int(rte2.ues_of(rep)[1]["rnti"]), 0x4602)

    def test_action_roundtrip(self):
        msg = W.encode_action(5, 100, 99, [(0, 65536), (3, 1)], flags=W.FLAG_WORK_CONSERVING, hold=2)
        d = W.decode_action(msg)
        self.assertEqual((d["seq"], d["tti"], d["src_tti"], d["hold"]), (5, 100, 99, 2))
        self.assertEqual(d["weights"], [(0, 65536), (3, 1)])

    def test_target_is_next_dl_slot(self):
        # slots 0..6 DL, 7..9 UL (bit k: slot ran_tti + k): from the last DL slot the target skips the UL slots
        mask = sum(1 << k for k in range(32) if (6 + k) % 10 < 7)
        self.assertEqual(W.next_dl_tti(1006, mask, 1), 1010)
        self.assertEqual(W.next_dl_tti(1003, 0xFFFFFFFF, 1), 1004)
        self.assertEqual(W.tti_diff(2, 0xFFFFFFFF), 3)


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.rep = make_report(10, UES)
        self.map = UeMap(None)

    def test_maxweight_skips_empty_buffers(self):
        w = policies.MaxWeight().weights(self.rep)
        self.assertNotIn(2, w)
        self.assertGreater(w[0], w[1])

    def test_bestcqi_picks_the_best_ue_with_data(self):
        sel = policies.SlotSelector()
        pol = policies.BestCQI()
        self.assertTrue(pol.always_single)
        self.assertEqual(sel.pick(100, pol.weights(self.rep), pol.single_mode), 0)

    def test_rr_rotates_over_ues_with_data_once_per_slot(self):
        sel = policies.SlotSelector()
        rr = policies.RoundRobin()
        seen = [sel.pick(100 + t, rr.weights(self.rep), rr.single_mode) for t in range(4)]
        self.assertEqual(seen, [0, 1, 3, 0])

    def test_fixed_by_name_uses_the_ue_map(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("ue1 0x4601\nue2 0x4603\nue3 0x4604\nue4 0x4602\n")
        m = UeMap(f.name)
        w = policies.Fixed("ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4", m).weights(self.rep)
        self.assertEqual(w, {0: 0.2, 2: 0.2, 3: 0.2, 1: 0.4})
        os.unlink(f.name)

    def test_fixed_by_position_without_a_map(self):
        w = policies.Fixed("0.1 0.2 0.3 0.4", self.map).weights(self.rep)
        self.assertEqual(w, {0: 0.1, 1: 0.2, 2: 0.3, 3: 0.4})

    def test_parse_weights_forms(self):
        self.assertEqual(policies.parse_weights("ue1 0.2 ue2 0.8")[0], {"ue1": 0.2, "ue2": 0.8})
        self.assertEqual(policies.parse_weights("0.5,0.5")[1], [0.5, 0.5])
        with self.assertRaises(ValueError):
            policies.parse_weights("ue1 0.2 ue2")

    def test_int_weights_min_share(self):
        iw = rte2.to_int_weights({0: 0.99, 1: 0.01}, min_share=0.02)
        self.assertEqual(list(iw), [0])
        iw = rte2.to_int_weights({0: 3, 1: 1})
        self.assertAlmostEqual(iw[0] / (iw[0] + iw[1]), 0.75, places=3)


class SlotSelectorTest(unittest.TestCase):
    def test_share_mode_hands_out_slots_in_proportion(self):
        sel = policies.SlotSelector()
        w = {0: 0.2, 1: 0.2, 2: 0.2, 3: 0.4}
        picks = [sel.pick(1000 + t, w, "share") for t in range(1000)]
        self.assertEqual([picks.count(u) for u in range(4)], [200, 200, 200, 400])
        # interleaved, not in bursts: UE 3 (40% of the slots) never waits more than 4 slots
        gaps = np.diff([t for t, u in enumerate(picks) if u == 3])
        self.assertLessEqual(int(gaps.max()), 4)

    def test_metric_mode_rotates_ties(self):
        sel = policies.SlotSelector()
        picks = [sel.pick(10 + t, {0: 15.0, 1: 15.0, 2: 15.0}, "metric") for t in range(6)]
        self.assertEqual(picks, [0, 1, 2, 0, 1, 2])
        self.assertEqual(sel.pick(99, {0: 9.0, 1: 15.0}, "metric"), 1)

    def test_refresh_for_the_same_slot_does_not_advance(self):
        sel = policies.SlotSelector()
        w = {0: 0.5, 1: 0.5}
        a = sel.pick(50, w, "share")
        self.assertEqual(sel.pick(50, w, "share"), a)  # refresh while UL slots go by
        self.assertEqual(sel.pick(50, w, "share"), a)
        self.assertNotEqual(sel.pick(51, w, "share"), a)  # next DL slot: the other UE


class RicTimeTest(unittest.TestCase):
    """RICtime rules: stale reports ignored (Lazy RAN), gaps counted, applied actions matched by seq."""

    def test_take_ignores_stale_and_counts_gaps(self):
        rt = rte2.RtE2.__new__(rte2.RtE2)
        rt.ric_time, rt.reports, rt.stale, rt.lost, rt.restarts = None, 0, 0, 0, 0
        out = []
        for tti in (100, 101, 101, 99, 104):
            buf = bytearray(W.REPORT_SIZE)
            W.REPORT_HDR.pack_into(buf, 0, *[tti if f == "ran_tti" else 0 for f in W.REPORT_FIELDS])
            rt.buf = buf
            rt._take(W.REPORT_SIZE, out)
        self.assertEqual([int(r["ran_tti"]) for r in out], [100, 101, 104])
        self.assertEqual((rt.stale, rt.lost), (2, 2))

    def test_sample_ring_matches_seq_and_expires(self):
        ring = rte2.SampleRing()
        ring.add(7, 101, "s7")
        ring.add(8, 102, "s8")
        self.assertIsNone(ring.resolve(make_report(101, UES, act_status=0)))  # 101 had no HIT: s7 expires
        res = ring.resolve(make_report(102, UES, act_status=1, act_seq=8))
        self.assertEqual(res[0], "s8")
        self.assertEqual((ring.hit, ring.expired), (1, 1))


class PpoTest(unittest.TestCase):
    def test_update_and_numpy_export_agree(self):
        from edgeric_rt.rl.ppo import PPO, NumpyPolicy

        ppo = PPO(4, epochs=2, minibatch=64, kind="gauss", arch="mlp")
        rng = np.random.default_rng(0)
        obs = rng.random((256, 8)).astype(np.float32)
        pol = ppo.export()
        samp = [pol.sample(o) for o in obs]
        z = np.stack([s[0] for s in samp])
        logp = np.array([s[1] for s in samp])
        val = np.array([s[2] for s in samp])
        rew = obs[:, 0] * 0.5  # reward favours UE 1's CQI
        stats = ppo.update(obs, z, logp, val, rew, last_value=0.0)
        self.assertTrue(np.isfinite(stats["pi_loss"]))
        pol = ppo.export()
        x = obs[0]
        import torch

        with torch.no_grad():
            mu_t = ppo.pi(torch.as_tensor(x)).numpy()
        np.testing.assert_allclose(pol.mean(x), mu_t, rtol=1e-4, atol=1e-5)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.npz")
            pol.save(p)
            np.testing.assert_allclose(NumpyPolicy.load(p).mean(x), pol.mean(x))
        w = pol.act(x)
        self.assertAlmostEqual(float(w.sum()), 1.0, places=6)

    def test_categorical_update_and_export(self):
        from edgeric_rt.rl.ppo import PPO, NumpyPolicy

        ppo = PPO(4, epochs=2, minibatch=64, kind="cat", arch="mlp")
        rng = np.random.default_rng(1)
        obs = rng.random((256, 8)).astype(np.float32)
        mask = np.ones((256, 4), dtype=bool)
        mask[:, 3] = False  # UE 4 absent: never picked
        pol = ppo.export()
        samp = [pol.sample_cat(o, m) for o, m in zip(obs, mask)]
        self.assertTrue(all(a != 3 for a, _, _ in samp))
        act = np.array([a for a, _, _ in samp])
        logp = np.array([lp for _, lp, _ in samp])
        val = np.array([v for _, _, v in samp])
        rew = (act == 0).astype(float)  # reward picking UE 1
        for _ in range(5):
            ppo.update(obs, act, logp, val, rew, last_value=0.0, mask=mask)
        pol = ppo.export()
        self.assertEqual(pol.kind, "cat")
        import torch

        with torch.no_grad():
            p_t = torch.softmax(ppo.pi(torch.as_tensor(obs[0])), -1).numpy()
        np.testing.assert_allclose(pol.probs(obs[0]), p_t, rtol=1e-4, atol=1e-6)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.npz")
            pol.save(path)
            q = NumpyPolicy.load(path)
            self.assertEqual(q.kind, "cat")
            self.assertGreater(q.probs(obs[0], mask[0])[0], 0.25)  # moved toward the rewarded UE


class SetPolicyTest(unittest.TestCase):
    """The default network: shared across UE positions."""

    def setUp(self):
        from edgeric_rt.rl.ppo import PPO

        self.ppo = PPO(4, epochs=3, minibatch=64, kind="cat", arch="set", seed=3)
        # scramble the small last layer so the outputs are not all ~0
        import torch

        with torch.no_grad():
            self.ppo.pi.l3.weight.normal_(0, 1.0)
        self.rng = np.random.default_rng(2)

    def test_numpy_export_matches_torch_and_masks(self):
        import torch

        pol = self.ppo.export()
        obs = self.rng.random((32, 8)).astype(np.float32)
        mask = np.ones((32, 4), dtype=bool)
        mask[::3, 2] = False
        with torch.no_grad():
            lt = self.ppo.logits(torch.as_tensor(obs), torch.as_tensor(mask)).numpy()
            vt = self.ppo.values(torch.as_tensor(obs), torch.as_tensor(mask)).numpy()
        for k in range(32):
            np.testing.assert_allclose(pol.mean(obs[k], mask[k]), lt[k], rtol=1e-4, atol=1e-5)
            self.assertAlmostEqual(pol.value(obs[k], mask[k]), float(vt[k]), places=4)
            if not mask[k, 2]:
                self.assertLess(pol.probs(obs[k], mask[k])[2], 1e-12)  # an absent UE is never picked

    def test_permuting_ues_permutes_logits_and_keeps_value(self):
        pol = self.ppo.export()
        x = self.rng.random(8)
        perm = [2, 0, 3, 1]
        xp = x.reshape(4, 2)[perm].ravel()
        np.testing.assert_allclose(pol.mean(xp), pol.mean(x)[perm], rtol=1e-9, atol=1e-12)
        self.assertAlmostEqual(pol.value(xp), pol.value(x), places=12)

    def test_save_load_keeps_the_shape(self):
        pol = self.ppo.export()
        x = self.rng.random(8)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.npz")
            pol.save(path)
            from edgeric_rt.rl.ppo import NumpyPolicy

            q = NumpyPolicy.load(path)
        self.assertEqual((q.kind, q.arch), ("cat", "set"))
        np.testing.assert_allclose(q.mean(x), pol.mean(x))

    def test_learning_on_some_positions_transfers_to_the_others(self):
        # reward the best-CQI UE; in training it is only ever UE 0 or UE 1
        from edgeric_rt.rl.ppo import PPO

        ppo = PPO(4, epochs=4, minibatch=128, kind="cat", arch="set", lr=3e-3, seed=0)
        for _ in range(8):
            pol = ppo.export()
            obs, act, logp, val, rew = [], [], [], [], []
            for _ in range(512):
                c = self.rng.integers(4, 10, 4)
                c[self.rng.integers(0, 2)] = self.rng.integers(11, 16)
                x = np.ravel(np.column_stack([c / 15.0, np.full(4, 0.8)])).astype(np.float32)
                a, lp, v = pol.sample_cat(x)
                obs.append(x), act.append(a), logp.append(lp), val.append(v), rew.append(c[a] / 15.0)
            ppo.update(np.stack(obs), np.array(act), np.array(logp), np.array(val), np.array(rew), 0.0,
                       mask=np.ones((512, 4), dtype=bool))
        pol = ppo.export()
        x = np.ravel(np.column_stack([[5, 6, 7, 14], np.full(4, 0.8)])).astype(np.float32)
        x[0::2] /= 15.0
        self.assertEqual(int(np.argmax(pol.probs(x))), 3)  # UE 3 was never the best in training

    def test_rl_scheduler_picks_argmax_for_a_cat_model(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.npz")
            self.ppo.export().save(path)
            self.assertEqual(policies.RL(path, UeMap(None)).single_mode, "metric")
            self.assertEqual(policies.RL(path, UeMap(None), pick="share").single_mode, "share")


class ObserverTest(unittest.TestCase):
    def test_encode_follows_the_ue_map_and_caches_the_order(self):
        from edgeric_rt.rl.ppo import B_NORM, Observer

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("ue1 0x4603\nue2 0x4601\nue3 0x4604\nue4 0x4602\n")
        try:
            obs = Observer(4, UeMap(f.name))
            rep = make_report(10, UES)
            for _ in range(2):  # second call: cached order
                x, idx = obs.encode(rep)
                self.assertEqual(idx, [2, 0, 3, 1])
                np.testing.assert_allclose(x[0::2], np.array([11, 15, 4, 7]) / 15.0, rtol=1e-6)
                np.testing.assert_allclose(x[1::2], np.log1p([0, 10**6, 500, 10**6]) / B_NORM, rtol=1e-6)
            x, idx = Observer(4, UeMap(f.name)).encode(make_report(11, UES[:2]))  # ue1 and ue3 gone
            self.assertEqual(idx, [0, 1, None, None])
            self.assertEqual(float(x[4]), 0.0)
        finally:
            os.unlink(f.name)


class PickQualityTest(unittest.TestCase):
    def test_best_pick_and_efficiency(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "muapps"))
        from train_ppo import pick_quality

        x = np.ravel(np.column_stack([np.array([15, 7, 11, 4]) / 15.0, np.full(4, 0.5)]))
        m = np.ones(4, dtype=bool)
        self.assertEqual(pick_quality(x, 0, m, "cat"), (1.0, 1.0))
        best, eff = pick_quality(x, 2, m, "cat")
        self.assertEqual(best, 0.0)
        self.assertAlmostEqual(eff, policies.CQI_EFF[11] / policies.CQI_EFF[15])
        m[0] = False  # UE 0 absent: UE 2 is now the best present
        self.assertEqual(pick_quality(x, 2, m, "cat")[0], 1.0)


if __name__ == "__main__":
    unittest.main()
