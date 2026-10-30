#!/usr/bin/env python3
# TCP forwarder for jrtc-ctl.
#
# jrtc-ctl hardcodes 127.0.0.1:30450 for the jbpf device, but in k3d the gNB
# agent is only reachable through a service name. This listens on that fixed
# local address inside the jrtc pod and forwards to the proxy service.
# Staged into jrtc-0 and started by scripts/setup_zmq_chan_demo.sh (and edgeric_rt.sh,
# dashboard_realtime_scheduling.sh, setup_ota.sh) when it is not already running.
#
# Kept byte-identical to the version used for every verified bring-up; the
# variant in ocudu-jbpf/deploy/fwd.py differs and is NOT interchangeable.
import socket, threading
TARGET = ("srs-gnb-du1-proxy.ran.svc.cluster.local", 30450)
def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except Exception: pass
    finally:
        for s in (a, b):
            try: s.close()
            except Exception: pass
def handle(c):
    try: u = socket.create_connection(TARGET, timeout=10)
    except Exception:
        c.close(); return
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
L = socket.socket(); L.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
L.bind(("127.0.0.1", 30450)); L.listen(128)
print("fwd 127.0.0.1:30450 ->", TARGET, flush=True)
while True:
    c, _ = L.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
