"""RT-E2 client for EdgeRIC-RT muApps: per-TTI reports in, per-TTI actions out.

The TTI coupling follows the EdgeRIC paper (NSDI'24 §5.1.1, arXiv 2304.11199 §4.1):

  RANtime  the gNB's slot counter, carried in every report (ran_tti).
  RICtime  the RANtime of the last report this muApp has taken in.
  Lazy RAN (RANtime <= RICtime): no newer report yet. The muApp waits (poll() blocks).
  Lazy RIC (RANtime > RICtime + 1 when a batch is read): the muApp fell behind. It keeps every report for
           bookkeeping (rewards, logs) but acts only on the newest, and RICtime jumps to it.

An action is tagged with the RANtime of the slot it is for (the first DL slot >= `lead` TTIs after the
report; lead 1 is the paper's RANtime + 1). The rt_ctrl codelet applies it only in exactly that slot, and the
report of that slot echoes the action's seq with status HIT, which is how rewards are matched to actions.
"""

import os
import socket
import sys
import time
from collections import OrderedDict

import numpy as np

APP_DIR = os.environ.get("EDGERIC_RT_APP_DIR", "/apps/edgeric_rt")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
import rt_wire as W  # noqa: E402

UE_DTYPE = np.dtype([(f, "<u4") for f in W.UE_FIELDS])
REPORT_DTYPE = np.dtype(
    [(f, "<u8") for f in W.REPORT_FIELDS[:4]]
    + [(f, "<u4") for f in W.REPORT_FIELDS[4:25]]
    + [("ue_count", "<u2"), ("pad0", "<u2"), ("ue", UE_DTYPE, (W.MAX_UES,))]
)
assert REPORT_DTYPE.itemsize == W.REPORT_SIZE

SLOT_S = 0.5e-3  # 30 kHz SCS


def decode(buf):
    """Report bytes -> numpy record (fields as in rt_report.proto; ue[:ue_count] are the UEs)."""
    return np.frombuffer(buf, dtype=REPORT_DTYPE, count=1)[0]


def ues_of(rep):
    return rep["ue"][: min(int(rep["ue_count"]), W.MAX_UES)]


