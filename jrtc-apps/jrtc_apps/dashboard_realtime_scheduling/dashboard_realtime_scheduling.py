# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
#
# dashboard-realtime-scheduling - what the DL scheduler does, per UE, for the Grafana dashboard of the same name
# (telemetry/rt-scheduling-dashboard.json, uid rt-scheduling).
#
# Consumes out_rts_stats: one message per 134 ms bucket from the rts_stats codelet on mac_sched_slot_report, which
# already aggregated every slot of the bucket. Like upt_app_demo.py, it turns a bucket into samples stamped at the
# bucket's close and writes them to VictoriaMetrics in InfluxDB line protocol:
#
#   rts_ue_{cqi,cqi_min,cqi_max,backlog_bytes,backlog_max_bytes,dl_mbps,tb_mbps,retx_mbps,prb_pct,slot_pct,mcs}
#       {du, rnti}       per UE; dl_mbps counts the DRB bytes of the UE's grants (the gNB throughput)
#   rts_cell_{dl_mbps,hit_pct,hold_pct,rb_util_pct,rt_factor}{cell}
#   rts_ue_ids{n, imsi, ip, amf_ue_ngap_id, ran_ue_ngap_id, du, rnti} = 1, every 2 s: the UE identity table
#
# The bucket grid (1 << 27 ns of epoch time) is the one telemetry/tcp_probe.py uses for the TCP throughput it
# measures with tcpdump on the UPF, so the gNB and TCP panels line up sample for sample.
#
# UE identities. Every bucket carries each UE's DU UE index and C-RNTI. ue_map.txt (scripts/ue_rnti_map.sh, the
# file the EdgeRIC muApps read) names each C-RNTI ue<n>, and UE n has IMSI ...n on this testbed
# (setup_zmq_chan_demo.sh). The core's view (IMSI, IP, NGAP UE IDs) arrives as the dashboard xApp's core events
# (scripts/core_ue_info.sh) on UDP 127.0.0.1:30503, read without blocking. Neither needs this xApp loaded before
# the UEs attach, unlike the dashboard xApp's ue_contexts codelets.
#
# One thread, flushing inline: jrtc runs each app in a Python sub-interpreter and aborts on unload while another
# thread of it lives (see upt_app_demo.py). And light: jrtc's Python xApps share one GIL with the rt_bridge xApp,
# which handles every TTI and waits for the GIL whenever this app holds it. So a bucket is read with one struct
# unpack rather than ctypes field by field, each line is one %-format, and the writes go over a kept-alive HTTP
# connection rather than urllib, whose request building and response parsing cost ~0.4 ms of CPU per write (20x).

import ctypes
import json
import os
import socket
import struct
import sys
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field

JRTC_APP_PATH = os.environ.get("JRTC_APP_PATH")
if JRTC_APP_PATH is None:
    raise ValueError("JRTC_APP_PATH not set")
sys.path.append(f"{JRTC_APP_PATH}")

import jrtc_app
from jrtc_app import *

from rts_stats import struct__rts_stats, struct__rts_ue

# Must match UPT_BUCKET_SHIFT in codelets/upt/upt_helpers.h
UPT_BUCKET_SHIFT = 27
UPT_BUCKET_NS = 1 << UPT_BUCKET_SHIFT          # 134.217728 ms
UPT_BUCKET_SECS = UPT_BUCKET_NS / 1e9
MAX_UES = 16
SLOT_SECS = 0.5e-3                             # 30 kHz SCS (n78 TDD on this testbed)

VM_URL = os.environ.get("RTS_VM_URL", "http://victoria-metrics.ran.svc.cluster.local:8428")
WRITE_URL = f"{VM_URL.rstrip('/')}/write"
UE_MAP = os.environ.get("RTS_UE_MAP", "/apps/edgeric_rt/run/ue_map.txt")
CORE_PORT = int(os.environ.get("RTS_CORE_PORT", "30503"))

