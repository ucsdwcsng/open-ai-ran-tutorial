"""Per-TTI run log (npz), rolling summaries, and 10 Hz aggregates to VictoriaMetrics for Grafana.

Throughput is reported per air time: bytes x 8 / (slots elapsed x 0.5 ms), using RANtime, so it does not depend
on how fast the ZMQ emulation runs relative to the wall clock. DRB bytes scheduled (`drb_bytes`) is the measure,
as in the EdgeRIC paper's reward; iperf3 at the UEs gives the delivered goodput.
"""

import os
import queue
import threading
import time
import urllib.request

import numpy as np

from .rte2 import SLOT_S, ues_of

MAX_UES = 16
UE_COLS = ("du_ue_index", "rnti", "cqi", "eff_cqi_x100", "dl_pending_bytes", "dl_prbs", "drb_bytes", "newtx_tbs",
           "retx_tbs", "dl_mcs", "rb_quota", "weight", "flags")
HDR_COLS = ("ran_tti", "t_slot_ns", "t_report_ns", "is_dl", "dl_rbs_avail", "dl_rbs_used", "n_weighted", "n_sel",
            "act_status", "act_seq", "act_tti", "act_src_tti", "act_t_src_report_ns", "act_t_sent_ns", "ue_count")


class RunLog:
    """Per-TTI record of every report, saved as npz (one array per field; UE fields are [T, 16])."""

    def __init__(self, path, chunk=65536):
        self.path = path
        self.chunk = chunk
        self.n = 0
        self.hdr = {c: np.zeros(chunk, dtype=np.uint64) for c in HDR_COLS}
        self.ue = {c: np.zeros((chunk, MAX_UES), dtype=np.uint32) for c in UE_COLS}
        self.events = []  # (ran_tti, text): scheduler switches etc.

    def add(self, rep):
        if self.n == len(self.hdr["ran_tti"]):
            for c in HDR_COLS:
                self.hdr[c] = np.concatenate([self.hdr[c], np.zeros(self.chunk, dtype=np.uint64)])
            for c in UE_COLS:
                self.ue[c] = np.concatenate([self.ue[c], np.zeros((self.chunk, MAX_UES), dtype=np.uint32)])
        i = self.n
        for c in HDR_COLS:
            self.hdr[c][i] = rep[c]
        ues = rep["ue"]
        for c in UE_COLS:
            self.ue[c][i, :] = ues[c]
        self.n += 1

    def event(self, ran_tti, text):
        self.events.append((int(ran_tti), text))

    def save(self, meta=None):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        arrays = {c: v[: self.n] for c, v in self.hdr.items()}
        arrays.update({"ue_" + c: v[: self.n] for c, v in self.ue.items()})
        arrays["events_tti"] = np.array([e[0] for e in self.events], dtype=np.int64)
        arrays["events_text"] = np.array([e[1] for e in self.events])
        if meta:
            arrays["meta"] = np.array(repr(meta))
        np.savez_compressed(self.path, **arrays)
        return self.path


