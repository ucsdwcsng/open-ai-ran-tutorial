# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
#
# jrtc app that subscribes to the rlc_stats codeletset output channels
# (output_map_dl / output_map_ul) and prints per-radio-bearer RLC stats.
# Output appears in the jrtc container log:
#   kubectl logs -n ran jrtc-0 -c jrtc -f

import os
import sys
import ctypes
from dataclasses import dataclass

JRTC_APP_PATH = os.environ.get("JRTC_APP_PATH")
if JRTC_APP_PATH is None:
    raise ValueError("JRTC_APP_PATH not set")
sys.path.append(f"{JRTC_APP_PATH}")

import jrtc_app
from jrtc_app import *

from rlc_dl_stats import struct__rlc_dl_stats
from rlc_ul_stats import struct__rlc_ul_stats

DL_SIDX = 0
UL_SIDX = 1

# rlc_mode enum from the srsRAN jbpf defs
RLC_MODE = {0: "TM", 1: "UM", 2: "AM"}


@dataclass
class AppStateVars:
    app: JrtcApp


def _rb(s):
    """Human label for a radio bearer entry."""
    kind = "SRB" if s.is_srb else "DRB"
    return f"ue={s.du_ue_index} {kind}{s.rb_id} {RLC_MODE.get(s.rlc_mode, s.rlc_mode)}"


def _item(i):
    """t_*_stats_item -> compact 'count total min/max'."""
    return f"n={i.count} tot={i.total} min={i.min} max={i.max}"


def _traffic(t):
    """t_*_traffic_stats_item -> 'count total'."""
    return f"n={t.count} bytes={t.total}"


def print_dl(data):
    n = data.stats_count
    print(f"\n=== RLC DL stats  ts={data.timestamp}  entries={n} ===", flush=True)
    for k in range(n):
        s = data.stats[k]
        print(f"  [{_rb(s)}]", flush=True)
        print(f"      sdu_new      : {_traffic(s.sdu_new_bytes)}", flush=True)
        print(f"      pdu_tx       : {_traffic(s.pdu_tx_bytes)}", flush=True)
        print(f"      sdu_queue_pkt: {_item(s.sdu_queue_pkts)}", flush=True)
        print(f"      sdu_queue_byt: {_item(s.sdu_queue_bytes)}", flush=True)
        print(f"      tx_started   : {_item(s.sdu_tx_started)}", flush=True)
        print(f"      tx_completed : {_item(s.sdu_tx_completed)}", flush=True)
        print(f"      tx_delivered : {_item(s.sdu_tx_delivered)}", flush=True)
        if s.has_am:
            am = s.am
            print(f"      AM retx      : {_traffic(am.pdu_retx_bytes)}", flush=True)
            print(f"      AM status    : {_traffic(am.pdu_status_bytes)}", flush=True)
            print(f"      AM retx_cnt  : {_item(am.pdu_retx_count)}", flush=True)
            print(f"      AM window_pkt: {_item(am.pdu_window_pkts)}", flush=True)
            print(f"      AM window_byt: {_item(am.pdu_window_bytes)}", flush=True)


def print_ul(data):
    n = data.stats_count
    print(f"\n=== RLC UL stats  ts={data.timestamp}  entries={n} ===", flush=True)
    for k in range(n):
        s = data.stats[k]
        print(f"  [{_rb(s)}]", flush=True)
        print(f"      pdu          : {_traffic(s.pdu_bytes)}", flush=True)
        print(f"      sdu_delivered: {_traffic(s.sdu_delivered_bytes)}", flush=True)
        print(f"      sdu_latency  : {_item(s.sdu_delivered_latency)}", flush=True)
        if s.has_um:
            print(f"      UM window_pkt: {_item(s.um.pdu_window_pkts)}", flush=True)
        if s.has_am:
            print(f"      AM window_pkt: {_item(s.am.pdu_window_pkts)}", flush=True)


def app_handler(timeout: bool, stream_idx: int, data_entry: struct_jrtc_router_data_entry, state: AppStateVars):
    if timeout:
        return

    if stream_idx == DL_SIDX:
        ptr = ctypes.cast(data_entry.data, ctypes.POINTER(struct__rlc_dl_stats))
        print_dl(ptr.contents)

    elif stream_idx == UL_SIDX:
        ptr = ctypes.cast(data_entry.data, ctypes.POINTER(struct__rlc_ul_stats))
        print_ul(ptr.contents)


def jrtc_start_app(capsule):

    streams = [
        # DL_SIDX
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(
                JRTC_ROUTER_REQ_DEST_ANY,
                JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                b"rlc_stats://jbpf_agent/rlc_stats/rlc_collect",
                b"output_map_dl"),
            True,
            None
        ),
        # UL_SIDX
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(
                JRTC_ROUTER_REQ_DEST_ANY,
                JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                b"rlc_stats://jbpf_agent/rlc_stats/rlc_collect",
                b"output_map_ul"),
            True,
            None
        ),
    ]

    app_cfg = JrtcAppCfg_t(
        b"rlc_stats_app",                              # context
        100,                                           # q_size
        len(streams),                                  # num_streams
        (JrtcStreamCfg_t * len(streams))(*streams),    # streams
        10.0,                                          # initialization_timeout_secs
        0.25,                                          # sleep_timeout_secs
        3600.0                                         # inactivity_timeout_secs
    )

    state = AppStateVars(app=None)
    state.app = jrtc_app_create(capsule, app_cfg, app_handler, state)

    jrtc_app_run(state.app)

    jrtc_app_destroy(state.app)
