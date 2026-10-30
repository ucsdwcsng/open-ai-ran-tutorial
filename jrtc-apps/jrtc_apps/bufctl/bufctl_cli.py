#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
#
# bufctl_cli - terminal front end for manual RLC DL buffer-size control.
#
# Run it on the testbed host, not in a container:
#     python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py
#
# On start it loads the bufctl deployment (codelet + xApp); on quit it unloads it,
# because while the codelet is loaded the control hook runs on every downlink packet.
# Limits already applied survive the unload, so RESET before quitting if you want the
# configured sizes back.
#
# How it talks to the xApp: this script appends JSON lines to commands.jsonl and reads
# state.json, both in this directory, which the jrtc pod sees as /apps/bufctl.
#
# Commands:
#   scan                    ask every UE 0..N / DRB 1..M to report (discovers bearers)
#   list                    show the table
#   set <ue> <drb> sdu <n>  set the SDU limit
#   set <ue> <drb> byte <n> set the byte limit
#   reset <ue> <drb>        restore the configured size
#   load / unload           load or unload the codelet + xApp by hand
#   quit                    unload and exit

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CMD_FILE = os.path.join(HERE, "commands.jsonl")
STATE_FILE = os.path.join(HERE, "state.json")

NS = os.environ.get("BUFCTL_NS", "ran")
POD = os.environ.get("BUFCTL_POD", "jrtc-0")
DEPLOYMENT = os.environ.get("BUFCTL_DEPLOYMENT", "/apps/bufctl/deployment.yaml")
CLUSTER = os.environ.get("BUFCTL_CLUSTER", "janus-cluster")

SCAN_UES = int(os.environ.get("BUFCTL_SCAN_UES", "4"))     # UE indices 0..SCAN_UES-1
SCAN_DRBS = int(os.environ.get("BUFCTL_SCAN_DRBS", "2"))   # DRB ids 1..SCAN_DRBS

# The gNB clamps a byte limit to at least one maximum-size PDCP PDU. Shown as guidance;
# the gNB is the authority and clamps anything out of range.
MIN_BYTES = 9007