class Window:
    """Rolling statistics between two summary lines."""

    def __init__(self):
        self.reset(None)

    def reset(self, first_tti):
        self.first_tti = first_tti
        self.last_tti = first_tti
        self.bytes = {}
        self.dl_slots = 0
        self.dl_hit = 0
        self.dl_hold = 0
        self.lat_us = []  # report -> action sent at the bridge, for applied actions
        self.slack_us = []  # action at the bridge -> slot start (positive: arrived in time)
        self.rbs_used = 0
        self.rbs_avail = 0

    def add(self, rep):
        tti = int(rep["ran_tti"])
        if self.first_tti is None:
            self.first_tti = tti
        self.last_tti = tti
        for u in ues_of(rep):
            k = int(u["du_ue_index"])
            self.bytes[k] = self.bytes.get(k, 0) + int(u["drb_bytes"])
        if rep["is_dl"]:
            self.dl_slots += 1
            st = int(rep["act_status"])
            self.dl_hit += st == 1
            self.dl_hold += st == 2
            self.rbs_used += int(rep["dl_rbs_used"])
            self.rbs_avail += int(rep["dl_rbs_avail"])
            if st == 1 and rep["act_t_src_report_ns"]:
                self.lat_us.append((int(rep["act_t_sent_ns"]) - int(rep["act_t_src_report_ns"])) / 1e3)
                self.slack_us.append((int(rep["t_slot_ns"]) - int(rep["act_t_sent_ns"])) / 1e3)

    def slots(self):
        if self.first_tti is None:
            return 0
        return ((self.last_tti - self.first_tti) & 0xFFFFFFFF) + 1

    def mbps(self):
        s = self.slots()
        if s == 0:
            return {}, 0.0
        per = {k: b * 8 / (s * SLOT_S) / 1e6 for k, b in self.bytes.items()}
        return per, sum(per.values())

    def summary(self):
        per, tot = self.mbps()
        hit = 100.0 * self.dl_hit / self.dl_slots if self.dl_slots else float("nan")
        lat = np.percentile(self.lat_us, [50, 99]) if self.lat_us else [float("nan")] * 2
        slack = np.percentile(self.slack_us, [1, 50]) if self.slack_us else [float("nan")] * 2
        return dict(per_ue_mbps=per, total_mbps=tot, dl_hit_pct=hit, dl_slots=self.dl_slots,
                    lat_p50_us=lat[0], lat_p99_us=lat[1], slack_p1_us=slack[0], slack_p50_us=slack[1],
                    rb_util=(self.rbs_used / self.rbs_avail) if self.rbs_avail else float("nan"))


class VmPusher:
    """Pushes InfluxDB line protocol to VictoriaMetrics from a background thread (never blocks the TTI loop)."""

    def __init__(self, url=None, measurement="edgeric_rt", period_s=0.1):
        self.url = url if url is not None else os.environ.get("VM_URL", "")
        self.measurement = measurement
        self.period_s = period_s
        self.q = queue.Queue(maxsize=64)
        self.win = Window()
        self.next_push = 0.0
        self.labels = {}
        self.failures = 0
        if self.url:
            threading.Thread(target=self._run, daemon=True).start()

    def add(self, rep, now, tags):
        """tags: e.g. {'scheduler': 'maxweight'}; ue names resolved by the caller via self.labels."""
        if not self.url:
            return
        self.win.add(rep)
        if now < self.next_push:
            return
        self.next_push = now + self.period_s
        s = self.win.summary()
        ts = time.time_ns()
        tagstr = ",".join(f"{k}={str(v).replace(' ', '_')}" for k, v in tags.items())
        lines = []
        for k, mbps in s["per_ue_mbps"].items():
            ue = self.labels.get(k, f"du{k}")
            lines.append(f"{self.measurement}_ue,{tagstr},ue={ue} tput_mbps={mbps:.3f} {ts}")
        last = {int(u["du_ue_index"]): u for u in ues_of(rep)}
        for k, u in last.items():
            ue = self.labels.get(k, f"du{k}")
            lines.append(f"{self.measurement}_ue,{tagstr},ue={ue} cqi={int(u['cqi'])}i,backlog={int(u['dl_pending_bytes'])}i,"
                         f"weight={int(u['weight'])}i,prbs={int(u['dl_prbs'])}i {ts}")
        f = [f"total_mbps={s['total_mbps']:.3f}"]
        for key in ("dl_hit_pct", "lat_p50_us", "lat_p99_us", "slack_p1_us", "rb_util"):
            if np.isfinite(s[key]):
                f.append(f"{key}={s[key]:.3f}")
        lines.append(f"{self.measurement},{tagstr} {','.join(f)} {ts}")
        self.win.reset(int(rep["ran_tti"]))
        try:
            self.q.put_nowait("\n".join(lines))
        except queue.Full:
            pass

    def _run(self):
        while True:
            body = self.q.get()
            try:
                req = urllib.request.Request(self.url, data=body.encode(), method="POST")
                urllib.request.urlopen(req, timeout=0.5).read()
            except Exception:  # noqa: BLE001 - metrics are best effort
                self.failures += 1
