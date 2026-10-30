# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
#
# rt_bridge_app - EdgeRIC-RT bridge (jrtc xApp): the RT-E2 transport between the gNB codelets and the muApps.
#
#   Reports: rt_report codelet --jrtc--> this app --Unix datagram--> muApp     (one report per TTI)
#   Actions: muApp --Unix datagram--> this app --> rt_ctrl codelet (rt_act_in) (one action per target TTI)
#
# It runs its own loop instead of JrtcApp.run(): it never sleeps, so a report leaves within microseconds and an
# action reaches the codelet as soon as the muApp sends it. The TTI coupling itself lives at the two ends: the
# codelet applies an action only in the exact RANtime it targets, and the muApp tracks RICtime (Lazy RAN /
# Lazy RIC). The bridge forwards every report (the muApp keeps all of them for rewards and acts only on the
# newest) and drops an action whose TTI it has already seen reported, since the slot is gone.
#
# Sockets live in <run_dir> = /apps/edgeric_rt/run (the edgeric container mounts the same host directory):
#   bridge.sock  bound here, actions in
#   muapp.sock   bound by the muApp, reports out
#
# Settings: /apps/edgeric_rt/bridge.conf (key = value), all optional:
#   inline   off | equal | maxweight | fixed:w0,w1,...   act inside the bridge, without a muApp, to measure the
#                                                        bare jbpf/jrtc loop (weights by du_ue_index order)
#   lead     1        target = first DL slot at least `lead` TTIs after the report (inline mode)
#   cpu      -        CPU list to pin the bridge thread to, e.g. 30 or 28-29
#   fifo     0        SCHED_FIFO priority for the bridge thread (0 = leave as is)
#   stats_s  5        period of the counter log line

import ctypes
import gc
import os
import socket
import struct
import sys
import time

JRTC_APP_PATH = os.environ.get("JRTC_APP_PATH")
if JRTC_APP_PATH is None:
    raise ValueError("JRTC_APP_PATH not set")
sys.path.append(f"{JRTC_APP_PATH}")

import jrtc_app  # noqa: E402
from jrtc_app import *  # noqa: E402,F401,F403
import jrtc_bindings  # noqa: E402
import jrtc_router_lib  # noqa: E402
from jrtc_wrapper_utils import get_ctx_from_capsule, get_data_entry_array_ptr  # noqa: E402

APP_DIR = os.path.join(os.environ.get("JRTC_APPS", "/apps"), "edgeric_rt")
sys.path.insert(0, APP_DIR)
import rt_wire as W  # noqa: E402

REPORT_SIDX = 0
CTRL_SIDX = 1
# URI = <deployment name>://jbpf_agent/<codeletset_id>/<codelet_name>
REPORT_URI = b"edgeric_rt://jbpf_agent/edgeric_rt/rt_report"
REPORT_CHAN = b"out_rt_report"
CTRL_URI = b"edgeric_rt://jbpf_agent/edgeric_rt/rt_ctrl"
CTRL_CHAN = b"rt_act_in"

CONF = os.path.join(APP_DIR, "bridge.conf")
LOG = "/tmp/edgeric_rt_bridge.log"
Q_SIZE = 256

_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
OFF_IS_DL = W.REPORT_OFFSETS["is_dl"]
OFF_DL_MASK = W.REPORT_OFFSETS["dl_mask"]
OFF_ACT_STATUS = W.REPORT_OFFSETS["act_status"]
OFF_ACT_T_SRC = W.REPORT_OFFSETS["act_t_src_report_ns"]
OFF_T_REPORT = W.REPORT_OFFSETS["t_report_ns"]


def _read_conf():
    conf = {"inline": "off", "lead": "1", "cpu": "", "fifo": "0", "stats_s": "5"}
    try:
        with open(CONF) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = (x.strip() for x in line.split("=", 1))
                    conf[k] = v
    except OSError:
        pass
    return conf


