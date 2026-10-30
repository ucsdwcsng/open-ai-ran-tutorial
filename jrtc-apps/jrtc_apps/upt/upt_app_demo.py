# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
#
# upt_app - consumes the three user-plane telemetry streams and writes them to
# VictoriaMetrics using InfluxDB line protocol.  Grafana then queries VM with
# PromQL for the top three dashboard panels:
#
#   GTP Arrival Rate      <- upt_gtp_arrival_mbps
#   RLC Queuing Latency   <- upt_rlc_queue_latency_ms
#   RLC Buffer Occupancy  <- upt_rlc_buffer_bytes
#
# Each codelet already aggregated per-packet events into fixed buckets, so this
# app does no windowing of its own -- it just converts a bucket to a sample and
# stamps it at the bucket's closing time, which keeps the RAN series time-aligned
# with the TCP series collected separately from tcpdump.

import os
import sys
import json
import time
import struct
import ctypes
import urllib.request
from dataclasses import dataclass, field

JRTC_APP_PATH = os.environ.get("JRTC_APP_PATH")
if JRTC_APP_PATH is None:
    raise ValueError("JRTC_APP_PATH not set")
sys.path.append(f"{JRTC_APP_PATH}")

import jrtc_app
from jrtc_app import *

from gtp_arrival_stats import struct__gtp_arrival_stats
from rlc_queue_stats import struct__rlc_queue_stats
from rlc_buffer_stats import struct__rlc_buffer_stats
from rlc_pkt_records import struct__rlc_pkt_records

# Must match UPT_BUCKET_SHIFT in codelets/upt/upt_helpers.h
UPT_BUCKET_SHIFT = 27
UPT_BUCKET_NS = 1 << UPT_BUCKET_SHIFT          # 134.217728 ms
UPT_BUCKET_SECS = UPT_BUCKET_NS / 1e9

VM_URL = os.environ.get("UPT_VM_URL", "http://victoria-metrics.ran.svc.cluster.local:8428")
WRITE_URL = f"{VM_URL}/write"

# DB tier: raw per-packet records appended as newline-delimited JSON (one object
# per batch) for long-term storage / offline TCP-segment joins / model training.
# The xApp keeps only a short in-memory window for fast percentile export; this
# file is the archive. Ship/rotate it out of band.
PKT_LOG = os.environ.get("UPT_PKT_LOG", "/tmp/upt_pkt_records.ndjson")
PCTL_WINDOW = 2048            # recent per-UE latencies kept for percentile export

GTP_SIDX = 0
RLCQ_SIDX = 1
RLCB_SIDX = 2
RLCPKT_SIDX = 3




# NOTE: no background writer thread on purpose.
# jrtc runs each app in a Python sub-interpreter and calls Py_EndInterpreter on
# unload, which aborts the whole process ("Fatal Python error: Py_EndInterpreter:
# not the last thread", SIGSEGV) if any other thread in that interpreter is still
# alive -- a daemon thread parked in queue.get() is enough to kill jrtc.
# Because the codelets already bucket per ~134 ms, this app only sees ~22
# messages/s in total, so buffering and flushing inline is cheap and keeps the
# app single-threaded.

FLUSH_MAX_LINES = 200


@dataclass
class AppStateVars:
    app: JrtcApp = None
    lines: list = field(default_factory=list)
    sent: int = 0
    failed: int = 0
    # per-packet
    pkt_total: int = 0
    pkt_batches: int = 0
    pkt_fh: object = None
    lat_window: dict = field(default_factory=dict)   # ue -> list[latency_ms] (rolling)


def _bearer(is_srb, rb_id):
    return f"{'SRB' if is_srb else 'DRB'}{rb_id}"


def _flush(state: AppStateVars):
    if not state.lines:
        return
    body = "\n".join(state.lines).encode()
    n = len(state.lines)
    state.lines.clear()
    try:
        req = urllib.request.Request(
            WRITE_URL, data=body, headers={"Content-Type": "text/plain"}
        )
        urllib.request.urlopen(req, timeout=2).read()
        state.sent += n
    except Exception as e:
        state.failed += n
        if state.failed <= 5 or state.failed % 200 == 0:
            print(f"[upt] VM write failed ({state.failed} lines total): {e}", flush=True)


