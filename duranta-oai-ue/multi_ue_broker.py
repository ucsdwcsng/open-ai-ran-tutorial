#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio ZMQ broker: one OCUDU gNB  <->  N OAI (Duranta) UEs, k3d netns variant.
#
# Port of ocudu-edgeric's broker/multi_ue_broker.py for the k3d pod: the broker runs in the pod
# netns (grbroker container), each UE in its own netns ue<i>, joined by a veth pair
# (pod side 10.201.<i>.1, UE side 10.201.<i>.2; see netns_setup_n.sh). The UEs are reached over
# tcp on those veths rather than ipc://, because ipc sockets live on a container's own
# filesystem and the broker and UEs are in different containers.
#
# Both OCUDU's and OAI's `zmq` radio drivers use the same REQ/REP scheme with complex float32
# samples on the wire: a TX port is a bound REP socket that hands out samples on request, an RX
# port is a connected REQ socket that pulls them.
#
#   downlink:  gNB tx --req_source--[throttle]--+--[DL gain]--rep_sink--> UE 1 rx
#                                               +--[DL gain]--rep_sink--> UE 2 rx
#                                               +-- ...
#
#   uplink:    UE 1 tx --req_source--[UL gain]--+
#              UE 2 tx --req_source--[UL gain]--+--(sum [+ gNB noise])--[throttle]--rep_sink--> gNB rx
#              ...                              +
#
# The per-UE mobility channel (path loss, shadowing, DL noise) runs inside each UE's zmq driver
# (--zmq.[0].chan); the broker only adds the cell-wide gNB receiver noise floor (--ul-noise-dbfs).
#
# Every UE the broker is started for must actually be running: the uplink sum waits on all UE
# inputs and the downlink fan-out stalls if one UE stops pulling samples.

import argparse
import signal
import sys
import threading
import time
import os

from gnuradio import analog, blocks, gr, zeromq


def db_to_amplitude(db):
    return 10.0 ** (db / 20.0)


class MultiUeBroker(gr.top_block):
    def __init__(self, args):
        gr.top_block.__init__(self, "OCUDU multi-UE ZMQ broker (k3d netns)")

        item = gr.sizeof_gr_complex
        n = args.num_ues
        timeout_ms = args.timeout_ms
        hwm = args.hwm

        # ---- downlink: gNB -> every UE --------------------------------------
        self.gnb_tx = zeromq.req_source(item, 1, args.gnb_tx, timeout_ms, False, hwm)
        dl_head = self.gnb_tx
        if args.throttle:
            self.dl_throttle = blocks.throttle(item, args.srate, True)
            self.connect(self.gnb_tx, self.dl_throttle)
            dl_head = self.dl_throttle

        self.dl_gain = []
        self.ue_rx = []
        # A 0 dB gain is left out: every GNU Radio block is one more thread and buffer copy in a
        # lock-step REQ/REP chain, so each one lowers how close to real time the chain can run.
        for i in range(1, n + 1):
            sink = zeromq.rep_sink(item, 1, args.ue_rx.format(i=i), timeout_ms, False, hwm)
            if args.dl_gain_db[i - 1] != 0.0:
                gain = blocks.multiply_const_cc(db_to_amplitude(args.dl_gain_db[i - 1]))
                self.connect(dl_head, gain, sink)
                self.dl_gain.append(gain)
            else:
                self.connect(dl_head, sink)
            self.ue_rx.append(sink)

        # ---- uplink: sum of every UE -> gNB ---------------------------------
        self.gnb_rx = zeromq.rep_sink(item, 1, args.gnb_rx, timeout_ms, False, hwm)
        ul_tail = self.gnb_rx
        if args.throttle and args.ul_throttle:
            self.ul_throttle = blocks.throttle(item, args.srate, True)
            self.connect(self.ul_throttle, self.gnb_rx)
            ul_tail = self.ul_throttle

        # The gNB receiver's noise floor: added once, after the UEs are summed, so each UE's UL SNR
        # follows its own path loss. Without it, attenuating a UE's UL changes nothing.
        noise = args.ul_noise_dbfs is not None
        if n > 1 or noise:
            self.ul_sum = blocks.add_vcc(1)
            self.connect(self.ul_sum, ul_tail)
        if noise:
            self.ul_noise = analog.fastnoise_source_c(analog.GR_GAUSSIAN,
                                                      10.0 ** (args.ul_noise_dbfs / 20.0), args.seed, 1 << 20)
            self.connect(self.ul_noise, (self.ul_sum, n))

        self.ue_tx = []
        self.ul_gain = []
        for i in range(1, n + 1):
            src = zeromq.req_source(item, 1, args.ue_tx.format(i=i), timeout_ms, False, hwm)
            out = src
            if args.ul_gain_db[i - 1] != 0.0:
                out = blocks.multiply_const_cc(db_to_amplitude(args.ul_gain_db[i - 1]))
                self.connect(src, out)
                self.ul_gain.append(out)
            self.connect(out, (self.ul_sum, i - 1) if (n > 1 or noise) else ul_tail)
            self.ue_tx.append(src)