def _cpus(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


class Stats:
    def __init__(self):
        self.reports = 0
        self.fwd = 0
        self.drop_full = 0
        self.drop_nomuapp = 0
        self.act_in = 0
        self.act_out = 0
        self.act_late = 0
        self.act_bad = 0
        self.act_fail = 0
        self.last_report_tti = None
        # Inline mode: outcome of the DL slots, as the reports echo it.
        self.dl_slots = 0
        self.dl_hit = 0
        self.lat_us = []


class St:
    def __init__(self):
        self.app = None
        self.fh = None
        self.conf = _read_conf()
        self.stats = Stats()


def _log(state, line):
    print(line, flush=True)
    if state.fh:
        state.fh.write(line + "\n")
        state.fh.flush()


def _inline_weights(policy, rep_buf):
    """Weights [(du_ue_index, w)] for the inline policies, from a raw report."""
    n = min(_U32.unpack_from(rep_buf, W.UE_COUNT_OFFSET)[0] & 0xFFFF, W.MAX_UES)
    ues = [W.UE_REC.unpack_from(rep_buf, W.UE_OFFSET + i * W.UE_REC.size) for i in range(n)]
    if policy == "equal":
        return [(u[0], 1) for u in ues]
    if policy == "maxweight":
        # w = CQI x backlog, scaled into 32 bits.
        return [(u[0], max(1, min((u[2] * u[5]) >> 4, 0xFFFFFFFF))) for u in ues if u[5] > 0]
    if policy.startswith("fixed:"):
        vals = [float(x) for x in policy.split(":", 1)[1].split(",")]
        order = sorted(u[0] for u in ues)
        return [(ue, int(1000 * vals[i])) for i, ue in enumerate(order) if i < len(vals) and vals[i] > 0]
    return []


def bridge_loop(app, state):
    st = state.stats
    conf = state.conf

    # Real-time placement of this app thread (the jrtc pod has CAP_SYS_NICE).
    if conf["cpu"]:
        try:
            os.sched_setaffinity(0, _cpus(conf["cpu"]))
        except (OSError, ValueError) as e:
            _log(state, f"[rt_bridge] cannot pin to cpu {conf['cpu']}: {e}")
    if int(conf["fifo"]) > 0:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(int(conf["fifo"])))
        except OSError as e:
            _log(state, f"[rt_bridge] cannot set SCHED_FIFO {conf['fifo']}: {e}")

    # ctypes entry points, bound once (the stock wrappers re-set argtypes on every call).
    lib = jrtc_router_lib.jrtc_router_lib
    recv = lib.jrtc_router_receive
    recv.argtypes = [ctypes.POINTER(jrtc_bindings.struct_dapp_router_ctx),
                     ctypes.POINTER(jrtc_bindings.struct_jrtc_router_data_entry), ctypes.c_size_t]
    recv.restype = ctypes.c_int
    release = lib.jrtc_router_channel_release_buf
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    send_in = lib.jrtc_router_channel_send_input_msg
    send_in.argtypes = [jrtc_bindings.struct_jrtc_router_stream_id, ctypes.c_void_p, ctypes.c_size_t]
    send_in.restype = ctypes.c_int

    dapp_ctx = app.data.env_ctx.dapp_ctx
    entries = get_data_entry_array_ptr(Q_SIZE)
    ctrl_sid = app.stream_items[CTRL_SIDX].sid

    rep_buf = bytearray(W.REPORT_SIZE)
    rep_addr = ctypes.addressof((ctypes.c_char * W.REPORT_SIZE).from_buffer(rep_buf))
    act_buf = bytearray(W.ACTION_SIZE + 64)
    act_addr = ctypes.addressof((ctypes.c_char * len(act_buf)).from_buffer(act_buf))

    run_dir = os.path.join(APP_DIR, "run")
    os.makedirs(run_dir, exist_ok=True)
    bridge_path = os.path.join(run_dir, "bridge.sock")
    muapp_path = os.path.join(run_dir, "muapp.sock")
    try:
        os.unlink(bridge_path)
    except FileNotFoundError:
        pass
    rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    rx.bind(bridge_path)
    os.chmod(bridge_path, 0o666)
    rx.setblocking(False)
    tx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    tx.setblocking(False)
    connected = False
    next_connect = 0.0

    inline = conf["inline"].strip()
    inline = "" if inline in ("", "off", "none") else inline
    lead = int(conf["lead"])
    seq = 0
    stats_s = float(conf["stats_s"])
    next_stats = time.monotonic() + stats_s
    _log(state, f"[rt_bridge] running: inline={inline or 'off'} lead={lead} run_dir={run_dir}")

    gc.collect()
    gc.freeze()
    gc.disable()
    exit_flag = app.data.env_ctx
    while not exit_flag.app_exit:
        now = time.monotonic()

        # Reports: forward every one, newest last.
        n = recv(dapp_ctx, entries, Q_SIZE)
        got = 0
        for i in range(n):
            d = entries[i].data
            if not d:
                continue
            ctypes.memmove(rep_addr, d, W.REPORT_SIZE)
            release(d)
            got += 1
            st.reports += 1
            tti = _U32.unpack_from(rep_buf, W.RAN_TTI_OFFSET)[0]
            st.last_report_tti = tti
            if inline:
                # The report echoes the outcome of this slot: the measure of the bare loop.
                if _U32.unpack_from(rep_buf, OFF_IS_DL)[0]:
                    st.dl_slots += 1
                    if _U32.unpack_from(rep_buf, OFF_ACT_STATUS)[0] == W.STATUS_HIT:
                        st.dl_hit += 1
                        t_src, t_sent = struct.unpack_from("<QQ", rep_buf, OFF_ACT_T_SRC)
                        if t_src and len(st.lat_us) < 20000:
                            st.lat_us.append((t_sent - t_src) / 1000.0)
                continue
            if not connected and now >= next_connect:
                next_connect = now + 0.5
                try:
                    tx.connect(muapp_path)
                    connected = True
                except OSError:
                    pass
            if not connected:
                st.drop_nomuapp += 1
                continue
            try:
                tx.send(rep_buf)
                st.fwd += 1
            except BlockingIOError:
                st.drop_full += 1
            except OSError:
                connected = False
                st.drop_nomuapp += 1

        # Inline mode: act on the newest report only (Lazy RIC: older ones are already history).
        if inline and got:
            ran_tti = st.last_report_tti
            dl_mask = _U32.unpack_from(rep_buf, OFF_DL_MASK)[0]
            target = W.next_dl_tti(ran_tti, dl_mask, lead)
            weights = _inline_weights(inline, rep_buf)
            if target is not None and weights:
                seq += 1
                t_rep = _U64.unpack_from(rep_buf, OFF_T_REPORT)[0]
                msg = W.encode_action(seq, target, ran_tti, weights, t_src_report_ns=t_rep,
                                      t_sent_ns=time.monotonic_ns())
                act_buf[: W.ACTION_SIZE] = msg
                if send_in(ctrl_sid, act_addr, W.ACTION_SIZE) == 0:
                    st.act_out += 1
                else:
                    st.act_fail += 1

        # Actions from the muApp.
        while True:
            try:
                nbytes = rx.recv_into(act_buf)
            except BlockingIOError:
                break
            except OSError:
                break
            st.act_in += 1
            if nbytes != W.ACTION_SIZE:
                st.act_bad += 1
                continue
            tti = _U32.unpack_from(act_buf, W.ACTION_TTI_OFFSET)[0]
            if st.last_report_tti is not None and W.tti_diff(tti, st.last_report_tti) <= 0:
                st.act_late += 1  # its slot is already over: Lazy RIC
                continue
            _U64.pack_into(act_buf, W.ACTION_T_SENT_OFFSET, time.monotonic_ns())
            if send_in(ctrl_sid, act_addr, W.ACTION_SIZE) == 0:
                st.act_out += 1
            else:
                st.act_fail += 1

        if now >= next_stats:
            next_stats = now + stats_s
            line = (f"[rt_bridge] tti={st.last_report_tti} reports={st.reports} fwd={st.fwd} "
                    f"drop_full={st.drop_full} no_muapp={st.drop_nomuapp} act_in={st.act_in} act_out={st.act_out} "
                    f"late={st.act_late} bad={st.act_bad} fail={st.act_fail}")
            if inline and st.dl_slots:
                lat = sorted(st.lat_us)
                p = (lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]) if lat else (lambda q: float("nan"))
                line += (f" | inline {inline}: DL HIT {100.0 * st.dl_hit / st.dl_slots:.1f}% of {st.dl_slots}"
                         f" report->send p50={p(0.5):.0f}us p99={p(0.99):.0f}us")
                st.dl_slots = st.dl_hit = 0
                st.lat_us.clear()
            _log(state, line)

    rx.close()
    tx.close()
    gc.enable()


