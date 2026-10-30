#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
#
# tcp_probe - bottom row of the user-plane dashboard.
#
# Streams pcap out of the UPF with tcpdump on ogstun (which carries the
# un-tunnelled per-UE IP traffic), reconstructs per-UE TCP state for the
# downlink flows, and writes throughput / RTT / bytes-in-flight to
# VictoriaMetrics.
#
# This is the libpcap method from the design deck: on a data segment record
# (seq_end -> send time); on an ACK, match the largest seq_end it covers to get
# an RTT sample, advance bytes_acked, and recompute in-flight.
#
# CRITICAL: it buckets on exactly the same grid as the RAN codelets
# (UPT_BUCKET_SHIFT = 27 -> 134.217728 ms) and stamps samples at the same
# absolute epoch-ns bucket-close time.  That is what makes the top and bottom
# dashboard rows line up sample-for-sample; do not "round to 100 ms" here.
#
# Runs on the host (the UPF image has tcpdump but no python3).
#
#   python3 telemetry/tcp_probe.py                       # auto-detect UE IPs
#   python3 telemetry/tcp_probe.py --map 10.45.0.4=0,10.45.0.5=1
#
# Stop with Ctrl-C.

import argparse
import os
import struct
import subprocess
import sys
import time
import urllib.request

# Must match UPT_BUCKET_SHIFT in jrtc-apps/codelets/upt/upt_helpers.h
UPT_BUCKET_SHIFT = 27
UPT_BUCKET_NS = 1 << UPT_BUCKET_SHIFT
UPT_BUCKET_SECS = UPT_BUCKET_NS / 1e9

# Push at least this often (the dashboard xApps flush every 0.5 s), not only every 50 lines.
PUSH_SECS = 0.5
# After a gap with no TCP packet at all, write zeros for at most this many buckets (10 s).
MAX_EMPTY_BUCKETS = 75


SERVER_IP = "10.45.0.1"          # iperf3 server / ogstun gateway
DEFAULT_VM = "http://localhost:30491"

TCP_PROTO = 6


def ip2str(b):
    return ".".join(str(x) for x in b)


def seq_lt(a, b):
    """Serial-number comparison (RFC 1982) for 32-bit TCP sequence space."""
    return ((a - b) & 0xFFFFFFFF) > 0x7FFFFFFF


def seq_diff(a, b):
    """a - b in sequence space, as a signed-ish forward distance."""
    d = (a - b) & 0xFFFFFFFF
    return d if d < 0x7FFFFFFF else 0


class FlowState:
    """Per (UE, 4-tuple) downlink TCP state."""

    __slots__ = ("sent", "highest_seq_end", "last_ack", "started")

    def __init__(self):
        self.sent = {}            # seq_end -> send timestamp (ns)
        self.highest_seq_end = None
        self.last_ack = None
        self.started = False


class UeBucket:
    __slots__ = ("bytes_acked", "rtt_sum", "rtt_n", "rtt_min", "rtt_max",
                 "inflight_last", "inflight_max", "dl_bytes", "retx")

    def __init__(self):
        self.bytes_acked = 0
        self.rtt_sum = 0.0
        self.rtt_n = 0
        self.rtt_min = None
        self.rtt_max = 0.0
        self.inflight_last = 0
        self.inflight_max = 0
        self.dl_bytes = 0
        self.retx = 0


