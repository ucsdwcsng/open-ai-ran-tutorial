# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
#
# bufctl_app - xApp for manual RLC DL buffer-size control.
#
# It is a relay, not a decision maker (that is the point of the split: the codelet
# executes, the xApp decides, and here "decides" means "passes on what the operator
# typed"). Later algorithms replace the file interface with their own logic.
#
#   bufctl_cli.py (host terminal)          this xApp (jrtc pod)           gNB
#   ----------------------------           --------------------           ---
#   append a line to commands.jsonl  -->   read new lines, send      -->  rlc_ctrl
#   read state.json / events.jsonl   <--   write reports back        <--  reports
#
# Files live in /apps/bufctl, which is the host directory
# jrtc-apps/jrtc_apps/bufctl mounted into the jrtc pod. They are a transport, not a
# database: state.json is a snapshot of the last report per bearer and can be deleted
# at any time.
#
# NOTE: no background threads. jrtc runs each app in a Python sub-interpreter and
# calls Py_EndInterpreter on unload, which aborts the process if another thread in
# that interpreter is still alive. Everything happens in app_handler.

import os
import sys
import json
import time
import struct
import ctypes
from dataclasses import dataclass, field

JRTC_APP_PATH = os.environ.get("JRTC_APP_PATH")
if JRTC_APP_PATH is None:
    raise ValueError("JRTC_APP_PATH not set")
sys.path.append(f"{JRTC_APP_PATH}")

import jrtc_app
from jrtc_app import *

from rlc_ctrl_report import struct__rlc_ctrl_report

# ---- streams ----
REPORT_SIDX = 0
CTRL_SIDX = 1

# URI = <deployment name>://jbpf_agent/<codeletset_id>/<codelet_name>
REPORT_URI = b"bufctl://jbpf_agent/bufctl/rlc_ctrl"
REPORT_CHAN = b"out_rlc_ctrl"
CTRL_URI = b"bufctl://jbpf_agent/bufctl/rlc_ctrl"
CTRL_CHAN = b"ctrl_in"

# ---- shared files (hostPath mount) ----
BASE = os.environ.get("BUFCTL_DIR", "/apps/bufctl")
CMD_FILE = os.path.join(BASE, "commands.jsonl")
STATE_FILE = os.path.join(BASE, "state.json")
EVENT_FILE = os.path.join(BASE, "events.jsonl")

# ---- ops: must match rlc_ctrl.cpp ----
OP_SET = 0
OP_RESET = 1
OP_GET = 2
REASON = {0: "changed", 1: "get", 2: "reset"}

POLL_SECS = 0.2


@dataclass
class AppState:
    app: object = None
    offset: int = 0                       # bytes of CMD_FILE already consumed
    cmd_sig: tuple = ()                   # (inode, size, mtime_ns) when we last read CMD_FILE
    bearers: dict = field(default_factory=dict)   # (ue, drb) -> last report
    sent: int = 0
    failed: int = 0
    reports: int = 0
    last_poll: float = 0.0
    dirty: bool = False


def _pack(ue, op, drb, byte_limit, sdu_limit):
    # struct rlc_ctrl_msg { uint32 du_ue_index, op, rb_id, byte_limit, sdu_limit; } -> 20 bytes
    return struct.pack("<IIIII", int(ue), int(op), int(drb), int(byte_limit), int(sdu_limit))


def _send(state, ue, op, drb, byte_limit=0, sdu_limit=0):
    msg = _pack(ue, op, drb, byte_limit, sdu_limit)
    res = jrtc_app_router_channel_send_input_msg(state.app, CTRL_SIDX, msg, len(msg))
    if res == 0:
        state.sent += 1
    else:
        state.failed += 1
    return res


def _append_event(obj):
    """Append one JSON line for the CLI. Best effort: never let logging break the app."""
    try:
        with open(EVENT_FILE, "a") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception as e:
        print(f"[bufctl] cannot write {EVENT_FILE}: {e}", flush=True)


def _write_state(state):
    """Snapshot the last report per bearer, atomically (tmp + rename)."""
    try:
        rows = []
        for (ue, drb), r in sorted(state.bearers.items()):
            row = {"ue": ue, "drb": drb}
            row.update(r)
            rows.append(row)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"updated": time.time(), "bearers": rows,
                       "sent": state.sent, "failed": state.failed,
                       "reports": state.reports}, f, indent=1)
        os.replace(tmp, STATE_FILE)
        state.dirty = False
    except Exception as e:
        print(f"[bufctl] cannot write {STATE_FILE}: {e}", flush=True)


def _handle_command(state, cmd):
    """One command from the CLI: {"op": "set"|"get"|"reset", "ue": N, "drb": N, ...}."""
    op_name = str(cmd.get("op", "")).lower()
    ue = int(cmd.get("ue", -1))
    drb = int(cmd.get("drb", -1))
    if ue < 0 or drb < 1:
        _append_event({"t": time.time(), "event": "rejected", "reason": "bad ue/drb", "cmd": cmd})
        return

    if op_name == "set":
        b = int(cmd.get("bytes", 0) or 0)
        s = int(cmd.get("sdus", 0) or 0)
        if b == 0 and s == 0:
            _append_event({"t": time.time(), "event": "rejected",
                           "reason": "set with no limits", "cmd": cmd})
            return
        res = _send(state, ue, OP_SET, drb, b, s)
        ev = {"t": time.time(), "event": "sent", "op": "set", "ue": ue, "drb": drb,
              "bytes": b, "sdus": s, "res": res}
    elif op_name == "reset":
        res = _send(state, ue, OP_RESET, drb)
        ev = {"t": time.time(), "event": "sent", "op": "reset", "ue": ue, "drb": drb, "res": res}
    elif op_name == "get":
        res = _send(state, ue, OP_GET, drb)
        ev = {"t": time.time(), "event": "sent", "op": "get", "ue": ue, "drb": drb, "res": res}
    else:
        _append_event({"t": time.time(), "event": "rejected", "reason": "unknown op", "cmd": cmd})
        return

    if res != 0:
        # Non-zero means the control channel is not there (codelet unloaded) or full.
        ev["error"] = "send failed: codelet not loaded, or ctrl_in full"
    _append_event(ev)
    print(f"[bufctl] {ev}", flush=True)