def _emit(state, measurement, tags, fields, ts_ns):
    tagstr = ",".join(f"{k}={v}" for k, v in tags.items())
    fieldstr = ",".join(f"{k}={v}" for k, v in fields.items())
    state.lines.append(f"{measurement},{tagstr} {fieldstr} {ts_ns}")
    if len(state.lines) >= FLUSH_MAX_LINES:
        _flush(state)


def handle_gtp(state, data):
    # bucket close time; bucket_id counts UPT_BUCKET_NS units since epoch
    ts_ns = int((data.bucket_id + 1) * UPT_BUCKET_NS)
    for i in range(data.stats_count):
        s = data.stats[i]
        mbps = (s.byte_count * 8.0) / UPT_BUCKET_SECS / 1e6
        pps = s.pkt_count / UPT_BUCKET_SECS
        _emit(
            state,
            "upt_gtp_arrival",
            {"ue": s.cu_ue_index, "bearer": _bearer(s.is_srb, s.rb_id)},
            {
                "mbps": round(mbps, 4),
                "pps": round(pps, 2),
                "bytes": s.byte_count,
                "pkts": s.pkt_count,
            },
            ts_ns,
        )


def handle_rlc_queue(state, data):
    ts_ns = int((data.bucket_id + 1) * UPT_BUCKET_NS)
    for i in range(data.stats_count):
        s = data.stats[i]
        if s.count == 0:
            continue
        avg_ms = (s.latency_sum_ns / s.count) / 1e6
        _emit(
            state,
            "upt_rlc_queue",
            {"ue": s.du_ue_index, "bearer": _bearer(s.is_srb, s.rb_id)},
            {
                "latency_ms": round(avg_ms, 4),
                "latency_min_ms": round(s.latency_min_ns / 1e6, 4),
                "latency_max_ms": round(s.latency_max_ns / 1e6, 4),
                "sdus": s.count,
                "retx": s.retx_count,
            },
            ts_ns,
        )


def handle_rlc_buffer(state, data):
    ts_ns = int((data.bucket_id + 1) * UPT_BUCKET_NS)
    for i in range(data.stats_count):
        s = data.stats[i]
        avg_bytes = (s.queue_bytes_sum / s.samples) if s.samples else 0
        _emit(
            state,
            "upt_rlc_buffer",
            {"ue": s.du_ue_index, "bearer": _bearer(s.is_srb, s.rb_id)},
            {
                "bytes": s.queue_bytes_last,
                "bytes_max": s.queue_bytes_max,
                "bytes_avg": round(avg_bytes, 1),
                "pkts": s.queue_pkts_last,
                "pkts_max": s.queue_pkts_max,
                "enq_pkts": s.enq_pkts,
                "deq_pkts": s.deq_pkts,
            },
            ts_ns,
        )


