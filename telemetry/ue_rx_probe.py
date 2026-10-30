#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
#
# ue_rx_probe - UDP as the UEs receive it, for the EdgeRIC real-time scheduling dashboard (uid rt-scheduling).
#
# tcp_probe.py sees TCP delivery at the UPF, from the ACKs. UDP has no ACKs, so what a UE received is only known at
# the UE. scripts/traffic_nue.sh runs one receiver per UE (udp_sink.pl with --plain, iperf3 -s otherwise), which logs
# one line per second to /tmp/ues/dl_ue<n>.log in the UE container. This script follows those logs (one kubectl exec
# running tail -F, nothing captured) and writes each UDP interval to VictoriaMetrics as
#
#   ue_rx_mbps{n}, ue_rx_bytes{n}, ue_rx_datagrams{n}
#
# stamped when the line arrives, the end of the second it covers. n is the UE number (ue<n>, IMSI ...n). TCP
# receivers are skipped: their delivery is tcp_probe.py's panel line.
#
# Runs on the host, like tcp_probe.py (the UE image has no python3).
#
#   python3 telemetry/ue_rx_probe.py [--vm http://localhost:30491] [--container durue1] [--ues 8]
#
# Stop with Ctrl-C.

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request

DEFAULT_VM = "http://localhost:30491"
PUSH_SECS = 0.5

# "[  1]    5.00-6.00    sec   1000000 Bytes   8.00 Mbits/sec   714 datagrams"          udp_sink.pl
# "[  5]   5.00-6.00   sec  1.19 MBytes  10.0 Mbits/sec  0.010 ms  0/862 (0%)"            iperf3 -s, UDP
# "[  6]   5.00-6.00   sec   384 KBytes  3.15 Mbits/sec"                                   iperf3 -s, TCP (skipped)
LINE = re.compile(r"^\[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+([\d.]+)\s+([KMG]?)Bytes\s+([\d.]+)\s+([KMG]?)bits/sec(.*)$")
FILE = re.compile(r"^==> /tmp/ues/dl_ue(\d+)\.log <==$")
BITS = {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}            # iperf3: decimal bit rates,
BYTES = {"": 1.0, "K": 2.0**10, "M": 2.0**20, "G": 2.0**30}   # binary byte counts


def parse(line):
    """(bytes, Mbit/s, datagrams) of a one-second UDP interval line, else None."""
    m = LINE.match(line)
    if not m or "sender" in line or "receiver" in line:
        return None
    t0, t1, nbytes, bunit, rate, runit, rest = m.groups()
    if float(t1) - float(t0) > 1.5:                       # an end-of-test summary
        return None
    dg = re.search(r"(\d+)\s+datagrams", rest)            # udp_sink.pl
    lost = re.search(r"ms\s+\d+/(\d+)", rest)             # iperf3 UDP: jitter, lost/total
    if not dg and not lost:
        return None                                       # TCP
    datagrams = int(dg.group(1)) if dg else int(lost.group(1))
    return float(nbytes) * BYTES[bunit], float(rate) * BITS[runit] / 1e6, datagrams


class Writer:
    def __init__(self, vm):
        self.url = f"{vm.rstrip('/')}/write"
        self.lines = []
        self.last_push = 0.0
        self.sent = 0
        self.failed = 0

    def add(self, n, nbytes, mbps, datagrams, ts_ns):
        self.lines.append(f"ue_rx,n={n} mbps={mbps:.4f},bytes={int(nbytes)}i,datagrams={datagrams}i {ts_ns}")

    def maybe_push(self, force=False):
        if not self.lines or (not force and time.monotonic() - self.last_push < PUSH_SECS):
            return
        self.last_push = time.monotonic()
        body, n, self.lines = "\n".join(self.lines).encode(), len(self.lines), []
        try:
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "text/plain"})
            urllib.request.urlopen(req, timeout=3).read()
            self.sent += n
        except Exception as e:
            self.failed += n
            if self.failed <= 3 or self.failed % 500 == 0:
                print(f"[ue_rx_probe] write failed ({self.failed} lines so far): {e}", file=sys.stderr, flush=True)


def follow(args, writer):
    files = " ".join(f"/tmp/ues/dl_ue{n}.log" for n in range(1, args.ues + 1))
    # -F follows each log by name: traffic_nue.sh deletes and recreates it on every start, and waits for missing ones.
    cmd = ["kubectl", "exec", "-n", args.namespace, args.pod, "-c", args.container, "--",
           "bash", "-c", f"exec tail -n 0 -F {files} 2>/dev/null"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    ue = None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            m = FILE.match(line)
            if m:
                ue = int(m.group(1))
                continue
            r = parse(line)
            if r is not None and ue is not None:
                writer.add(ue, *r, time.time_ns())
            writer.maybe_push()
    finally:
        writer.maybe_push(force=True)
        proc.terminate()


def main():
    ap = argparse.ArgumentParser(description="UDP received at the UEs (receiver logs) -> VictoriaMetrics")
    ap.add_argument("--vm", default=os.environ.get("UPT_VM_URL", DEFAULT_VM))
    ap.add_argument("--namespace", default="ran")
    ap.add_argument("--pod", default="srs-gnb-du1-0")
    ap.add_argument("--container", default="durue1")
    ap.add_argument("--ues", type=int, default=8, help="follow /tmp/ues/dl_ue1..N.log")
    args = ap.parse_args()
    writer = Writer(args.vm)
    print(f"[ue_rx_probe] following {args.pod}/{args.container}:/tmp/ues/dl_ue1..{args.ues}.log, "
          f"writing to {writer.url}", flush=True)
    while True:
        try:
            follow(args, writer)
        except KeyboardInterrupt:
            break
        print("[ue_rx_probe] kubectl exec ended; retrying in 5 s", file=sys.stderr, flush=True)
        time.sleep(5)
    print(f"[ue_rx_probe] wrote {writer.sent} lines, {writer.failed} failed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