class RtBridge(jrtc_app.JrtcApp):
    def run(self):
        if self.init() != 0:
            _log(self.data.app_state, "[rt_bridge] jrtc init failed (codeletset edgeric_rt loaded?)")
            return
        bridge_loop(self, self.data.app_state)


def jrtc_start_app(capsule):
    streams = [
        # RX: per-TTI reports (wildcard destination, like the other telemetry streams).
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_ANY, JRTC_ROUTER_REQ_DEVICE_ID_ANY, REPORT_URI, REPORT_CHAN),
            True, None),
        # TX: actions into rt_ctrl's input channel (a concrete device: required for input channels).
        JrtcStreamCfg_t(JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_NONE, 1, CTRL_URI, CTRL_CHAN), False, None),
    ]
    app_cfg = JrtcAppCfg_t(
        b"rt_bridge",
        Q_SIZE,
        len(streams),
        (JrtcStreamCfg_t * len(streams))(*streams),
        30.0,  # initialization_timeout_secs
        0.0,  # sleep_timeout_secs (unused: own loop)
        0.0,  # inactivity_timeout_secs (unused: own loop)
    )

    state = St()
    try:
        state.fh = open(LOG, "a")
    except OSError as e:
        print(f"[rt_bridge] cannot open {LOG}: {e}", flush=True)
    _log(state, f"[rt_bridge] starting, conf={state.conf}")

    state.app = RtBridge(get_ctx_from_capsule(capsule), app_cfg, None, state)
    state.app.run()

    s = state.stats
    _log(state, f"[rt_bridge] exiting: reports={s.reports} fwd={s.fwd} act_out={s.act_out} late={s.act_late}")
    state.app.cleanup()
    if state.fh:
        state.fh.close()