def _kubeconfig():
    env = dict(os.environ)
    if "KUBECONFIG" not in env:
        try:
            out = subprocess.run(["k3d", "kubeconfig", "write", CLUSTER],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                env["KUBECONFIG"] = out.stdout.strip()
        except Exception:
            pass
    return env


def _check_tree():
    """Warn if jrtc-0 reads /apps from a different tree than this script lives in.

    /apps is a hostPath pinned to the REPO_ROOT the Helm release was installed
    with. If this file sits somewhere else, we would append commands to a
    commands.jsonl that no xApp ever reads, and every 'set' would look like it
    worked while changing nothing.
    """
    try:
        r = subprocess.run(["kubectl", "get", "statefulset", "-n", NS, "jrtc", "-o",
                            "jsonpath={range .spec.template.spec.volumes[*]}"
                            "{.name}={.hostPath.path}{\"\\n\"}{end}"],
                           capture_output=True, text=True, timeout=30, env=_kubeconfig())
        pod_apps = next((l.split("=", 1)[1] for l in (r.stdout or "").splitlines()
                         if l.startswith("apps=")), "")
    except Exception:
        return
    if not pod_apps:
        return
    mine = os.path.dirname(HERE)          # .../jrtc-apps/jrtc_apps
    if os.path.realpath(pod_apps) != os.path.realpath(mine):
        print("=" * 78)
        print(" !! TREE MISMATCH -- commands from here would be ignored")
        print(f"    jrtc-0 reads /apps from : {pod_apps}")
        print(f"    this checkout is        : {mine}")
        print("    Run the CLI from that tree instead, or re-install the Helm release")
        print("    with your own REPO_ROOT (README section 4).")
        print("=" * 78)


def _jrtc_ctl(action):
    """Run jrtc-ctl load|unload inside the jrtc pod."""
    cmd = ("export JRTC_APPS=/apps JBPF_CODELETS=/codelets; "
           f"/jrtc/out/bin/jrtc-ctl {action} -c {DEPLOYMENT} --log-level error 2>&1 | tail -3")
    try:
        r = subprocess.run(["kubectl", "exec", "-n", NS, POD, "-c", "jrtc", "--",
                            "bash", "-c", cmd],
                           capture_output=True, text=True, timeout=180, env=_kubeconfig())
        out = (r.stdout or "") + (r.stderr or "")
        print(f"  jrtc-ctl {action}: {out.strip() or 'ok'}")
        return r.returncode == 0
    except Exception as e:
        print(f"  ! jrtc-ctl {action} failed: {e}")
        return False


def _send(op, ue, drb, **kw):
    obj = {"op": op, "ue": int(ue), "drb": int(drb), "ts": time.time()}
    obj.update(kw)
    try:
        with open(CMD_FILE, "a") as f:
            f.write(json.dumps(obj) + "\n")
        return True
    except Exception as e:
        print(f"  ! cannot write {CMD_FILE}: {e}")
        return False


def _state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _print_table(wait=1.5):
    """Print the bearer table from the xApp's snapshot."""
    time.sleep(wait)                      # give the xApp a moment to write it
    st = _state()
    if not st or not st.get("bearers"):
        print("  (no bearers reported yet -- is the gNB running with downlink traffic?")
        print("   a bearer only reports once it carries DL packets; try 'scan')")
        return
    age = time.time() - st.get("updated", 0)
    print(f"\n  UE  DRB   current            configured         last event   ranges")
    print("  " + "-" * 76)
    for b in st["bearers"]:
        cur = f"{b['cur_bytes']}B/{b['cur_sdus']}SDU"
        cfg = f"{b['cfg_bytes']}B/{b['cfg_sdus']}SDU"
        rng = f"sdu 1..{b['cfg_sdus']}, byte {MIN_BYTES}..{b['cfg_bytes']}"
        flag = "" if cur == cfg else "  <- changed"
        print(f"  {b['ue']:<3} {b['drb']:<5} {cur:<18} {cfg:<18} {b['reason']:<12} {rng}{flag}")
    print(f"  (snapshot {age:.1f}s old; reports={st.get('reports', 0)}, "
          f"sent={st.get('sent', 0)}, failed={st.get('failed', 0)})\n")


def _validate(ue, drb, unit, value):
    """Check against the bearer's configured range when we know it. The gNB clamps anyway."""
    st = _state() or {}
    row = None
    for b in st.get("bearers", []):
        if b["ue"] == ue and b["drb"] == drb:
            row = b
            break
    if row is None:
        print(f"  note: UE {ue} DRB {drb} has not reported yet; sending anyway")
        return True
    if unit == "sdu":
        lo, hi = 1, row["cfg_sdus"]
    else:
        lo, hi = MIN_BYTES, row["cfg_bytes"]
    if value < lo or value > hi:
        print(f"  ! {value} is outside {lo}..{hi} for this bearer; the gNB would clamp it.")
        return input("    send anyway? [y/N] ").strip().lower().startswith("y")
    return True


# Whether WE currently have the codeletset loaded, so main() can always unload it.
_LOADED = {"on": False}

HELP = """  Commands:
    scan                      ask UE 0..%d / DRB 1..%d to report (discovers bearers)
    list                      show the bearer table
    set <ue> <drb> sdu <n>    set the SDU limit     e.g.  set 0 1 sdu 512
    set <ue> <drb> byte <n>   set the byte limit    e.g.  set 0 1 byte 200000
    reset <ue> <drb>          restore the configured size
    load / unload             load or unload the codelet + xApp by hand
    help                      this text
    quit                      unload and exit

  Notes:
    - A change applies on that bearer's NEXT downlink packet; an idle bearer
      does not report until traffic resumes.
    - The gNB is the authority and clamps out-of-range values: SDU to
      1..cfg_sdus, bytes to %d..cfg_bytes. 'list' shows each bearer's range.
    - Applied limits SURVIVE the unload, so 'reset <ue> <drb>' before quitting
      if you want the configured sizes back."""


def _help():
    print(HELP % (SCAN_UES - 1, SCAN_DRBS, MIN_BYTES))


def _session():
    _check_tree()
    print("=" * 78)
    print(" RLC DL buffer-size control")
    print(" The codelet is loaded now and unloaded on quit, because while it is")
    print(" loaded the control hook runs on every downlink packet.")
    print("=" * 78)
    if _jrtc_ctl("load"):
        _LOADED["on"] = True
    else:
        # A 400 here almost always means a previous session exited without
        # unloading, so the codelet is still attached -- and a loaded control
        # codelet runs on EVERY downlink packet.
        print("  (load failed. A 400 / 'already loaded' means a previous session did")
        print("   not unload -- type 'unload' then 'load' for a clean state.)")
        _LOADED["on"] = True     # assume attached, so we still clean up on exit
    _send("get", 0, 1)                    # nudge: ask UE0/DRB1 to report
    _print_table()
    print("  type 'help' for commands")

    while True:
        try:
            line = input("bufctl> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = "quit"
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ("q", "quit", "exit"):
            print("  bye")
            return
        if cmd in ("h", "help", "?"):
            _help()
            continue
        if cmd == "list":
            _print_table(wait=0.2)
            continue
        if cmd == "scan":
            n = 0
            for ue in range(SCAN_UES):
                for drb in range(1, SCAN_DRBS + 1):
                    _send("get", ue, drb)
                    n += 1
            print(f"  asked {n} (UE, DRB) pairs to report")
            _print_table(wait=2.0)
            continue
        if cmd in ("load", "unload"):
            if _jrtc_ctl(cmd):
                _LOADED["on"] = (cmd == "load")
            continue
        if cmd == "reset":
            if len(parts) != 3:
                print("  usage: reset <ue> <drb>")
                continue
            ue, drb = int(parts[1]), int(parts[2])
            _send("reset", ue, drb)
            print(f"  RESET sent for UE {ue} DRB {drb} (applies on its next DL packet)")
            _print_table()
            continue
        if cmd == "set":
            if len(parts) != 5 or parts[3].lower() not in ("sdu", "sdus", "byte", "bytes"):
                print("  usage: set <ue> <drb> sdu|byte <value>")
                print("         e.g.  set 0 1 sdu 512      set 0 1 byte 200000")
                continue
            ue, drb = int(parts[1]), int(parts[2])
            unit = "sdu" if parts[3].lower().startswith("sdu") else "byte"
            try:
                value = int(parts[4])
            except ValueError:
                print("  ! value must be an integer")
                continue
            if not _validate(ue, drb, unit, value):
                continue
            if unit == "sdu":
                _send("set", ue, drb, sdus=value)
            else:
                _send("set", ue, drb, bytes=value)
            print(f"  SET sent: UE {ue} DRB {drb} {unit}={value} "
                  f"(applies on its next DL packet)")
            _print_table()
            continue
        print(f"  ! unknown command '{cmd}' -- try 'help'")


def main():
    """Run the session and unload on EVERY exit path, exceptions included.

    A crash that leaves the codeletset loaded is not harmless: the control hook
    keeps running on every downlink packet until someone unloads it by hand.
    """
    try:
        _session()
    finally:
        if _LOADED["on"]:
            print("  unloading bufctl (applied limits stay in force)")
            _jrtc_ctl("unload")
            _LOADED["on"] = False


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  interrupted")
        sys.exit(1)