def _pctl(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[i]


def handle_rlc_pkts(state, data):
    """Lossless per-packet records: archive raw, keep a rolling window, export
    percentiles. This is the sensor stream for the buffer control loop."""
    n = data.pkts_count
    if n <= 0:
        return
    ts_ns = int(data.timestamp)

    # 1. archive raw (DB tier) -- one JSON object per batch
    recs = []
    for k in range(n):
        p = data.pkts[k]
        recs.append({
            "ue": p.du_ue_index, "srb": p.is_srb, "rb": p.rb_id,
            "sn": p.pdcp_sn, "lat_ns": p.latency_ns,
            "retx": p.is_retx, "qbytes": p.queue_bytes,
        })
        w = state.lat_window.setdefault(p.du_ue_index, [])
        w.append(p.latency_ns / 1e6)
    state.pkt_total += n
    state.pkt_batches += 1
    if state.pkt_fh is not None:
        try:
            state.pkt_fh.write(json.dumps({"ts_ns": ts_ns, "n": n, "pkts": recs}) + "\n")
        except Exception:
            pass

    # 2. percentile export per UE from the rolling window (control-facing)
    for ue, w in state.lat_window.items():
        if len(w) > PCTL_WINDOW:
            del w[:-PCTL_WINDOW]
        s = sorted(w)
        _emit(state, "upt_rlc_pkt",
              {"ue": ue},
              {"lat_p50_ms": round(_pctl(s, 0.50), 4),
               "lat_p95_ms": round(_pctl(s, 0.95), 4),
               "lat_p99_ms": round(_pctl(s, 0.99), 4),
               "lat_max_ms": round(s[-1], 4),
               "pkts_total": state.pkt_total},
              ts_ns)


def app_handler(timeout: bool, stream_idx: int, data_entry, state: AppStateVars):
    if timeout:
        # fires every sleep_timeout_secs (0.25 s) -- flush partial batches
        _flush(state)
        if state.pkt_fh is not None:
            try:
                state.pkt_fh.flush()
            except Exception:
                pass
        return
    if stream_idx == RLCPKT_SIDX:
        handle_rlc_pkts(state, ctypes.cast(
            data_entry.data, ctypes.POINTER(struct__rlc_pkt_records)).contents)
        return
    if stream_idx == GTP_SIDX:
        handle_gtp(state, ctypes.cast(data_entry.data,
                                      ctypes.POINTER(struct__gtp_arrival_stats)).contents)
    elif stream_idx == RLCQ_SIDX:
        handle_rlc_queue(state, ctypes.cast(data_entry.data,
                                            ctypes.POINTER(struct__rlc_queue_stats)).contents)
    elif stream_idx == RLCB_SIDX:
        handle_rlc_buffer(state, ctypes.cast(data_entry.data,
                                             ctypes.POINTER(struct__rlc_buffer_stats)).contents)


def jrtc_start_app(capsule):
    streams = [
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_ANY, JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                              b"upt://jbpf_agent/upt/gtp_arrival", b"out_gtp_arrival"),
            True, None),
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_ANY, JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                              b"upt://jbpf_agent/upt/rlc_queueing", b"out_rlc_queue"),
            True, None),
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_ANY, JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                              b"upt://jbpf_agent/upt/rlc_buffer_enq", b"out_rlc_buffer"),
            True, None),
        # RLCPKT_SIDX (3) -- lossless per-packet records
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_ANY, JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                              b"upt://jbpf_agent/upt/rlc_pkt_record", b"out_rlc_pkts"),
            True, None),
        # NOTE: no input (is_rx=False) stream. Open-loop control lives in the
        # rlc_ctrl codelet (time-based schedule); the xApp only observes and
        # annotates it. The jrtc input-channel path is deferred to the closed-loop
        # phase (its init gate never satisfies for a gNB-side control channel).
    ]

    app_cfg = JrtcAppCfg_t(
        b"upt_app",
        100,
        len(streams),
        (JrtcStreamCfg_t * len(streams))(*streams),
        10.0,
        0.25,
        3600.0,
    )

    state = AppStateVars()
    try:
        state.pkt_fh = open(PKT_LOG, "a")
        print(f"[upt] per-packet archive -> {PKT_LOG}", flush=True)
    except Exception as e:
        print(f"[upt] could not open {PKT_LOG}: {e}", flush=True)
    print(f"[upt] writing to {WRITE_URL}", flush=True)

    state.app = jrtc_app_create(capsule, app_cfg, app_handler, state)
    jrtc_app_run(state.app)

    _flush(state)
    if state.pkt_fh is not None:
        try:
            state.pkt_fh.flush()
            state.pkt_fh.close()
        except Exception:
            pass
    print(f"[upt] exiting: {state.sent} VM lines, {state.failed} failed; "
          f"{state.pkt_total} pkts in {state.pkt_batches} batches", flush=True)
    jrtc_app_destroy(state.app)
