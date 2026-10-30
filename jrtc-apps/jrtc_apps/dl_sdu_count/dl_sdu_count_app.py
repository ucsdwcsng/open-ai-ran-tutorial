# dl_sdu_count_app: print the per-UE downlink SDU counts that the dl_sdu_count codelet sends.

import ctypes
import os
import sys
from dataclasses import dataclass

sys.path.append(os.environ["JRTC_APP_PATH"])
from jrtc_app import *

from dl_sdu_count import struct__dl_sdu_count  # generated from dl_sdu_count.proto

PERIOD_S = (1 << 30) / 1e9  # the codelet's report period


@dataclass
class AppState:
    app: JrtcApp


def app_handler(timeout: bool, stream_idx: int, data_entry: struct_jrtc_router_data_entry, state: AppState):
    if timeout:
        return
    msg = ctypes.cast(data_entry.data, ctypes.POINTER(struct__dl_sdu_count)).contents
    mbps = msg.bytes * 8 / PERIOD_S / 1e6
    print(f"[dl_sdu_count] ue {msg.du_ue_index}: {msg.sdus} SDUs, {mbps:.1f} Mbit/s", flush=True)


def jrtc_start_app(capsule):
    streams = [
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(
                JRTC_ROUTER_REQ_DEST_ANY,
                JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                b"dl_sdu_count://jbpf_agent/dl_sdu_count/dl_sdu_count",  # deployment://jbpf_agent/codeletset/codelet
                b"out_counts"),                                          # the codelet's output channel
            True,   # receive
            None)
    ]
    cfg = JrtcAppCfg_t(
        b"dl_sdu_count",                              # context
        100,                                          # queue size
        len(streams),
        (JrtcStreamCfg_t * len(streams))(*streams),
        10.0,                                         # initialization timeout, s
        0.25,                                         # sleep timeout, s
        2.0)                                          # inactivity timeout, s
    state = AppState(app=None)
    state.app = jrtc_app_create(capsule, cfg, app_handler, state)
    jrtc_app_run(state.app)
    jrtc_app_destroy(state.app)