# rts_stats as the codelet writes it (nanopb struct): header, then ue[16].
HDR = struct.Struct("<QQ9I")    # timestamp, bucket_id, cell_id, slots, dl_slots, hit, hold, rbs_avail, rbs_used,
                                # tti_first, tti_last
UE = struct.Struct("<6IQ8I")    # du_ue_index, rnti, slots, cqi_sum, cqi_min, cqi_max, backlog_sum, backlog_max,
                                # backlog_last, drb_bytes, newtx_bytes, retx_bytes, prbs, grant_slots, mcs_sum
UE_OFF = struct__rts_stats.ue.offset
STATS_SIZE = ctypes.sizeof(struct__rts_stats)
assert UE.size == ctypes.sizeof(struct__rts_ue) and UE_OFF + MAX_UES * UE.size == STATS_SIZE, "rts_stats layout"
MBPS = 8 / UPT_BUCKET_SECS / 1e6

FLUSH_SECS = 0.5
IDS_SECS = 2.0
UE_MAP_SECS = 2.0

STREAM_URI = b"dashboard-realtime-scheduling://jbpf_agent/dashboard_realtime_scheduling/rts_stats"
STREAM_CHAN = b"out_rts_stats"
RTS_SIDX = 0


class VmConn:
    """POST /write to VictoriaMetrics over one kept-alive HTTP/1.1 connection, reopened after any error."""

    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        self.addr = (u.hostname, u.port or 80)
        self.head = (f"POST {u.path.rstrip('/')}/write HTTP/1.1\r\nHost: {u.netloc}\r\n"
                     f"Content-Type: text/plain\r\nContent-Length: ").encode()
        self.sock = None

    def post(self, body: bytes):
        try:
            if self.sock is None:
                self.sock = socket.create_connection(self.addr, timeout=1)
            self.sock.sendall(b"%s%d\r\n\r\n%s" % (self.head, len(body), body))
            resp = b""
            while b"\r\n\r\n" not in resp:      # 204 No Content: the headers are the whole answer
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("VictoriaMetrics closed the connection")
                resp += chunk
            if not resp.startswith(b"HTTP/1.1 2"):
                raise ConnectionError(resp.split(b"\r\n", 1)[0].decode(errors="replace"))
        except Exception:
            self.close()
            raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None


@dataclass
class AppStateVars:
    app: JrtcApp = None
    lines: list = field(default_factory=list)
    sent: int = 0
    failed: int = 0
    paused_until: float = 0.0
    buckets: int = 0
    errors: int = 0
    next_flush: float = 0.0
    next_ids: float = 0.0
    # identities
    ues: dict = field(default_factory=dict)         # du_ue_index -> C-RNTI, from the latest bucket
    rnti_n: dict = field(default_factory=dict)      # C-RNTI -> n, from ue_map.txt
    ue_map_mtime: float = -1.0
    next_ue_map: float = 0.0
    core: dict = field(default_factory=dict)        # n -> {imsi, ip, amf_ue_ngap_id, ran_ue_ngap_id}
    core_sock: object = None
    vm: VmConn = field(default_factory=lambda: VmConn(VM_URL))

    def emit(self, measurement, tags, fields, ts_ns):
        tagstr = ",".join(f"{k}={v}" for k, v in tags.items())
        fieldstr = ",".join(f"{k}={v}" for k, v in fields.items())
        self.lines.append(f"{measurement},{tagstr} {fieldstr} {ts_ns}")

    def flush(self):
        """Posts the batch, on a new connection if the kept-alive one is gone. A write that fails twice drops the
        batch and pauses writing for 10 s, so an unreachable VictoriaMetrics cannot stall the app (as the dashboard
        xApp's VmWriter does)."""
        if not self.lines:
            return
        body = "\n".join(self.lines).encode()
        n = len(self.lines)
        self.lines = []
        if time.monotonic() < self.paused_until:
            self.failed += n
            return
        try:
            try:
                self.vm.post(body)
            except Exception:
                self.vm.post(body)  # VictoriaMetrics closes connections every 2 min (-http.connTimeout): once more
            self.sent += n
        except Exception as e:
            self.failed += n
            self.paused_until = time.monotonic() + 10
            print(f"[rts] VictoriaMetrics write failed ({self.failed} lines so far): {e}", flush=True)


