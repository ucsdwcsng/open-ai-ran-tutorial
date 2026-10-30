#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. Licensed under the MIT license.
#
# CC x buffer-size campaign over the Scout RAN.
#
# Transport: picoquic (userspace QUIC/UDP) so we can select congestion control
# in userspace -- newreno, cubic, bbr, prague -- without kernel modules (the
# host kernel only has cubic/reno). The rlc_ctrl codelet sweeps the RLC DL
# byte-limit through 8 fixed sizes (~2 min each); we run one full sweep per CC
# on BOTH UEs and bin the results by the live setpoint.
#
# Measurements (both transport-agnostic, so they work for QUIC):
#   goodput  = d(UE tun_srsue RX bytes)/dt, sampled here every 2 s -> VM upt_goodput
#   latency  = upt_rlc_queue_latency_ms  (RAN codelet, already in VM)
#   setpoint = upt_ctrl_byte_limit       (RAN, already in VM)
#
# Writes telemetry/cc_manifest.json = {cc: [t_start_s, t_end_s]} for the plotter.

import json, os, subprocess, time, urllib.request

NS = "ran"; POD = "srs-gnb-du1-0"; GNB = "gnb"
SERVER = "10.45.0.1"; PORT = "4433"
UES = {0: "ue1", 1: "ue2"}
VM = os.environ.get("UPT_VM_URL", "http://localhost:30491")
CCS = os.environ.get("CC_LIST", "newreno,cubic,bbr,prague").split(",")
CYCLE_S = int(os.environ.get("CYCLE_S", "1140"))     # >= one 8x137s sweep
SAMPLE_S = 2
HUGE = "200000000000"                                 # 200 GB -> effectively continuous
MANIFEST = os.path.join(os.path.dirname(__file__), "cc_manifest.json")

KUBECONFIG = os.environ.get("KUBECONFIG", "")


def kx(args, **kw):
    return subprocess.run(["kubectl", "exec", "-n", NS, POD, "-c", GNB, "--"] + args,
                          capture_output=True, text=True, timeout=kw.get("t", 30))


def rx_bytes():
    """RX bytes on tun_srsue for both UEs, in one exec."""
    script = ("for ns in ue1 ue2; do ip netns exec $ns cat /proc/net/dev "
              "| awk '/tun_srsue/{gsub(/:/,\" \"); print $2}'; done")
    out = kx(["bash", "-c", script]).stdout.split()
    vals = {}
    for i, ns in enumerate(("ue1", "ue2")):
        try: vals[i] = int(out[i])
        except (IndexError, ValueError): vals[i] = None
    return vals


def push(lines):
    if not lines: return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{VM}/write", data="\n".join(lines).encode(),
            headers={"Content-Type": "text/plain"}), timeout=3).read()
    except Exception as e:
        print(f"[cc] VM write failed: {e}", flush=True)


def start_clients(cc):
    """Launch a continuous picoquic download on each UE (host-side bg processes)."""
    procs = []
    for ue, ns in UES.items():
        cmd = ["kubectl", "exec", "-n", NS, POD, "-c", GNB, "--", "ip", "netns", "exec", ns,
               "env", "LD_LIBRARY_PATH=/tmp", "/tmp/picoquicdemo", "-a", "perf",
               "-G", cc, "-D", "-d", "1200000", SERVER, PORT, f"*1:397:{HUGE};"]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return procs


def stop_clients(procs):
    subprocess.run(["kubectl", "exec", "-n", NS, POD, "-c", GNB, "--",
                    "bash", "-c", "pkill -9 picoquicdemo 2>/dev/null; true"],
                   capture_output=True)
    for p in procs:
        try: p.kill()
        except Exception: pass


def main():
    manifest = {}
    print(f"[cc] campaign: CCs={CCS} cycle={CYCLE_S}s (~{CYCLE_S*len(CCS)/60:.0f} min total)", flush=True)
    for cc in CCS:
        print(f"[cc] === {cc} ===", flush=True)
        procs = start_clients(cc)
        t_start = time.time()
        last = rx_bytes(); last_t = t_start
        # let the flow ramp before we count the window as valid
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
        stop_clients(procs)
        manifest[cc] = [t_start, t_end]
        json.dump(manifest, open(MANIFEST, "w"), indent=2)
        print(f"[cc] {cc} done ({t_end-t_start:.0f}s). manifest saved.", flush=True)
        time.sleep(8)   # gap so buffers drain / setpoint boundary
    print(f"[cc] CAMPAIGN COMPLETE. manifest: {MANIFEST}", flush=True)


if __name__ == "__main__":
    main()
