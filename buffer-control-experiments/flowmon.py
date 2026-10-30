#!/usr/bin/env python3
"""flowmon - per-FLOW TCP metrics for the experiments, pushed live to VictoriaMetrics.

tcp_probe labels samples only by UE IP, so two flows to the same UE come out as one line.
flowmon reads the kernel's own view of each sender socket (`ss -ti` in the UPF, which is
where the downlink iperf3 clients run) and labels it by destination port:

    5201 -> bulk      5202 -> video      (override with --flows 5201=bulk,5202=video)

Metrics (VictoriaMetrics, Influx line protocol):
    flow_rtt_ms{flow=...}     kernel smoothed RTT of that flow
    flow_tput_mbps{flow=...}  bytes_acked rate (goodput as the sender sees it)
    flow_cwnd_kb{flow=...}    congestion window

One long-running `kubectl exec` streams ss every ~135 ms (50 ms sleep + ss round trip); nothing is re-spawned per sample.
Samples are averaged onto tcp_probe's 134.2 ms bucket grid (mean RTT, bytes ACKed over the
bucket) and pushed once per bucket, so the dashboard updates as smoothly as the upt panels.

    python3 buffer-control-experiments/flowmon.py            # foreground, Ctrl-C to stop
    bash buffer-control-experiments/flowmon.sh start|stop    # background
"""
import argparse
import os
import re
import subprocess
import sys
import urllib.request

VM = "http://localhost:30491"
BUCKET_SHIFT = 27            # 2^27 ns = 134.2 ms: must match UPT_BUCKET_SHIFT (upt_helpers.h, tcp_probe.py)
LOOP = ("while :; do echo \"@@ $(date +%s.%N)\"; "
        "ss -tinH state established '( dport = :{ports} )'; sleep {period}; done")


def env():
    e = dict(os.environ)
    e["PATH"] = os.path.expanduser("~/.local/bin") + ":" + e.get("PATH", "")
    if "KUBECONFIG" not in e:
        e["KUBECONFIG"] = subprocess.run(["k3d", "kubeconfig", "write", "janus-cluster"],
                                         capture_output=True, text=True, env=e).stdout.strip()
    return e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flows", default="5201=bulk,5202=video")
    ap.add_argument("--period", type=float, default=0.05)
    a = ap.parse_args()
    names = dict(kv.split("=") for kv in a.flows.split(","))
    E = env()
    upf = [l for l in subprocess.run(["kubectl", "get", "pods", "-n", "open5gs", "--no-headers", "-o",
                                      "custom-columns=:.metadata.name"], capture_output=True,
                                     text=True, env=E).stdout.split() if l.startswith("open5gs-upf-")][0]
    cmd = LOOP.format(ports=" or dport = :".join(names), period=a.period)
    p = subprocess.Popen(["kubectl", "exec", "-n", "open5gs", upf, "-c", "open5gs-upf", "--",
                          "bash", "-c", cmd], stdout=subprocess.PIPE, text=True, env=E)
    print(f"[flowmon] {names} -> {VM}/write", flush=True)

    # Aggregate onto tcp_probe's grid: 134.2 ms buckets (UPT_BUCKET_SHIFT = 27), stamped at the
    # bucket-close time, so these points line up with the upt_* and tcp_* series.
    agg = {}                      # port -> {"b": bucket id, "rtt": [..], "cwnd": kB, "t": ts, "acked": n}
    last = {}                     # port -> (ts, bytes_acked) at the end of the previous bucket
    ts, port, sock, lines = None, None, {}, []

    def emit(prt, a):
        fields = [f"rtt_ms={sum(a['rtt']) / len(a['rtt']):.2f}", f"cwnd_kb={a['cwnd']:.1f}"]
        if prt in last and a["t"] > last[prt][0] and a["acked"] >= last[prt][1]:
            fields.append(f"tput_mbps={(a['acked'] - last[prt][1]) * 8 / 1e6 / (a['t'] - last[prt][0]):.3f}")
        last[prt] = (a["t"], a["acked"])
        lines.append(f"flow,flow={names[prt]} {','.join(fields)} {(a['b'] + 1) << BUCKET_SHIFT}")

    def flush_block():
        # several sockets can share a port (iperf3's control connection): keep the busiest
        best = {}
        for prt, st in sock.items():
            for s in st:
                if prt not in best or s["acked"] > best[prt]["acked"]:
                    best[prt] = s
        b = int(ts * 1e9) >> BUCKET_SHIFT
        for prt, s in best.items():
            a = agg.get(prt)
            if a and a["b"] != b:
                emit(prt, a)
                a = None
            if a is None:
                a = agg[prt] = {"b": b, "rtt": []}
            a["rtt"].append(s["rtt"])
            a.update(cwnd=s["cwnd_kb"], t=ts, acked=s["acked"])

    for line in p.stdout:
        if line.startswith("@@"):
            if ts is not None:
                flush_block()
            ts, sock, port = float(line.split()[1]), {}, None
            if lines:
                try:
                    urllib.request.urlopen(urllib.request.Request(f"{VM}/write", data="\n".join(lines).encode()),
                                           timeout=5)
                except Exception as e:
                    print(f"[flowmon] write failed: {e}", file=sys.stderr, flush=True)
                lines.clear()
            continue
        m = re.search(r":(\d+)\s*$", line.split("\t")[0].strip()) if not line.startswith(("\t", " ")) else None
        if m:
            port = m.group(1) if m.group(1) in names else None
            continue
        if port:
            rtt = re.search(r"\brtt:([\d.]+)/", line)
            acked = re.search(r"bytes_acked:(\d+)", line)
            cwnd = re.search(r"\bcwnd:(\d+)", line)
            mss = re.search(r"\bmss:(\d+)", line)
            if rtt and acked:
                sock.setdefault(port, []).append({
                    "rtt": float(rtt.group(1)), "acked": int(acked.group(1)),
                    "cwnd_kb": int(cwnd.group(1)) * int(mss.group(1)) / 1e3 if cwnd and mss else 0})
            port = None


if __name__ == "__main__":
    main()
