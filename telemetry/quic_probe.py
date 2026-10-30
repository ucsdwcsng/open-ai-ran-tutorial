#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
#
# quic_probe.py -- QUIC transport probe, the QUIC analog of tcp_probe.py.
#
# WHY THIS IS DIFFERENT FROM tcp_probe.py
# ---------------------------------------
# tcp_probe.py reconstructs RTT / in-flight / retx *passively* from a pcap,
# because TCP headers (seq, ack, timestamps) are on the wire in the clear.
# QUIC puts all of that INSIDE the encrypted packet, so an on-path capture of
# the downlink UDP flow can see byte volume and timing but NOT the transport's
# RTT, cwnd, or loss. The authoritative source for those is the endpoint.
#
# picoquic (the CC-over-QUIC transport the campaign uses) can emit a qlog
# (`-q <dir>`) whose `recovery/metrics_updated` events carry, over time:
#     smoothed_rtt, min_rtt, latest_rtt   (us)   <- QUIC's own RTT estimate
#     cwnd, bytes_in_flight               (bytes)
#     pacing_rate                         (bits/s)
# and `recovery/packet_lost` events give loss. So this probe RUNS a picoquic
# download with qlog enabled, then parses the qlog and pushes a per-event time
# series to VictoriaMetrics -- the same role tcp_probe plays for TCP.
#
# It writes:
#     quic,ue=N,cc=CC smoothed_rtt_ms=,min_rtt_ms=,latest_rtt_ms=,
#                     cwnd_bytes=,bytes_in_flight=,pacing_mbps=,lost_total= <ts_ns>
# with ts_ns = (qlog reference_time + event delta) so it lines up on the same
# epoch clock as the RAN codelet metrics (upt_rlc_queue_latency_ms, etc).
#
# Modes:
#   (default) wrap : launch picoquicdemo -q on the UE(s), wait, parse, push.
#   --parse-only   : parse a qlog dir already present in the pod (no traffic).
#
# Example:
#   python3 quic_probe.py --ue both --cc bbr --seconds 60
#   python3 quic_probe.py --ue ue1 --cc cubic --bytes 50000000

import argparse, json, os, re, subprocess, sys, time, urllib.request

NS = "ran"; POD = "srs-gnb-du1-0"; GNB = "gnb"
SERVER = "10.45.0.1"; PORT = "4433"
# UE netns -> numeric ue tag, matching tcp_probe.py / cc_campaign.py.
UE_TAG = {"ue1": 0, "ue2": 1}
VM = os.environ.get("UPT_VM_URL", "http://localhost:30491")
PICO = "/tmp/picoquicdemo"                 # Ubuntu build copied into the gNB pod
LDPATH = "LD_LIBRARY_PATH=/tmp"            # picks up /tmp/libcrypto.so.3


def kx(args, timeout=30):
    return subprocess.run(["kubectl", "exec", "-n", NS, POD, "-c", GNB, "--"] + args,
                          capture_output=True, text=True, timeout=timeout)


def push(lines):
    if not lines:
        return
    for i in range(0, len(lines), 5000):                # chunk large batches
        body = "\n".join(lines[i:i + 5000]).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{VM}/write", data=body,
                headers={"Content-Type": "text/plain"}), timeout=5).read()
        except Exception as e:
            print(f"[quic] VM write failed: {e}", file=sys.stderr)


# ------------------------------------------------------------------ run client
def run_client(ns, cc, qdir, nbytes, seconds):
    """Launch a picoquic perf download on UE `ns` with qlog into `qdir`.
    Returns (goodput_mbps, stdout, epoch0_ns). Blocks until the transfer
    finishes or the -d cap (seconds) elapses. epoch0_ns is wall-clock at launch,
    used to anchor the qlog's monotonic timeline to VM's clock."""
    # fresh qlog dir
    kx(["ip", "netns", "exec", ns, "rm", "-rf", qdir])
    kx(["ip", "netns", "exec", ns, "mkdir", "-p", qdir])
    scenario = f"*1:397:{nbytes};"                       # id:post:response(bytes)
    cmd = ["ip", "netns", "exec", ns, "env", LDPATH, PICO,
           "-a", "perf", "-G", cc, "-q", qdir, "-D"]
    if seconds:
        cmd += ["-d", str(int(seconds * 1_000_000))]     # -d is microseconds
    cmd += [SERVER, PORT, scenario]
    # allow the exec to run a little past the -d cap
    to = (seconds or 120) + 30
    epoch0_ns = time.time_ns()                           # wall-clock at launch
    r = kx(cmd, timeout=to)
    gp = None
    m = re.search(r"Download_Mbps:\s*([0-9.]+)", r.stdout) \
        or re.search(r"Received\s+\d+\s+bytes.*?([0-9.]+)\s*Mbps", r.stdout)
    if m:
        gp = float(m.group(1))
    return gp, r.stdout, epoch0_ns


