#!/usr/bin/env python3
# Buffer-size sweep with FRESH TCP per step.
#   baseline UE (ue2, netns ue2, port 5202): default 4096 buffer, ONE continuous Cubic flow (the contrast)
#   test UE     (ue0, netns ue1, port 5201): buffer swept by the codelet; RESTART its Cubic flow at each
#                                            sweep step so Cubic ramps cleanly into the new (constant) buffer
# This avoids the mid-flow buffer drop that collapses Cubic and prevents the buffer from binding.
import subprocess, time, urllib.request, json, os, sys

VM = "http://localhost:30491"
NS, POD = "ran", "srs-gnb-du1-0"

def kx(container, cmd, t=30):
    return subprocess.run(["kubectl", "exec", "-n", NS, POD, "-c", container, "--", "bash", "-c", cmd],
                          capture_output=True, text=True, timeout=t)

def sdu_limit():
    try:
        d = json.load(urllib.request.urlopen(VM + "/api/v1/query?query=upt_ctrl_sdu_limit", timeout=3))["data"]["result"]
        return d[0]["value"][1] if d else None
    except Exception:
        return None

def start_flow(netns, port, dur):
    kx("durue1",
       f"setsid bash -c 'exec ip netns exec {netns} iperf3 -c 10.45.0.1 -p {port} -t {dur} -R "
       f"--logfile /tmp/iperf_{'ue0' if port==5201 else 'ue1'}.log' </dev/null >/dev/null 2>&1 & disown; echo started")

# 0. clean slate: kill all stray iperf clients in the UE runner
kx("durue1", "pkill -9 -x iperf3 2>/dev/null; sleep 2; echo cleaned")

# 1. (re)start the continuous baseline on the fixed-4096 UE (ue2 netns, port 5202)
start_flow("ue2", 5202, 3600)
print("[orch] baseline ue2@4096 continuous flow started", flush=True)

# 2. watch the sweep; restart the test UE's flow on every buffer-size change
prev = None
while True:
    cur = sdu_limit()
    if cur and cur != prev:
        prev = cur
        kx("durue1", "pkill -f 'p 5201' 2>/dev/null; sleep 3")
        start_flow("ue1", 5201, 120)   # fresh flow; killed & restarted at next step
        print(f"[orch] test UE fresh Cubic flow @ buffer={cur} SDUs   (baseline ue2@4096 running)", flush=True)
    time.sleep(2)
