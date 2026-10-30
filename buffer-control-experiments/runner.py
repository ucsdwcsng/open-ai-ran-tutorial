#!/usr/bin/env python3
"""Control-hook experiment runner for the Open AI-RAN ZMQ testbed (run on the testbed host).

    python3 runner.py configs/<exp>.json [--out results/<name>]

A config describes UEs, downlink TCP flows, and timed control events:

  {
    "name": "expA_cubic",
    "title": "...",
    "duration": 210,                     # seconds from t0 to the end of every flow
    "settle": 10,                        # seconds skipped at the start of each phase
    "ues":   [{"label": "UE", "container": "durue1", "netns": null, "du_index": "auto"}],
    "flows": [
      {"name": "video", "ue": 0, "cc": "cubic", "rate": "2M", "start": 0},
      {"name": "bulk",  "ue": 0, "cc": "cubic", "start": 15}
    ],
    "events": [
      {"t": 60,  "bufcap": "1m"},        # load a cap (swaps out any loaded one)
      {"t": 180, "bufcap": "off"},       # restore the configured buffer and unload
      {"t": 90,  "radio": "10"},         # channel: DL MCS <= 10 ("good" = normal; "10 fade" = fading)
      {"t": 90,  "bufctl": {"op": "set", "ue": 0, "drb": 1, "bytes": 200000}}
    ],
    "idle_ping": 0                       # optional pings before t0 (sparse ping != TCP RTT)
  }

Per flow it records receiver throughput (iperf3 server in the UE) and the sender's
srtt / cwnd / retransmits (iperf3 client on the UPF), every 0.5 s. Per UE it records RLC
queue bytes and queuing delay from the jbpf upt telemetry in VictoriaMetrics.
Outputs <out>/{events.json, raw/*} and then runs analyze.py on <out>.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BUFCTL_DIR = os.path.join(REPO, "jrtc-apps/jrtc_apps/bufctl")
VM = "http://localhost:30491"
GW = "10.45.0.1"
NS, POD = "ran", "srs-gnb-du1-0"


def _env():
    env = dict(os.environ)
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    env.setdefault("MPLCONFIGDIR", "/tmp/mpl")
    if "KUBECONFIG" not in env:
        env["KUBECONFIG"] = subprocess.run(["k3d", "kubeconfig", "write", "janus-cluster"],
                                           capture_output=True, text=True, env=env).stdout.strip()
    return env


ENV = _env()


def sh(args, timeout=60):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=ENV)
    return r.stdout


UPF_POD = [l for l in sh(["kubectl", "get", "pods", "-n", "open5gs", "--no-headers", "-o",
                          "custom-columns=:.metadata.name"]).split() if l.startswith("open5gs-upf-")][0]


def ue_exec(ue, cmd, timeout=60):
    if ue.get("netns"):
        cmd = f"ip netns exec {ue['netns']} bash -c {json.dumps(cmd)}"
    return sh(["kubectl", "exec", "-n", NS, POD, "-c", ue.get("container", "durue1"), "--",
               "bash", "-c", cmd], timeout)


def upf_exec(cmd, timeout=60):
    return sh(["kubectl", "exec", "-n", "open5gs", UPF_POD, "-c", "open5gs-upf", "--",
               "bash", "-c", cmd], timeout)


def spawn(where, argv):
    """Start a long-running command in a container WITHOUT orphaning it there.

    The host keeps a backgrounded kubectl exec for it. An orphan inside the container would be
    re-parented to the container's PID 1 (tail / open5gs-upfd), which never reaps it, leaving a
    zombie per run; an exec'd process is reaped by the container runtime.
    where: ("ran", POD, container) or ("open5gs", UPF_POD, "open5gs-upf").
    """
    ns, pod, c = where
    subprocess.Popen(["kubectl", "exec", "-n", ns, pod, "-c", c, "--"] + argv, env=ENV,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def bufcap(arg):
    args = ["off"] if arg == "off" else ["load", arg]
    return sh(["bash", os.path.join(HERE, "bufcap.sh")] + args, 200).strip()


def radio(arg):
    return sh(["bash", os.path.join(HERE, "radio.sh")] + str(arg).split(), 300).strip()


def jrtc_ctl_bufctl(action):
    cmd = ("export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl "
           f"{action} -c /apps/bufctl/deployment.yaml --log-level error 2>&1 | tail -3")
    return sh(["kubectl", "exec", "-n", NS, "jrtc-0", "-c", "jrtc", "--", "bash", "-c", cmd], 180).strip()


def bufctl_send(ev):
    obj = {"op": ev["op"], "ue": int(ev["ue"]), "drb": int(ev.get("drb", 1)), "ts": time.time()}
    for k in ("bytes", "sdus"):
        if k in ev:
            obj[k] = int(ev[k])
    with open(os.path.join(BUFCTL_DIR, "commands.jsonl"), "a") as f:
        f.write(json.dumps(obj) + "\n")


def vm_range(expr, t0, t1, step=0.5):
    q = urllib.parse.urlencode({"query": expr, "start": t0, "end": t1, "step": f"{step}s"})
    try:
        r = json.load(urllib.request.urlopen(f"{VM}/api/v1/query_range?{q}", timeout=30))
        return r["data"]["result"]
    except Exception as e:
        print(f"  ! VM query failed for {expr}: {e}")
        return []


def ue_ip(ue):
    m = re.search(r"10\.45\.0\.\d+", ue_exec(ue, f"ip -o -4 addr show {ue.get('dev', 'oaitun_ue1')}"))
    return m.group(0) if m else None


def resolve_du(ue):
    """du_index of a netns UE: its RNTI (UE log) -> the gNB's 'ue=N c-rnti=0x<rnti>' line."""
    if not ue.get("netns"):
        return 0                               # single-UE bring-up: the only UE is 0
    n = ue["netns"][-1]
    m = re.findall(r"RNTI ([0-9a-f]{4}) stats", ue_exec({"container": "durue1"},
                   f"grep -a 'RNTI .* stats' /tmp/ue{n}.log | tail -1"))
    if not m:
        return None
    g = sh(["kubectl", "exec", "-n", NS, POD, "-c", "ocudujbpf", "--", "bash", "-c",
            f"grep -a 'c-rnti=0x{m[0]} du_ue' /tmp/gnb.log | tail -1"])
    d = re.search(r"ue=(\d+) c-rnti", g)
    return int(d.group(1)) if d else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--out")
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    out = a.out or os.path.join(HERE, "results", cfg["name"])
    raw = os.path.join(out, "raw")
    os.makedirs(raw, exist_ok=True)
    dur = int(cfg["duration"])
    ues, flows, events = cfg["ues"], cfg["flows"], cfg.get("events", [])
    uses_bufcap = any("bufcap" in e for e in events)
    uses_bufctl = any("bufctl" in e for e in events)
    uses_radio = any("radio" in e for e in events)

    for i, ue in enumerate(ues):
        ue.setdefault("label", f"UE{i}")
        ue["ip"] = ue_ip(ue)
        if not ue["ip"]:
            sys.exit(f"{ue['label']}: no IP -- UE not attached")
        if ue.get("du_index", "auto") == "auto":
            ue["du_index"] = resolve_du(ue)
        print(f"  {ue['label']}: ip={ue['ip']} du_index={ue['du_index']}")
    for i, f in enumerate(flows):
        f.setdefault("port", 5201 + i)
        f.setdefault("label", f"{f['name']} ({f.get('cc', 'cubic')}"
                     + (f", {f['rate']}" if f.get("rate") else "") + ")")

    # clean slate: no traffic, no leftover cap from an earlier run
    upf_exec("pkill -9 -x iperf3; rm -f /tmp/exp_*; true")
    for ue in ues:
        ue_exec(ue, "pkill -9 -x iperf3; pkill -9 -x ping; rm -f /tmp/exp_*; true")
    if uses_bufcap:   # 'off' needs DL packets to act, so configs end with an explicit off event
        print("  pre-run:", sh(["bash", os.path.join(HERE, "bufcap.sh"), "unload"], 200).strip().splitlines()[0])
    if uses_radio:
        print("  pre-run:", radio("good").splitlines()[0])
    if uses_bufctl:
        print("  bufctl load:", jrtc_ctl_bufctl("load"))

    for ue in ues:
        n = int(cfg.get("idle_ping", 0))
        if n:
            txt = ue_exec(ue, f"ping -c {n} -i 0.2 -W 2 {GW}", 30)
            ue["idle_rtts"] = [float(x) for x in re.findall(r"time=([\d.]+)", txt)]

    for f in flows:
        ue = ues[f.get("ue", 0)]
        ns_prefix = ["ip", "netns", "exec", ue["netns"]] if ue.get("netns") else []
        spawn((NS, POD, ue.get("container", "durue1")),
              ns_prefix + ["iperf3", "-s", "-1", "-J", "-i", "0.5", "-B", ue["ip"], "-p", str(f["port"]),
                           "--logfile", f"/tmp/exp_rx_{f['port']}.json"])
    time.sleep(1.5)

    t0 = time.time()
    applied = []

    def start_flow(f):
        ue = ues[f.get("ue", 0)]
        fdur = int((f.get("stop") or dur) - f.get("start", 0))
        rate = ["-b", f["rate"]] if f.get("rate") else []
        spawn(("open5gs", UPF_POD, "open5gs-upf"),
              ["iperf3", "-c", ue["ip"], "-p", str(f["port"]), "-C", f.get("cc", "cubic"), "-t", str(fdur)]
              + rate + ["-i", "0.5", "-J", "--logfile", f"/tmp/exp_tx_{f['port']}.json"])
        applied.append({"t_actual": time.time() - t0, "kind": "flow_start", "flow": f["name"]})

    sched = [(f.get("start", 0), "flow", f) for f in flows] + [(e["t"], "event", e) for e in events]
    sched.sort(key=lambda x: x[0])
    try:
        for when, kind, obj in sched:
            while time.time() - t0 < when:
                time.sleep(0.02)
            if kind == "flow":
                threading.Thread(target=start_flow, args=(obj,)).start()
                continue
            ts = time.time() - t0
            if "bufcap" in obj:
                res = bufcap(obj["bufcap"])
                rec = {"kind": "bufcap", "arg": obj["bufcap"], "t_issued": ts,
                       "t_actual": time.time() - t0, "result": res}
                label = obj.get("note") or ("no cap" if obj["bufcap"] == "off" else f"cap {obj['bufcap']}")
            elif "radio" in obj:
                res = radio(obj["radio"])
                rec = {"kind": "radio", "arg": str(obj["radio"]), "t_issued": ts,
                       "t_actual": time.time() - t0, "result": res}
                label = obj.get("note") or ("channel normal" if obj["radio"] == "good" else f"MCS<={obj['radio']}")
            else:
                bufctl_send(obj["bufctl"])
                rec = dict(obj["bufctl"], kind="bufctl", t_issued=ts, t_actual=ts)
                label = obj.get("note") or f"bufctl {obj['bufctl']['op']}"
            rec["label"] = label
            applied.append(rec)
            print(f"  t={rec['t_issued']:6.1f}s -> {rec['t_actual']:6.1f}s  {label}"
                  + (f"   [{rec.get('result', '').splitlines()[-1] if rec.get('result') else ''}]"))

        while time.time() - t0 < dur + 3:
            time.sleep(0.5)
    finally:
        t1 = time.time()
        # iperf3 writes its JSON only when the test finishes; the end-of-test exchange
        # waits behind whatever is still queued (seconds, with a bloated buffer).
        for _ in range(60):
            if upf_exec("pgrep -c -x iperf3 || true").strip() in ("0", ""):
                break
            time.sleep(1)
        if uses_radio:
            print("  post-run:", radio("good").splitlines()[0])
        if uses_bufcap:
            print("  post-run:", sh(["bash", os.path.join(HERE, "bufcap.sh"), "unload"], 200).strip().splitlines()[0])
        if uses_bufctl:
            for ue in ues:
                bufctl_send({"op": "reset", "ue": ue["du_index"], "drb": 1})
            time.sleep(2)
            print("  bufctl unload:", jrtc_ctl_bufctl("unload"))

    for f in flows:
        ue, p = ues[f.get("ue", 0)], f["port"]
        open(f"{raw}/rx_{p}.json", "w").write(ue_exec(ue, f"cat /tmp/exp_rx_{p}.json"))
        open(f"{raw}/tx_{p}.json", "w").write(upf_exec(f"cat /tmp/exp_tx_{p}.json"))
    vm = {m: vm_range(f'{m}{{bearer="DRB1"}}', t0 - 2, t1 + 2)
          for m in ["upt_rlc_buffer_bytes", "upt_rlc_buffer_bytes_max",
                    "upt_rlc_queue_latency_ms", "upt_rlc_pkt_lat_p95_ms"]}
    json.dump(vm, open(f"{raw}/vm.json", "w"))
    json.dump({"t0": t0, "t1": t1, "config": cfg, "applied": applied},
              open(f"{out}/events.json", "w"), indent=1, default=str)

    subprocess.run([sys.executable, os.path.join(HERE, "analyze.py"), out], env=ENV)


if __name__ == "__main__":
    main()