class Probe:
    def __init__(self, ue_map, vm_url, verbose=False):
        self.ue_map = ue_map                      # ip -> ue index
        self.write_url = f"{vm_url}/write"
        self.flows = {}                           # key -> FlowState
        self.buckets = {}                         # ue -> UeBucket
        self.cur_bucket = None
        self.lines = []
        self.sent_ok = 0
        self.sent_fail = 0
        self.verbose = verbose
        self.last_push = 0.0

    # ---------- output ----------

    def _bucket(self, ue):
        b = self.buckets.get(ue)
        if b is None:
            b = UeBucket()
            self.buckets[ue] = b
        return b

    def flush_bucket(self, bucket_id):
        ts_ns = int((bucket_id + 1) * UPT_BUCKET_NS)
        for ip, ue in self.ue_map.items():
            b = self.buckets.get(ue)
            if b is None:
                # No packet of this UE in the bucket: it got 0. Without the sample, averages and sums over the
                # bucket grid skip it (or carry the previous rate on), which overstates bursty schedules.
                self.lines.append(f"tcp,ue={ue},ip={ip} throughput_mbps=0,bytes_acked=0i,dl_bytes=0i,retx=0i {ts_ns}")
                continue
            mbps = (b.bytes_acked * 8.0) / UPT_BUCKET_SECS / 1e6
            fields = [
                f"throughput_mbps={mbps:.4f}",
                f"bytes_acked={b.bytes_acked}i",
                f"dl_bytes={b.dl_bytes}i",
                f"bytes_in_flight={b.inflight_last}i",
                f"bytes_in_flight_max={b.inflight_max}i",
                f"retx={b.retx}i",
            ]
            if b.rtt_n:
                fields += [
                    f"rtt_ms={(b.rtt_sum / b.rtt_n):.4f}",
                    f"rtt_min_ms={b.rtt_min:.4f}",
                    f"rtt_max_ms={b.rtt_max:.4f}",
                    f"rtt_samples={b.rtt_n}i",
                ]
            self.lines.append(f"tcp,ue={ue},ip={ip} {','.join(fields)} {ts_ns}")
        self.buckets.clear()
        if len(self.lines) >= 50 or time.monotonic() - self.last_push >= PUSH_SECS:
            self.push()

    def push(self):
        self.last_push = time.monotonic()
        if not self.lines:
            return
        body = "\n".join(self.lines).encode()
        n = len(self.lines)
        self.lines = []
        try:
            req = urllib.request.Request(self.write_url, data=body,
                                         headers={"Content-Type": "text/plain"})
            urllib.request.urlopen(req, timeout=3).read()
            self.sent_ok += n
        except Exception as e:
            self.sent_fail += n
            if self.sent_fail <= 3:
                print(f"[tcp_probe] write failed: {e}", file=sys.stderr, flush=True)

    # ---------- packet handling ----------

    def handle(self, ts_ns, ip_pkt):
        if len(ip_pkt) < 20:
            return
        vihl = ip_pkt[0]
        if (vihl >> 4) != 4:
            return                                  # IPv4 only
        ihl = (vihl & 0x0F) * 4
        total_len = struct.unpack("!H", ip_pkt[2:4])[0]
        proto = ip_pkt[9]
        if proto != TCP_PROTO or len(ip_pkt) < ihl + 20:
            return
        src = ip2str(ip_pkt[12:16])
        dst = ip2str(ip_pkt[16:20])

        tcp = ip_pkt[ihl:]
        sport, dport = struct.unpack("!HH", tcp[0:4])
        seq, ack = struct.unpack("!II", tcp[4:12])
        doff = (tcp[12] >> 4) * 4
        flags = tcp[13]
        payload = max(0, total_len - ihl - doff)

        # bucket rollover on the shared grid
        bucket = ts_ns >> UPT_BUCKET_SHIFT
        if self.cur_bucket is None:
            self.cur_bucket = bucket
        elif bucket != self.cur_bucket:
            self.flush_bucket(self.cur_bucket)
            # buckets without a single packet in between: zeros for every UE, up to MAX_EMPTY_BUCKETS of them
            for empty in range(max(self.cur_bucket + 1, bucket - MAX_EMPTY_BUCKETS), bucket):
                self.flush_bucket(empty)
            self.cur_bucket = bucket

        # downlink data: server -> UE
        if src == SERVER_IP and dst in self.ue_map:
            ue = self.ue_map[dst]
            key = (dst, dport, sport)
            f = self.flows.setdefault(key, FlowState())
            b = self._bucket(ue)
            if payload > 0:
                seq_end = (seq + payload) & 0xFFFFFFFF
                b.dl_bytes += payload
                if f.highest_seq_end is not None and seq_lt(seq_end, f.highest_seq_end):
                    b.retx += 1            # already-sent range => retransmission
                else:
                    f.highest_seq_end = seq_end
                # keep first send time for this range (Karn: skip retx for RTT)
                f.sent.setdefault(seq_end, ts_ns)
                if len(f.sent) > 20000:
                    f.sent.clear()

        # uplink ACK: UE -> server
        elif dst == SERVER_IP and src in self.ue_map:
            ue = self.ue_map[src]
            key = (src, sport, dport)
            f = self.flows.get(key)
            if f is None or not (flags & 0x10):     # need ACK flag
                return
            b = self._bucket(ue)

            if f.last_ack is None:
                f.last_ack = ack
            else:
                adv = seq_diff(ack, f.last_ack)
                if adv > 0:
                    b.bytes_acked += adv
                    f.last_ack = ack

            # RTT: newest send whose seq_end this ACK covers
            best_ts, covered = None, []
            for seq_end, sent_ts in f.sent.items():
                if not seq_lt(ack, seq_end):        # ack >= seq_end
                    covered.append(seq_end)
                    if best_ts is None or sent_ts > best_ts:
                        best_ts = sent_ts
            for s in covered:
                del f.sent[s]
            if best_ts is not None:
                rtt_ms = (ts_ns - best_ts) / 1e6
                if 0 <= rtt_ms < 60000:
                    b.rtt_sum += rtt_ms
                    b.rtt_n += 1
                    b.rtt_max = max(b.rtt_max, rtt_ms)
                    b.rtt_min = rtt_ms if b.rtt_min is None else min(b.rtt_min, rtt_ms)

            if f.highest_seq_end is not None and f.last_ack is not None:
                inflight = seq_diff(f.highest_seq_end, f.last_ack)
                b.inflight_last = inflight
                b.inflight_max = max(b.inflight_max, inflight)