def handle_stats(state: AppStateVars, buf: bytes):
    _, bucket_id, cell, _, dl_slots, hit, hold, rbs_avail, rbs_used, tti_first, tti_last = HDR.unpack_from(buf)
    ts_ns = (bucket_id + 1) * UPT_BUCKET_NS
    total = 0
    ues = {}
    lines = state.lines
    for (du, rnti, slots, cqi_sum, cqi_min, cqi_max, backlog_sum, backlog_max, _, drb, newtx, retx, prbs, grants,
         mcs_sum) in UE.iter_unpack(buf[UE_OFF:STATS_SIZE]):
        if slots == 0:
            continue
        total += drb
        ues[du] = rnti
        lines.append(
            "rts_ue,du=%d,rnti=0x%04x cqi=%.2f,cqi_min=%d,cqi_max=%d,backlog_bytes=%d,backlog_max_bytes=%d,"
            "dl_mbps=%.3f,tb_mbps=%.3f,retx_mbps=%.3f,prb_pct=%.2f,slot_pct=%.2f%s %d" % (
                du, rnti, cqi_sum / slots, cqi_min, cqi_max, backlog_sum // slots, backlog_max,
                drb * MBPS, newtx * MBPS, retx * MBPS,
                100.0 * prbs / rbs_avail if rbs_avail else 0.0, 100.0 * grants / dl_slots if dl_slots else 0.0,
                ",mcs=%.2f" % (mcs_sum / grants) if grants else "", ts_ns))
    # rt_factor, air time over wall time: RANtime counts every slot, also those the emulated radio skipped
    lines.append("rts_cell,cell=%d dl_mbps=%.3f,hit_pct=%.2f,hold_pct=%.2f,rb_util_pct=%.2f,rt_factor=%.4f %d" % (
        cell, total * MBPS, 100.0 * hit / dl_slots if dl_slots else 0.0, 100.0 * hold / dl_slots if dl_slots else 0.0,
        100.0 * rbs_used / rbs_avail if rbs_avail else 0.0,
        (((tti_last - tti_first) & 0xFFFFFFFF) + 1) * SLOT_SECS / UPT_BUCKET_SECS, ts_ns))
    if ues != state.ues:
        state.next_ids = 0.0  # a UE came, went or changed C-RNTI: refresh the identity table now
    state.ues = ues
    state.buckets += 1


def read_ue_map(state: AppStateVars):
    """ue_map.txt: "ue<n> 0x<C-RNTI>" lines. Re-read when it changes."""
    try:
        mtime = os.stat(UE_MAP).st_mtime
    except OSError:
        state.rnti_n, state.ue_map_mtime = {}, -1.0
        return
    if mtime == state.ue_map_mtime:
        return
    rnti_n = {}
    with open(UE_MAP) as f:
        for line in f:
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and parts[0].startswith("ue") and parts[0][2:].isdigit():
                try:
                    rnti_n[int(parts[1], 16)] = int(parts[0][2:])
                except ValueError:
                    pass
    state.rnti_n, state.ue_map_mtime = rnti_n, mtime
    print(f"[rts] {UE_MAP}: {len(rnti_n)} UEs", flush=True)


