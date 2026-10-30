# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
"""EdgeRIC-RT wire format, Python side.

Mirrors jrtc-apps/codelets/edgeric_rt/rt_wire.h:
  - the per-TTI report the rt_report codelet emits (nanopb rt_report, 1080 bytes),
  - the 168-byte action the bridge writes into the rt_ctrl codelet's rt_act_in channel.

Standard library only, so the jrtc bridge xApp and the muApps in the edgeric container share it.
"""

import struct

MAX_UES = 16

# Slot status of the applied action (report act_status).
STATUS_NONE = 0  # no action for this TTI: stock scheduler
STATUS_HIT = 1  # action tagged with exactly this RANtime
STATUS_HOLD = 2  # last HIT re-applied within its hold budget

# Action flags.
FLAG_WORK_CONSERVING = 0x1
FLAG_GREEDY_PDCCH = 0x2

# Per-UE outcome flags (report ue flags).
UE_FALLBACK = 0x01
UE_CANDIDATE = 0x02
UE_SELECTED = 0x04
UE_PDCCH_FAIL = 0x08
UE_UCI_FAIL = 0x10
UE_IN_ACTION = 0x20
UE_NO_QUOTA = 0x40
UE_OVER_PDCCH = 0x80

ACTION_MAGIC = 0xE5C1
ACTION_VER = 1

# ---- Report: 4 x u64, 21 x u32, u16 ue_count, u16 pad, then 16 x rt_ue (15 x u32) ----
REPORT_HDR = struct.Struct("<4Q21IHH")
UE_REC = struct.Struct("<15I")
REPORT_SIZE = REPORT_HDR.size + MAX_UES * UE_REC.size
assert REPORT_HDR.size == 120 and REPORT_SIZE == 1080

REPORT_FIELDS = (
    "t_slot_ns", "t_report_ns", "act_t_src_report_ns", "act_t_sent_ns",
    "ran_tti", "sfn", "slot", "is_dl", "dl_mask", "dl_rbs_avail", "dl_rbs_used", "n_weighted", "n_sel",
    "act_status", "act_seq", "act_tti", "act_src_tti", "act_flags",
    "n_rx", "n_late", "n_early", "n_overwritten", "n_bad", "n_hit", "cell_id",
    "ue_count", "pad0",
)
UE_FIELDS = (
    "du_ue_index", "rnti", "cqi", "eff_cqi_x100", "olla_mcs", "dl_pending_bytes", "free_harqs",
    "dl_prbs", "newtx_tbs", "retx_tbs", "dl_mcs", "drb_bytes", "rb_quota", "weight", "flags",
)
assert len(REPORT_FIELDS) == 27 and len(UE_FIELDS) == 15

# Byte offset of every header field.
REPORT_OFFSETS = {}
_off = 0
for _i, _f in enumerate(REPORT_FIELDS):
    REPORT_OFFSETS[_f] = _off
    _off += 8 if _i < 4 else (2 if _f in ("ue_count", "pad0") else 4)
assert _off == REPORT_HDR.size

RAN_TTI_OFFSET = REPORT_OFFSETS["ran_tti"]
UE_COUNT_OFFSET = REPORT_OFFSETS["ue_count"]
UE_OFFSET = REPORT_HDR.size
assert (RAN_TTI_OFFSET, UE_COUNT_OFFSET, UE_OFFSET) == (32, 116, 120)

# ---- Action: magic u16, ver u8, n u8, seq, tti, src_tti, flags, hold (u32), t_src_report_ns, t_sent_ns (u64),
# then 16 x {weight u32, du_ue_index u16, flags u8, reserved u8} ----
ACTION_HDR = struct.Struct("<HBBIIIIIQQ")
ACTION_UE = struct.Struct("<IHBB")
ACTION_SIZE = ACTION_HDR.size + MAX_UES * ACTION_UE.size
assert ACTION_HDR.size == 40 and ACTION_SIZE == 168
ACTION_TTI_OFFSET = 8
ACTION_T_SENT_OFFSET = 32

_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")


def tti_diff(a, b):
    """Signed difference a - b of two 32-bit RANtimes (wrap safe)."""
    d = (a - b) & 0xFFFFFFFF
    return d - 0x100000000 if d >= 0x80000000 else d


def report_ran_tti(buf):
    return _U32.unpack_from(buf, RAN_TTI_OFFSET)[0]


def decode_report(buf):
    """Report bytes -> dict of header fields plus 'ues': a list of per-UE dicts (ue_count entries)."""
    if len(buf) < REPORT_SIZE:
        raise ValueError(f"report is {len(buf)} bytes, expected {REPORT_SIZE}")
    rep = dict(zip(REPORT_FIELDS, REPORT_HDR.unpack_from(buf, 0)))
    n = min(rep["ue_count"], MAX_UES)
    rep["ues"] = [dict(zip(UE_FIELDS, UE_REC.unpack_from(buf, UE_OFFSET + i * UE_REC.size))) for i in range(n)]
    return rep


def next_dl_tti(ran_tti, dl_mask, lead=1):
    """RANtime of the first DL slot at least `lead` slots after ran_tti, from the report's dl_mask (None if none)."""
    for k in range(max(lead, 0), 32):
        if dl_mask & (1 << k):
            return (ran_tti + k) & 0xFFFFFFFF
    return None


def encode_action(seq, tti, src_tti, weights, flags=0, hold=0, t_src_report_ns=0, t_sent_ns=0):
    """Action bytes. weights: iterable of (du_ue_index, weight) with integer weights (0 = not scheduled)."""
    w = [(int(u), int(x)) for u, x in weights][:MAX_UES]
    buf = bytearray(ACTION_SIZE)
    ACTION_HDR.pack_into(buf, 0, ACTION_MAGIC, ACTION_VER, len(w), seq & 0xFFFFFFFF, tti & 0xFFFFFFFF,
                         src_tti & 0xFFFFFFFF, flags, hold, t_src_report_ns, t_sent_ns)
    for i, (ue, x) in enumerate(w):
        ACTION_UE.pack_into(buf, ACTION_HDR.size + i * ACTION_UE.size, max(0, min(x, 0xFFFFFFFF)), ue, 0, 0)
    return bytes(buf)


def decode_action(buf):
    if len(buf) != ACTION_SIZE:
        raise ValueError(f"action is {len(buf)} bytes, expected {ACTION_SIZE}")
    magic, ver, n, seq, tti, src_tti, flags, hold, t_src, t_sent = ACTION_HDR.unpack_from(buf, 0)
    ues = [ACTION_UE.unpack_from(buf, ACTION_HDR.size + i * ACTION_UE.size)[:2] for i in range(min(n, MAX_UES))]
    return dict(magic=magic, ver=ver, n=n, seq=seq, tti=tti, src_tti=src_tti, flags=flags, hold=hold,
                t_src_report_ns=t_src, t_sent_ns=t_sent, weights=[(u, w) for w, u in ues])


def stamp_sent(buf, t_ns):
    """Copy of an action with t_sent_ns set."""
    b = bytearray(buf)
    _U64.pack_into(b, ACTION_T_SENT_OFFSET, t_ns)
    return bytes(b)