# ------------------------------------------------------------------ parse qlog
# event line: [ <delta_us>, "recovery", "metrics_updated"|"packet_lost", "grp", {..} ]
# delta_us is relative to the connection start (qlog "reference_time"). That
# reference is picoquic's MONOTONIC clock, not epoch, so we ignore it and anchor
# to epoch0_ns (wall-clock captured at client launch) instead.
EVT_RE = re.compile(
    r'\[\s*(\d+)\s*,\s*"recovery"\s*,\s*"(metrics_updated|packet_lost)"[^{]*(\{.*\})?')

# picoquic emits only the CHANGED fields per metrics_updated event; carry-forward
# the last known value for the rest.
METRIC_KEYS = ("smoothed_rtt", "min_rtt", "latest_rtt", "cwnd",
               "bytes_in_flight", "pacing_rate")


def parse_qlog(text, ue_tag, cc, epoch0_ns):
    """Turn one qlog's text into VM line-protocol samples, timestamps anchored to
    epoch0_ns (wall-clock at client launch) + the qlog's per-event delta."""
    state = {}                                           # last-known metric values
    lost = 0
    lines = []
    for raw in text.splitlines():
        m = EVT_RE.search(raw)
        if not m:
            continue
        delta_us = int(m.group(1))
        ts_ns = epoch0_ns + delta_us * 1000
        if m.group(2) == "packet_lost":
            lost += 1
            lines.append(f"quic,ue={ue_tag},cc={cc} lost_total={lost}i {ts_ns}")
            continue
        obj = m.group(3)
        if not obj:
            continue
        try:
            d = json.loads(obj)
        except json.JSONDecodeError:
            continue
        state.update({k: d[k] for k in METRIC_KEYS if k in d})
        if "smoothed_rtt" not in state and "latest_rtt" not in state:
            continue                                     # nothing useful yet
        fields = []
        if "smoothed_rtt" in state:
            fields.append(f"smoothed_rtt_ms={state['smoothed_rtt']/1000.0:.3f}")
        if "min_rtt" in state:
            fields.append(f"min_rtt_ms={state['min_rtt']/1000.0:.3f}")
        if "latest_rtt" in state:
            fields.append(f"latest_rtt_ms={state['latest_rtt']/1000.0:.3f}")
        if "cwnd" in state:
            fields.append(f"cwnd_bytes={int(state['cwnd'])}i")
        if "bytes_in_flight" in state:
            fields.append(f"bytes_in_flight={int(state['bytes_in_flight'])}i")
        if "pacing_rate" in state:
            fields.append(f"pacing_mbps={state['pacing_rate']/1e6:.4f}")
        fields.append(f"lost_total={lost}i")
        lines.append(f"quic,ue={ue_tag},cc={cc} {','.join(fields)} {ts_ns}")
    return lines


def pull_qlog(ns, qdir):
    """Find the qlog file(s) in the pod dir and return their text."""
    ls = kx(["ip", "netns", "exec", ns, "bash", "-c",
             f"ls {qdir}/*.qlog {qdir}/*.sqlog 2>/dev/null"]).stdout.split()
    texts = []
    for f in ls:
        r = kx(["ip", "netns", "exec", ns, "cat", f], timeout=60)
        if r.stdout:
            texts.append((f, r.stdout))
    return texts


