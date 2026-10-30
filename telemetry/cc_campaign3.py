#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
#
# Run 2 of the CC x buffer-size campaign -- same structure as cc_campaign.py but
# with QUIC endpoint telemetry (quic_probe) enabled.
#
# Differences from Run 1:
#   * Buffer sweep is now SMALL binding sizes (5-400KB, set in rlc_ctrl.cpp) so
#     the RLC limit actually constrains the ~40KB working set and bufferbloat /
#     under-buffering shows up.
#   * Each picoquic download runs with `-q` qlog. At the end of every CC we pull
#     the qlog, parse it (quic_probe.parse_qlog) and push the transport's OWN
#     RTT / cwnd / bytes_in_flight / loss to VM as `quic_*`, timestamps anchored
#     to the client launch wall-clock. Goodput (UE tun RX) is sampled as before.
#
# Writes telemetry/cc_manifest3.json = {cc: [t_start_s, t_end_s]} for plot2.

import json, os, subprocess, time, urllib.request
from quic_probe import parse_qlog, UE_TAG

NS = "ran"; POD = "srs-gnb-du1-0"; GNB = "gnb"
SERVER = "10.45.0.1"; PORT = "4433"
UES = {0: "ue1", 1: "ue2"}
VM = os.environ.get("UPT_VM_URL", "http://localhost:30491")
CCS = os.environ.get("CC_LIST", "newreno,cubic,bbr,prague").split(",")
CYCLE_S = int(os.environ.get("CYCLE_S", "1140"))     # >= one 8x137s sweep
SAMPLE_S = 2
HUGE = "200000000000"                                 # 200 GB -> effectively continuous
IDLE_MS = "1200000"                                   # -d idle timeout (keep alive)
QDIR = "/tmp/qlog2"                                   # per-UE qlog dir base (pod /tmp)
MANIFEST = os.path.join(os.path.dirname(__file__), "cc_manifest3.json")


def kx(args, t=30):
    return subprocess.run(["kubectl", "exec", "-n", NS, POD, "-c", GNB, "--"] + args,
                          capture_output=True, text=True, timeout=t)


def rx_bytes():
    script = ("for ns in ue1 ue2; do ip netns exec $ns cat /proc/net/dev "
              "| awk '/tun_srsue/{gsub(/:/,\" \"); print $2}'; done")
    out = kx(["bash", "-c", script]).stdout.split()
    vals = {}
    for i in range(2):
        try: vals[i] = int(out[i])
        except (IndexError, ValueError): vals[i] = None
    return vals


def push(lines):
    if not lines: return
    for i in range(0, len(lines), 5000):
        body = "\n".join(lines[i:i + 5000]).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{VM}/write", data=body, headers={"Content-Type": "text/plain"}),
                timeout=5).read()
        except Exception as e:
            print(f"[cc3] VM write failed: {e}", flush=True)


def start_clients(cc):
    """Continuous picoquic download per UE with qlog. Returns (procs, epoch0)."""
    procs = []; epoch0 = {}
    for ue, ns in UES.items():
        qd = f"{QDIR}_{ns}"
        kx(["ip", "netns", "exec", ns, "bash", "-c", f"rm -rf {qd}; mkdir -p {qd}"])
        cmd = ["kubectl", "exec", "-n", NS, POD, "-c", GNB, "--", "ip", "netns", "exec", ns,
               "env", "LD_LIBRARY_PATH=/tmp", "/tmp/picoquicdemo", "-a", "perf",
               "-G", cc, "-q", qd, "-D", "-d", IDLE_MS, SERVER, PORT, f"*1:397:{HUGE};"]
        epoch0[ue] = time.time_ns()
        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return procs, epoch0


def harvest_qlogs(cc, epoch0):
    """Pull each UE's qlog (while clients still alive so it's flushed to disk),
    parse and push quic_* metrics."""
    total = 0
    for ue, ns in UES.items():
        qd = f"{QDIR}_{ns}"
        files = kx(["bash", "-c", f"ls {qd}/*.client.qlog 2>/dev/null"]).stdout.split()
        lines = []
        for f in files:
            txt = kx(["cat", f], t=120).stdout
            if txt:
                lines += parse_qlog(txt, UE_TAG[ns], cc, epoch0[ue])
        push(lines)
        total += len(lines)
        print(f"[cc3]   ue{ue} qlog -> {len(lines)} quic samples", flush=True)
    return total


def stop_clients(procs):
    subprocess.run(["kubectl", "exec", "-n", NS, POD, "-c", GNB, "--",
                    "bash", "-c", "pkill -9 picoquicdemo 2>/dev/null; true"],
                   capture_output=True)
    for p in procs:
        try: p.kill()
        except Exception: pass


def main():
    manifest = {}
    print(f"[cc3] Run 2 (QUIC probe): CCs={CCS} cycle={CYCLE_S}s "
          f"(~{CYCLE_S*len(CCS)/60:.0f} min total)", flush=True)
    for cc in CCS:
        print(f"[cc3] === {cc} ===", flush=True)
        procs, epoch0 = start_clients(cc)
        t_start = time.time()
        last = rx_bytes(); last_t = t_start
        while time.time() - t_start < CYCLE_S:
            time.sleep(SAMPLE_S)
            now = time.time(); cur = rx_bytes()
            dt = now - last_t; lines = []
            for ue in UES:
                if cur[ue] is not None and last[ue] is not None and dt > 0:
                    d = cur[ue] - last[ue]
                    if d >= 0:
                        mbps = d * 8.0 / dt / 1e6
                        lines.append(f"upt_goodput,ue={ue},cc={cc} mbps={mbps:.4f} {int(now*1e9)}")
            push(lines)
            last, last_t = cur, now
        t_end = time.time()
        # harvest qlogs BEFORE killing (file is flushed-so-far on disk)
        harvest_qlogs(cc, epoch0)
        stop_clients(procs)
        manifest[cc] = [t_start, t_end]
        json.dump(manifest, open(MANIFEST, "w"), indent=2)
        print(f"[cc3] {cc} done ({t_end-t_start:.0f}s). manifest saved.", flush=True)
        time.sleep(8)
    print(f"[cc3] CAMPAIGN COMPLETE. manifest: {MANIFEST}", flush=True)


if __name__ == "__main__":
    main()
