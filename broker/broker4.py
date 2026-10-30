#!/usr/bin/env python3
# GNU Radio ZMQ broker for srsRAN gNB <-> 4x srsUE (20 MHz / base_srate 23.04e6).
#   gNB : tx=2101 rx=2100 | UE1: tx2001/rx2000  UE2: tx2011/rx2010
#                          | UE3: tx2021/rx2020  UE4: tx2031/rx2030
# UL: sum(UE1..4 tx) -> throttle -> gNB rx (2100)
# DL: gNB tx (2101) -> throttle -> each UE rx
from gnuradio import blocks, gr, zeromq
import signal, sys

UES = [(2001, 2000), (2011, 2010), (2021, 2020), (2031, 2030)]  # (ue_tx->our src, ue_rx->our sink)


class top_block(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "4UE ZMQ Broker")
        sr = 23.04e6
        self.src_gnb = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://localhost:2101", 100, False, -1)
        self.snk_gnb = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://*:2100", 100, False, -1)
        self.thr_gnb = blocks.throttle(gr.sizeof_gr_complex, sr, True)
        self.adder = blocks.add_vcc(1)
        # UL: each UE tx -> adder -> throttle -> gNB rx
        self.src_ue = []
        for i, (tx, _rx) in enumerate(UES):
            s = zeromq.req_source(gr.sizeof_gr_complex, 1, f"tcp://localhost:{tx}", 100, False, -1)
            self.src_ue.append(s)
            self.connect((s, 0), (self.adder, i))
        self.connect((self.adder, 0), (self.thr_gnb, 0)); self.connect((self.thr_gnb, 0), (self.snk_gnb, 0))
        # DL: gNB tx -> per-UE throttle -> UE rx
        self.thr_ue = []; self.snk_ue = []
        for (_tx, rx) in UES:
            t = blocks.throttle(gr.sizeof_gr_complex, sr, True)
            k = zeromq.rep_sink(gr.sizeof_gr_complex, 1, f"tcp://*:{rx}", 100, False, -1)
            self.thr_ue.append(t); self.snk_ue.append(k)
            self.connect((self.src_gnb, 0), (t, 0)); self.connect((t, 0), (k, 0))


tb = top_block()
def _stop(*_): tb.stop(); tb.wait(); sys.exit(0)
signal.signal(signal.SIGINT, _stop); signal.signal(signal.SIGTERM, _stop)
tb.start(); print("broker running (4 UE)", flush=True); tb.wait()