def pcap_stream(fh, probe):
    gh = fh.read(24)
    if len(gh) < 24:
        raise RuntimeError("short pcap global header")
    magic = gh[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        endian, nano = "<", False
    elif magic == b"\xa1\xb2\xc3\xd4":
        endian, nano = ">", False
    elif magic == b"\x4d\x3c\xb2\xa1":
        endian, nano = "<", True
    elif magic == b"\xa1\xb2\x3c\x4d":
        endian, nano = ">", True
    else:
        raise RuntimeError(f"not a pcap stream (magic {magic!r})")
    linktype = struct.unpack(endian + "I", gh[20:24])[0]

    # ogstun is a TUN device -> raw IP, no link-layer header
    if linktype in (12, 14, 101):
        l2 = 0
    elif linktype == 1:
        l2 = 14                                   # Ethernet
    elif linktype == 113:
        l2 = 16                                   # Linux cooked
    else:
        l2 = 0
    print(f"[tcp_probe] pcap linktype={linktype} l2_offset={l2}", flush=True)

    hdr = struct.Struct(endian + "IIII")
    while True:
        ph = fh.read(16)
        if len(ph) < 16:
            break
        ts_sec, ts_frac, incl, _orig = hdr.unpack(ph)
        data = fh.read(incl)
        if len(data) < incl:
            break
        ts_ns = ts_sec * 1_000_000_000 + (ts_frac if nano else ts_frac * 1000)
        probe.handle(ts_ns, data[l2:])


def autodetect_ue_map():
    """ue1 -> index 0, ue2 -> index 1, matching srsUE start order (du_ue_index)."""
    env = os.environ.copy()
    m = {}
    for idx, ns in enumerate(("ue1", "ue2")):
        try:
            out = subprocess.run(
                ["kubectl", "exec", "-n", "ran", "srs-gnb-du1-0", "-c", "gnb", "--",
                 "ip", "netns", "exec", ns, "ip", "-4", "-o", "addr", "show", "tun_srsue"],
                capture_output=True, text=True, timeout=30, env=env).stdout
            for tok in out.split():
                if tok.startswith("10.45."):
                    m[tok.split("/")[0]] = idx
                    break
        except Exception as e:
            print(f"[tcp_probe] autodetect {ns} failed: {e}", file=sys.stderr)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", help="ip=ue,ip=ue (default: autodetect from ue1/ue2 netns)")
    ap.add_argument("--vm", default=os.environ.get("UPT_VM_URL", DEFAULT_VM))
    ap.add_argument("--iface", default="ogstun")
    # The open5gs-upf container runs as uid 999 with an empty effective capability
    # set, so tcpdump there fails with "You don't have permission to capture on
    # that device".  Capture instead from a privileged ephemeral container sharing
    # the UPF network namespace:
    #   kubectl debug -n open5gs <upf> --image=nicolaka/netshoot --profile=sysadmin \
    #       -c tcpdumper --target=open5gs-upf -- sleep infinity
    ap.add_argument("--container", default="tcpdumper")
    args = ap.parse_args()

    if args.map:
        ue_map = {}
        for part in args.map.split(","):
            ip, ue = part.split("=")
            ue_map[ip.strip()] = int(ue)
    else:
        ue_map = autodetect_ue_map()

    if not ue_map:
        print("[tcp_probe] no UE IPs found - are the UEs attached?", file=sys.stderr)
        return 1
    print(f"[tcp_probe] UE map: {ue_map}", flush=True)
    print(f"[tcp_probe] writing to {args.vm}/write", flush=True)

    upf = subprocess.run(
        ["bash", "-c",
         "kubectl get pods -n open5gs --no-headers | awk '/upf/{print $1}' | head -1"],
        capture_output=True, text=True).stdout.strip()
    if not upf:
        print("[tcp_probe] UPF pod not found", file=sys.stderr)
        return 1
    print(f"[tcp_probe] capturing on {upf}:{args.iface}", flush=True)

    cmd = ["kubectl", "exec", "-n", "open5gs", upf, "-c", args.container, "--",
           "tcpdump", "-i", args.iface, "-U", "-s", "128", "-w", "-", "tcp"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    probe = Probe(ue_map, args.vm)
    try:
        pcap_stream(proc.stdout, probe)
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        print(f"[tcp_probe] {e}\n[tcp_probe] tcpdump said: {err}", file=sys.stderr)
    finally:
        if probe.cur_bucket is not None:
            probe.flush_bucket(probe.cur_bucket)
        probe.push()
        proc.terminate()
        print(f"[tcp_probe] wrote {probe.sent_ok} lines, {probe.sent_fail} failed",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
