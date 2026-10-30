#!/usr/bin/env python3
# 2-UE ZMQ broker, netns variant: gNB on localhost (pod netns); each UE reached via its veth IP.
# Derived from broker4_netns.py, trimmed to 2 UEs so the adder does not starve waiting
# on absent UE3/UE4 streams. Ports match oaiue{1,2}_netns.conf.
from gnuradio import blocks, gr, zeromq
import signal, sys
# (ue_tx_port, ue_rx_port, ue_netns_ip)
UES = [(2001, 2000, "10.201.1.2"), (2011, 2010, "10.201.2.2")]


class top_block(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "2UE ZMQ Broker (netns)")
        sr = 23.04e6
        self.src_gnb = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://localhost:2101", 100, False, -1)
        self.snk_gnb = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://*:2100", 100, False, -1)
        self.thr_gnb = blocks.throttle(gr.sizeof_gr_complex, sr, True)
        self.adder = blocks.add_vcc(1)
        self.src_ue = []
        for i, (tx, _rx, ip) in enumerate(UES):
            s = zeromq.req_source(gr.sizeof_gr_complex, 1, f"tcp://{ip}:{tx}", 100, False, -1)
            self.src_ue.append(s)
            self.connect((s, 0), (self.adder, i))
        self.connect((self.adder, 0), (self.thr_gnb, 0))
        self.connect((self.thr_gnb, 0), (self.snk_gnb, 0))
        self.thr_ue = []
        self.snk_ue = []
        for (_tx, rx, _ip) in UES:
            t = blocks.throttle(gr.sizeof_gr_complex, sr, True)
            k = zeromq.rep_sink(gr.sizeof_gr_complex, 1, f"tcp://*:{rx}", 100, False, -1)
            self.thr_ue.append(t)
            self.snk_ue.append(k)
            self.connect((self.src_gnb, 0), (t, 0))
            self.connect((t, 0), (k, 0))


tb = top_block()


def _stop(*_):
    tb.stop()
    tb.wait()
    sys.exit(0)


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)
tb.start()
print("broker running (2 UE netns)", flush=True)
tb.wait()