def expand_gains(values, n, name):
    """Accept either one gain for all UEs or exactly one gain per UE."""
    if len(values) == 1:
        return values * n
    if len(values) != n:
        sys.exit(f"--{name} takes 1 value or {n} values (one per UE), got {len(values)}")
    return values


def parse_args():
    p = argparse.ArgumentParser(
        description="GNU Radio ZMQ broker connecting one OCUDU gNB to N OAI UEs (k3d netns).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-n", "--num-ues", type=int, required=True, help="number of UEs")
    p.add_argument("--gnb-tx", default="tcp://127.0.0.1:2101",
                   help="gNB TX port (ru_sdr tx_port); the broker connects here")
    p.add_argument("--gnb-rx", default="tcp://127.0.0.1:2100",
                   help="gNB RX port (ru_sdr rx_port); the broker binds here")
    p.add_argument("--ue-tx", default="tcp://10.201.{i}.2:2001",
                   help="UE i TX endpoint (UE binds, broker connects); {i} = 1..N")
    p.add_argument("--ue-rx", default="tcp://10.201.{i}.1:2000",
                   help="UE i RX endpoint (broker binds, UE connects); {i} = 1..N")
    p.add_argument("--srate", type=float, default=23.04e6,
                   help="sample rate in S/s (only used by --throttle)")
    p.add_argument("--dl-gain-db", type=float, nargs="+", default=[0.0],
                   help="downlink path gain in dB, one value or one per UE")
    p.add_argument("--ul-gain-db", type=float, nargs="+", default=[0.0],
                   help="uplink path gain in dB, one value or one per UE")
    p.add_argument("--throttle", action=argparse.BooleanOptionalAction, default=True,
                   help="pace both directions at --srate")
    # Off by default: the loop is closed (UEs only transmit once they have received DL), so DL pacing
    # already bounds it, and a second throttle with its own clock only adds stalls (4 UEs: +3-5% real time).
    p.add_argument("--ul-throttle", action=argparse.BooleanOptionalAction, default=False,
                   help="also pace the uplink")
    p.add_argument("--throttle-rearm-s", type=float, default=0.5,
                   help="re-arm the throttles this often (0 = never); see main()")
    p.add_argument("--ul-noise-dbfs", type=float, default=None,
                   help="gNB receiver noise floor in dBFS per sample, added once after the UL sum")
    p.add_argument("--seed", type=int, default=0, help="UL noise seed (0 = random)")
    p.add_argument("--timeout-ms", type=int, default=10000, help="ZMQ socket timeout")
    p.add_argument("--hwm", type=int, default=-1, help="ZMQ high-water mark (-1 = default)")
    args = p.parse_args()

    if args.num_ues < 1:
        sys.exit("--num-ues must be >= 1")
    args.dl_gain_db = expand_gains(args.dl_gain_db, args.num_ues, "dl-gain-db")
    args.ul_gain_db = expand_gains(args.ul_gain_db, args.num_ues, "ul-gain-db")
    return args


def main():
    args = parse_args()
    tb = MultiUeBroker(args)

    print(f"[broker] gNB tx {args.gnb_tx}  ->  {args.num_ues} UE(s)  ->  gNB rx {args.gnb_rx}", flush=True)
    if args.ul_noise_dbfs is not None:
        print(f"[broker] gNB receiver noise {args.ul_noise_dbfs:.1f} dBFS", flush=True)
    for i in range(1, args.num_ues + 1):
        print(f"[broker]   UE{i}: rx {args.ue_rx.format(i=i)}  tx {args.ue_tx.format(i=i)}"
              f"  (DL {args.dl_gain_db[i - 1]:+.1f} dB, UL {args.ul_gain_db[i - 1]:+.1f} dB)", flush=True)

    def stop(*_):
        # tb.wait() can block for the whole ZMQ timeout on a peer that is gone,
        # so bound the graceful stop and then leave regardless.
        threading.Timer(2.0, os._exit, args=(0,)).start()
        tb.stop()
        tb.wait()
        os._exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    tb.start()
    print("[broker] running (Ctrl-C to stop)", flush=True)

    # gr throttle paces against a clock that starts with the flowgraph and never caps its credit:
    # every second the gNB and UEs are not yet connected is later let through as fast as they can
    # go (~115% of real time here) until the backlog is spent, which takes minutes. set_sample_rate
    # restarts that clock, so re-arming it bounds any catch-up burst to one interval.
    throttles = [t for t in (getattr(tb, "dl_throttle", None), getattr(tb, "ul_throttle", None)) if t]
    if throttles and args.throttle_rearm_s > 0:
        def rearm():
            while True:
                time.sleep(args.throttle_rearm_s)
                for t in throttles:
                    t.set_sample_rate(args.srate)
        threading.Thread(target=rearm, daemon=True).start()
    signal.pause()


if __name__ == "__main__":
    main()