def read_core_events(state: AppStateVars):
    """Core events from scripts/core_ue_info.sh, the dashboard xApp's JSON format:
    {"context_type": "amf-ue", "event": "ran-ue-add"|"ran-ue-remove",
     "context": {"supi": "imsi-...", "ue_ip": ..., "ran_ue": {"ran_ue_ngap_id": .., "amf_ue_ngap_id": ..}}}"""
    if state.core_sock is None:
        return
    for _ in range(64):
        try:
            data = state.core_sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            print(f"[rts] core events: {e}", flush=True)
            return
        try:
            j = json.loads(data.decode())
            ctx = j.get("context", {})
            supi = ctx.get("supi") or ""
            imsi = supi[len("imsi-"):] if supi.startswith("imsi-") else supi
            if j.get("context_type") != "amf-ue" or not imsi[-9:].isdigit():
                continue
            n = int(imsi[-9:])
            if j.get("event") == "ran-ue-remove":
                state.core.pop(n, None)
                continue
            ran = ctx.get("ran_ue", {})
            state.core[n] = {"imsi": imsi, "ip": ctx.get("ue_ip") or "none",
                             "amf_ue_ngap_id": ran.get("amf_ue_ngap_id", "none"),
                             "ran_ue_ngap_id": ran.get("ran_ue_ngap_id", "none")}
            state.next_ids = 0.0
        except Exception as e:
            print(f"[rts] bad core event {data[:80]!r}: {e}", flush=True)


def publish_ids(state: AppStateVars):
    """rts_ue_ids for every UE in the latest bucket: n from ue_map.txt (du<index> without it), the core's view of
    UE n when it was sent."""
    ts_ns = time.time_ns()
    for du, rnti in sorted(state.ues.items()):
        n = state.rnti_n.get(rnti)
        core = state.core.get(n, {}) if n is not None else {}
        state.emit("rts_ue", {
            "n": n if n is not None else f"du{du}",
            "imsi": core.get("imsi", "none"),
            "ip": core.get("ip", "none"),
            "amf_ue_ngap_id": core.get("amf_ue_ngap_id", "none"),
            "ran_ue_ngap_id": core.get("ran_ue_ngap_id", "none"),
            "du": du,
            "rnti": f"0x{rnti:04x}",
        }, {"ids": 1}, ts_ns)


def tick(state: AppStateVars):
    now = time.monotonic()
    if now < state.next_flush:
        return
    state.next_flush = now + FLUSH_SECS
    read_core_events(state)
    if now >= state.next_ue_map:
        state.next_ue_map = now + UE_MAP_SECS
        read_ue_map(state)
    if now >= state.next_ids:
        state.next_ids = now + IDS_SECS
        publish_ids(state)
    state.flush()


def app_handler(timeout: bool, stream_idx: int, data_entry, state: AppStateVars):
    """A failing message logs a traceback instead of ending the xApp."""
    try:
        if not timeout and stream_idx == RTS_SIDX:
            handle_stats(state, ctypes.string_at(data_entry.data, STATS_SIZE))
        tick(state)
    except Exception:
        state.errors += 1
        if state.errors <= 5 or state.errors % 1000 == 0:
            print(f"[rts] error #{state.errors}:\n{traceback.format_exc()}", flush=True)


def jrtc_start_app(capsule):
    streams = [
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_ANY, JRTC_ROUTER_REQ_DEVICE_ID_ANY, STREAM_URI, STREAM_CHAN),
            True, None),
    ]
    app_cfg = JrtcAppCfg_t(
        b"dashboard_realtime_scheduling",
        100,
        len(streams),
        (JrtcStreamCfg_t * len(streams))(*streams),
        10.0,   # initialization timeout
        0.1,    # sleep between polls of the router
        1.0,    # with no data for 1 s, the handler also runs on timeouts (identities and flushes go on)
    )

    state = AppStateVars()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", CORE_PORT))
        s.setblocking(False)
        state.core_sock = s
    except OSError as e:
        print(f"[rts] no core events: cannot bind udp 127.0.0.1:{CORE_PORT}: {e}", flush=True)
    print(f"[rts] writing to {WRITE_URL}; UE names from {UE_MAP}; core events on udp 127.0.0.1:{CORE_PORT}",
          flush=True)

    state.app = jrtc_app_create(capsule, app_cfg, app_handler, state)
    jrtc_app_run(state.app)

    state.flush()
    state.vm.close()
    if state.core_sock is not None:
        state.core_sock.close()
    print(f"[rts] exiting: {state.buckets} buckets, {state.sent} VM lines, {state.failed} failed, "
          f"{state.errors} errors", flush=True)
    jrtc_app_destroy(state.app)