class RtE2:
    """Report socket (muapp.sock, bound here) and action socket (to the bridge's bridge.sock)."""

    def __init__(self, run_dir=None, lead=1):
        self.run_dir = run_dir or os.path.join(APP_DIR, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.path = os.path.join(self.run_dir, "muapp.sock")
        self.bridge_path = os.path.join(self.run_dir, "bridge.sock")
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.rx.bind(self.path)
        os.chmod(self.path, 0o666)
        self.tx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.tx.setblocking(False)
        self.lead = lead
        self.buf = bytearray(W.REPORT_SIZE + 64)

        self.ric_time = None
        self.seq = 0
        # Counters.
        self.reports = 0
        self.lazy_ric = 0  # reports read but not acted on: the muApp was behind
        self.lost = 0  # gaps in RANtime between consecutive reports (dropped before reaching the muApp)
        self.stale = 0  # reports not newer than RICtime (Lazy RAN: nothing new)
        self.restarts = 0  # RANtime went back: the gNB restarted
        self.sent = 0
        self.send_fail = 0

    def close(self):
        self.rx.close()
        self.tx.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def poll(self, timeout=1.0):
        """Block until at least one report arrives (Lazy RAN wait), then drain everything queued.

        Returns the reports oldest first as numpy records; [] on timeout. RICtime advances to the newest.
        """
        out = []
        self.rx.settimeout(timeout)
        try:
            n = self.rx.recv_into(self.buf)
        except socket.timeout:
            return out
        self._take(n, out)
        self.rx.setblocking(False)
        while True:
            try:
                n = self.rx.recv_into(self.buf)
            except BlockingIOError:
                break
            self._take(n, out)
        if len(out) > 1:
            self.lazy_ric += len(out) - 1
        return out

    def drain(self):
        """Read everything queued right now without waiting (e.g. reports that piled up while the muApp was not
        acting) and return the number of reports taken. RICtime moves to the newest."""
        out = []
        self.rx.setblocking(False)
        while True:
            try:
                n = self.rx.recv_into(self.buf)
            except BlockingIOError:
                break
            self._take(n, out)
        return len(out)

    def _take(self, n, out):
        if n != W.REPORT_SIZE:
            return
        rep = decode(bytes(self.buf[:n]))
        tti = int(rep["ran_tti"])
        if self.ric_time is not None:
            d = W.tti_diff(tti, self.ric_time)
            if d < -1000:
                self.restarts += 1  # gNB restarted: start over
            elif d <= 0:
                self.stale += 1
                return
            elif d > 1:
                self.lost += d - 1
        self.ric_time = tti
        self.reports += 1
        out.append(rep)

    def target_tti(self, rep, lead=None):
        return W.next_dl_tti(int(rep["ran_tti"]), int(rep["dl_mask"]), self.lead if lead is None else lead)

    def send(self, rep, weights, flags=0, hold=0, lead=None):
        """Send an action computed from report `rep`. weights: {du_ue_index: int weight}. Returns (seq, target)."""
        target = self.target_tti(rep, lead)
        if target is None or not weights:
            return None, None
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        msg = W.encode_action(self.seq, target, int(rep["ran_tti"]), sorted(weights.items()), flags=flags,
                              hold=hold, t_src_report_ns=int(rep["t_report_ns"]))
        try:
            self.tx.sendto(msg, self.bridge_path)
            self.sent += 1
        except OSError:
            self.send_fail += 1
            return None, None
        return self.seq, target


def to_int_weights(w, min_share=0.0, scale=1 << 16):
    """{ue: float >= 0} -> {ue: int}, shares below min_share dropped and the rest renormalized."""
    w = {u: float(x) for u, x in w.items() if x > 0 and np.isfinite(x)}
    tot = sum(w.values())
    if tot <= 0:
        return {}
    if min_share > 0:
        w = {u: x for u, x in w.items() if x / tot >= min_share}
        tot = sum(w.values())
        if tot <= 0:
            return {}
    return {u: max(1, int(round(scale * x / tot))) for u, x in w.items()}


class SampleRing:
    """Actions sent but not yet applied: seq -> sample. A sample completes when the report of its target TTI
    echoes the seq with status HIT; its reward comes from that report."""

    def __init__(self, maxlen=1024):
        self.pending = OrderedDict()
        self.maxlen = maxlen
        self.hit = 0
        self.expired = 0

    def add(self, seq, target, sample):
        self.pending[seq] = (target, sample)
        while len(self.pending) > self.maxlen:
            self.pending.popitem(last=False)
            self.expired += 1

    def resolve(self, rep):
        """If `rep` is the slot an action was applied in, pop and return (sample, rep); else None. Actions whose
        target is already past are dropped (they missed their TTI)."""
        tti = int(rep["ran_tti"])
        out = None
        if int(rep["act_status"]) == W.STATUS_HIT:
            item = self.pending.pop(int(rep["act_seq"]), None)
            if item is not None:
                self.hit += 1
                out = (item[1], rep)
        # Expire actions for slots already reported.
        while self.pending:
            seq, (target, _) = next(iter(self.pending.items()))
            if W.tti_diff(target, tti) <= 0:
                self.pending.popitem(last=False)
                self.expired += 1
            else:
                break
        return out


class Pacer:
    """Real-time placement of the muApp process."""

    @staticmethod
    def apply(cpus=None, fifo=0):
        msgs = []
        if cpus:
            try:
                os.sched_setaffinity(0, cpus)
                msgs.append(f"pinned to {sorted(cpus)}")
            except OSError as e:
                msgs.append(f"cannot pin: {e}")
        if fifo:
            try:
                os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(fifo))
                msgs.append(f"SCHED_FIFO {fifo}")
            except OSError as e:
                msgs.append(f"cannot set SCHED_FIFO: {e}")
        return msgs


def parse_cpus(spec):
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def now_ns():
    return time.monotonic_ns()