def _poll_commands(state, now):
    """Read any new lines appended to CMD_FILE since the last poll."""
    if now - state.last_poll < POLL_SECS:
        return
    state.last_poll = now
    try:
        st = os.stat(CMD_FILE)
        size = st.st_size
    except Exception:
        return                      # no file yet: nothing to do
    # A rewrite can land on exactly the byte count we have already consumed
    # (truncate + append of the same length), and ": > file" keeps the inode, so
    # size alone cannot tell "nothing new" from "replaced". Compare the full
    # signature: if the file changed while size <= offset, re-read from the start.
    sig = (st.st_ino, size, st.st_mtime_ns)
    if size < state.offset:
        state.offset = 0            # file was truncated or replaced
    elif size == state.offset and state.cmd_sig and sig != state.cmd_sig:
        print("[bufctl] %s was rewritten to the same length; re-reading from 0"
              % CMD_FILE, flush=True)
        state.offset = 0
    if size == state.offset:
        state.cmd_sig = sig
        return
    try:
        with open(CMD_FILE) as f:
            f.seek(state.offset)
            lines = f.readlines()
            state.offset = f.tell()
        state.cmd_sig = (st.st_ino, size, st.st_mtime_ns)
    except Exception as e:
        print(f"[bufctl] cannot read {CMD_FILE}: {e}", flush=True)
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception:
            _append_event({"t": time.time(), "event": "rejected",
                           "reason": "malformed line", "line": line[:200]})
            continue
        _handle_command(state, cmd)


def _handle_report(state, rep):
    ue = int(rep.du_ue_index)
    drb = int(rep.rb_id)
    row = {
        "cur_bytes": int(rep.cur_byte_limit),
        "cur_sdus": int(rep.cur_sdu_limit),
        "cfg_bytes": int(rep.cfg_byte_limit),
        "cfg_sdus": int(rep.cfg_sdu_limit),
        "reason": REASON.get(int(rep.reason), int(rep.reason)),
        "t": time.time(),
    }
    state.bearers[(ue, drb)] = row
    state.reports += 1
    state.dirty = True
    ev = {"t": row["t"], "event": "report", "ue": ue, "drb": drb}
    ev.update({k: row[k] for k in ("cur_bytes", "cur_sdus", "cfg_bytes", "cfg_sdus", "reason")})
    _append_event(ev)
    print(f"[bufctl] report ue={ue} drb={drb} cur={row['cur_bytes']}B/{row['cur_sdus']}SDU "
          f"cfg={row['cfg_bytes']}B/{row['cfg_sdus']}SDU ({row['reason']})", flush=True)


def app_handler(timeout, stream_idx, data_entry, state):
    now = time.time()
    if not timeout and stream_idx == REPORT_SIDX:
        _handle_report(state, ctypes.cast(
            data_entry.data, ctypes.POINTER(struct__rlc_ctrl_report)).contents)
    # Poll for new CLI commands on every wake-up (timeout tick or report).
    _poll_commands(state, now)
    if state.dirty:
        _write_state(state)


def jrtc_start_app(capsule):
    streams = [
        # RX: reports from the codelet (wildcard dest, like other telemetry streams)
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_ANY, JRTC_ROUTER_REQ_DEVICE_ID_ANY,
                              REPORT_URI, REPORT_CHAN),
            True, None),
        # TX: commands into the codelet (CONCRETE device -- required for input channels)
        JrtcStreamCfg_t(
            JrtcStreamIdCfg_t(JRTC_ROUTER_REQ_DEST_NONE, 1,
                              CTRL_URI, CTRL_CHAN),
            False, None),
    ]

    app_cfg = JrtcAppCfg_t(
        b"bufctl_app",
        100,                     # q_size
        len(streams),
        (JrtcStreamCfg_t * len(streams))(*streams),
        10.0,                    # initialization_timeout_secs (wait for channels)
        0.2,                     # sleep_timeout_secs
        0.2,                     # inactivity_timeout_secs -> timeout tick polls the command file
    )

    state = AppState()
    try:
        os.makedirs(BASE, exist_ok=True)
        # Start from the end of any existing command file: old commands are not replayed.
        if os.path.exists(CMD_FILE):
            _st = os.stat(CMD_FILE)
            state.offset = _st.st_size
            state.cmd_sig = (_st.st_ino, _st.st_size, _st.st_mtime_ns)
    except Exception as e:
        print(f"[bufctl] setup warning: {e}", flush=True)

    print(f"[bufctl] up. commands={CMD_FILE} state={STATE_FILE}", flush=True)
    _append_event({"t": time.time(), "event": "app_start"})

    state.app = jrtc_app_create(capsule, app_cfg, app_handler, state)
    jrtc_app_run(state.app)

    _write_state(state)
    _append_event({"t": time.time(), "event": "app_stop",
                   "sent": state.sent, "failed": state.failed, "reports": state.reports})
    print(f"[bufctl] exiting: sent={state.sent} failed={state.failed} "
          f"reports={state.reports}", flush=True)