# ------------------------------------------------------------------ summary
def summarize(lines, ue_tag, cc, gp):
    srtt = [float(re.search(r"smoothed_rtt_ms=([0-9.]+)", l).group(1))
            for l in lines if "smoothed_rtt_ms=" in l]
    cwnd = [int(re.search(r"cwnd_bytes=(\d+)i", l).group(1))
            for l in lines if "cwnd_bytes=" in l]
    inflt = [int(re.search(r"bytes_in_flight=(\d+)i", l).group(1))
             for l in lines if "bytes_in_flight=" in l]
    lost = 0
    for l in lines:
        m = re.search(r"lost_total=(\d+)i", l)
        if m:
            lost = max(lost, int(m.group(1)))

    def pct(a, p):
        if not a:
            return float("nan")
        a = sorted(a); return a[min(len(a) - 1, int(p * len(a)))]
    print(f"  ue{ue_tag} cc={cc}: "
          f"goodput={gp if gp is not None else float('nan'):.2f} Mbps  "
          f"srtt(min/p50/p95)={pct(srtt,0.0):.1f}/{pct(srtt,0.5):.1f}/{pct(srtt,0.95):.1f} ms  "
          f"cwnd_max={max(cwnd)/1000 if cwnd else 0:.0f}KB  "
          f"inflight_max={max(inflt)/1000 if inflt else 0:.0f}KB  "
          f"lost={lost}  samples={len(srtt)}")


# ------------------------------------------------------------------ main
def main():
    global VM
    ap = argparse.ArgumentParser(description="QUIC endpoint probe (picoquic qlog -> VM)")
    ap.add_argument("--ue", default="both", help="ue1 | ue2 | both")
    ap.add_argument("--cc", default="cubic", help="picoquic CC: newreno cubic bbr prague ...")
    ap.add_argument("--bytes", default="200000000", help="response size per flow (bytes)")
    ap.add_argument("--seconds", type=float, default=60, help="cap the download (0=until bytes done)")
    ap.add_argument("--qdir", default="/tmp/qlog", help="qlog dir inside the UE netns")
    ap.add_argument("--parse-only", action="store_true", help="parse an existing qdir, no traffic")
    ap.add_argument("--no-push", action="store_true", help="parse + summarize but do not push to VM")
    ap.add_argument("--vm", default=VM)
    args = ap.parse_args()
    VM = args.vm or VM

    ues = list(UE_TAG) if args.ue == "both" else [args.ue]
    for ns in ues:
        if ns not in UE_TAG:
            print(f"[quic] unknown UE {ns}", file=sys.stderr); sys.exit(2)

    print(f"[quic] cc={args.cc} ues={ues} "
          f"{'(parse-only)' if args.parse_only else f'bytes={args.bytes} cap={args.seconds}s'}",
          flush=True)

    # wrap mode runs both UEs concurrently so they share the radio like the campaign
    goodput = {}
    epoch0 = {}
    if not args.parse_only:
        import threading
        def worker(ns):
            qdir = f"{args.qdir}_{ns}"
            gp, _, e0 = run_client(ns, args.cc, qdir, args.bytes, args.seconds)
            goodput[ns] = gp; epoch0[ns] = e0
        ts = [threading.Thread(target=worker, args=(ns,)) for ns in ues]
        for t in ts: t.start()
        for t in ts: t.join()

    total = 0
    for ns in ues:
        qdir = f"{args.qdir}_{ns}" if not args.parse_only else args.qdir
        texts = pull_qlog(ns, qdir)
        if not texts:
            print(f"  ue{UE_TAG[ns]}: no qlog found in {qdir}", file=sys.stderr); continue
        # parse-only has no launch time; fall back to now minus a nominal span
        e0 = epoch0.get(ns, time.time_ns())
        lines = []
        for _, txt in texts:
            lines += parse_qlog(txt, UE_TAG[ns], args.cc, e0)
        if not args.no_push:
            push(lines)
        total += len(lines)
        summarize(lines, UE_TAG[ns], args.cc, goodput.get(ns))
    print(f"[quic] pushed {total} samples to {VM}"
          f"{' (skipped, --no-push)' if args.no_push else ''}", flush=True)


if __name__ == "__main__":
    main()
