#!/usr/bin/env python3
# GNU Radio ZMQ broker for srsRAN gNB <-> 2x srsUE (10 MHz / base_srate 11.52e6).
#   gNB  : tx_port=2101  rx_port=2100
#   UE1  : tx_port=2001  rx_port=2000
#   UE2  : tx_port=2011  rx_port=2010
# UL: UE1(2001)+UE2(2011) -> add -> throttle -> gNB rx (2100)
# DL: gNB tx (2101) -> throttle -> UE1 rx (2000) and UE2 rx (2010)
from gnuradio import blocks, gr, zeromq
import signal, sys


class top_block(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "2UE ZMQ Broker")
        samp_rate = 23.04e6
        self.src_gnb = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://localhost:2101", 100, False, -1)
        self.src_ue1 = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://localhost:2001", 100, False, -1)
        self.src_ue2 = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://localhost:2011", 100, False, -1)
        self.snk_ue1 = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://*:2000", 100, False, -1)
        self.snk_ue2 = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://*:2010", 100, False, -1)
        self.snk_gnb = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://*:2100", 100, False, -1)
        self.thr_gnb = blocks.throttle(gr.sizeof_gr_complex, samp_rate, True)
        self.thr_ue1 = blocks.throttle(gr.sizeof_gr_complex, samp_rate, True)
        self.thr_ue2 = blocks.throttle(gr.sizeof_gr_complex, samp_rate, True)
        self.adder = blocks.add_vcc(1)
        self.connect((self.adder, 0), (self.thr_gnb, 0)); self.connect((self.thr_gnb, 0), (self.snk_gnb, 0))
        self.connect((self.src_ue1, 0), (self.adder, 0)); self.connect((self.src_ue2, 0), (self.adder, 1))
        self.connect((self.src_gnb, 0), (self.thr_ue1, 0)); self.connect((self.thr_ue1, 0), (self.snk_ue1, 0))
        self.connect((self.src_gnb, 0), (self.thr_ue2, 0)); self.connect((self.thr_ue2, 0), (self.snk_ue2, 0))


tb = top_block()


def _stop(*_):
    tb.stop(); tb.wait(); sys.exit(0)


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)
tb.start()
print("broker running", flush=True)
tb.wait()
