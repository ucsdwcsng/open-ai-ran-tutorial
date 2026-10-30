#!/usr/bin/env python3
"""EdgeRIC-RT scheduler muApp: every TTI, per-UE weights for the next DL slot, from the scheduler you choose.

    python3 muapps/sched_muapp.py --scheduler maxweight
    python3 muapps/sched_muapp.py --scheduler fixed --weights ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4
    python3 muapps/sched_muapp.py --scheduler rr | pf | maxcqi | bestcqi | stock
    python3 muapps/sched_muapp.py --scheduler rl --model runs/ppo_4ue/best.npz

Run it in the edgeric container (scripts/edgeric_muapp.sh start sched ...). Switch while running:

    bash scripts/edgeric_muapp.sh ctl "scheduler rr"
    bash scripts/edgeric_muapp.sh ctl "scheduler fixed ue1=0.1 ue2=0.1 ue3=0.4 ue4=0.4"
    bash scripts/edgeric_muapp.sh ctl "alloc shared" | "lead 2" | "mode work-conserving" | "hold 2" | "status"

Each report is the state of one TTI (RANtime). The muApp keeps every report (log, throughput, PF averages) but
acts only on the newest (Lazy RIC), tagging the action with the RANtime of the next DL slot (--lead 1 = the
paper's RANtime + 1). The gNB applies it only in that slot and otherwise runs its own scheduler.

--alloc single (default): one UE gets the whole slot, chosen by the policy's metric or, for share policies (fixed,
rl), by smooth weighted round robin over its weights. --alloc shared: every weighted UE gets RBs in proportion to
its weight in the same slot (see edgeric_rt/policies.py).
"""

import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from edgeric_rt import metrics, policies, rte2  # noqa: E402
from edgeric_rt.ue_map import UeMap  # noqa: E402

W = rte2.W
HOME = os.environ.get("EDGERIC_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scheduler", required=True, help="stock fixed rr pf maxcqi bestcqi maxweight rl")
    ap.add_argument("--weights", help="fixed: ue1=0.2,ue2=0.2,ue3=0.2,ue4=0.4 (or 0.2,0.2,0.2,0.4 by UE order)")
    ap.add_argument("--model", help="rl: trained policy (runs/<run>/best.npz)")
    ap.add_argument("--stochastic", action="store_true", help="rl: sample actions instead of the mean")
    ap.add_argument("--rl-pick", choices=("share", "argmax"), default=None,
                    help="rl with --alloc single: its probabilities as slot shares, or always its top UE "
                         "(default: argmax for a 'cat' model, share for a 'gauss' one)")
    ap.add_argument("--alloc", choices=("single", "shared"), default="single",
                    help="single: one UE per slot (default); shared: RBs split by weight within the slot")
    ap.add_argument("--pf-mode", choices=("cqi", "rate"), default="cqi")
    ap.add_argument("--pf-alpha", type=float, default=0.01)
    ap.add_argument("--lead", type=int, default=1, help="target the first DL slot >= lead TTIs after the report")
    ap.add_argument("--work-conserving", action="store_true", help="RBs a UE cannot use go to the others")
    ap.add_argument("--greedy-pdcch", action="store_true", help="let DL use the whole PDCCH budget")
    ap.add_argument("--hold", type=int, default=0, help="TTIs the gNB may re-apply an action after its HIT")
    ap.add_argument("--min-share", type=float, default=0.02, help="drop weights below this share")
    ap.add_argument("--ue-map", default=os.path.join(rte2.APP_DIR, "run", "ue_map.txt"))
    ap.add_argument("--duration", type=float, default=0.0, help="seconds to run (0 = until stopped)")
    ap.add_argument("--tag", help="run name (default <scheduler>-<time>)")
    ap.add_argument("--runs-dir", default=os.path.join(HOME, "runs"))
    ap.add_argument("--no-log", action="store_true", help="do not save the per-TTI npz")
    ap.add_argument("--stats", type=float, default=5.0, help="seconds between summary lines")
    ap.add_argument("--cpus", default="", help="pin the muApp, e.g. 29")
    ap.add_argument("--fifo", type=int, default=0, help="SCHED_FIFO priority")
    return ap.parse_args(argv)


class Ctl:
    """Control socket for live changes: one text command per datagram, reply to the sender."""

    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, "sched_ctl.sock")
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o666)
        self.sock.setblocking(False)

    def poll(self):
        try:
            data, addr = self.sock.recvfrom(4096)
        except (BlockingIOError, OSError):
            return None, None
        return data.decode(errors="replace").strip(), addr

    def reply(self, addr, text):
        if addr:
            try:
                self.sock.sendto(text.encode(), addr)
            except OSError:
                pass

    def close(self):
        self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def main(argv=None):
    args = build_args(argv)
    ue_map = UeMap(args.ue_map)
    pol = policies.make(args.scheduler, args, ue_map)
    for m in rte2.Pacer.apply(rte2.parse_cpus(args.cpus), args.fifo):
        print(f"[sched] {m}", flush=True)

    rt = rte2.RtE2(lead=args.lead)
    ctl = Ctl(rt.run_dir)
    tag = args.tag or f"{args.scheduler}-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = os.path.join(args.runs_dir, tag)
    log = None if args.no_log else metrics.RunLog(os.path.join(out_dir, "ttis.npz"))
    win = metrics.Window()
    vm = metrics.VmPusher()

    st = dict(wc=args.work_conserving, greedy=args.greedy_pdcch, hold=args.hold, min_share=args.min_share,
              alloc=args.alloc)
    sel = policies.SlotSelector()

    def flags():
        return (W.FLAG_WORK_CONSERVING if st["wc"] else 0) | (W.FLAG_GREEDY_PDCCH if st["greedy"] else 0)

    def describe():
        mode = "work-conserving" if st["wc"] else "strict"
        return (f"{pol.describe()} | alloc {st['alloc']} | lead {rt.lead} | {mode} | hold {st['hold']} | "
                f"min-share {st['min_share']}")

    print(f"[sched] {describe()} | reports from {rt.path} | run {tag}", flush=True)
    t0 = time.monotonic()
    next_stats = t0 + args.stats
    last_wait_msg = 0.0
    sent_before = 0
    summaries = []
    try:
        while True:
            now = time.monotonic()
            if args.duration and now - t0 >= args.duration:
                break
            reps = rt.poll(timeout=0.5)
            if not reps:
                if now - last_wait_msg > 5:
                    last_wait_msg = now
                    print("[sched] waiting for reports (Lazy RAN): bridge loaded? gNB running?", flush=True)
                cmd, addr = ctl.poll()
                if cmd:
                    pol_ref = [pol]
                    ctl.reply(addr, handle(cmd, args, ue_map, st, rt, log, None, pol_ref))
                    pol = pol_ref[0]
                continue

            for r in reps:
                pol.observe(r)  # policy state first (e.g. PF averages): the action below sees every report

            # Act on the newest report before any bookkeeping: the action has to reach the gNB within the slot.
            newest = reps[-1]
            w = pol.weights(newest)
            if w is not None:
                if st["alloc"] == "single" or pol.always_single:
                    target = rt.target_tti(newest)
                    u = sel.pick(target, w, pol.single_mode) if target is not None else None
                    iw = {u: 1} if u is not None else {}
                else:
                    iw = rte2.to_int_weights(w, st["min_share"])
                if iw:
                    rt.send(newest, iw, flags=flags(), hold=st["hold"])

            for r in reps:
                if log:
                    log.add(r)
                win.add(r)
                vm.add(r, now, {"scheduler": pol.name})

            cmd, addr = ctl.poll()
            if cmd:
                pol_ref = [pol]
                ctl.reply(addr, handle(cmd, args, ue_map, st, rt, log, newest, pol_ref))
                if pol_ref[0] is not pol:
                    pol = pol_ref[0]
                    print(f"[sched] switched at RANtime {int(newest['ran_tti'])}: {describe()}", flush=True)
                    win.reset(int(newest["ran_tti"]))

            if now >= next_stats:
                next_stats = now + args.stats
                ue_map.reload()
                vm.labels = {i: ue_map.name_of(i, rte2.ues_of(newest)) for i in ue_map.ordered(rte2.ues_of(newest))}
                s = win.summary()
                s.update(ran_tti=int(newest["ran_tti"]), scheduler=pol.describe(), lazy_ric=rt.lazy_ric,
                         lost=rt.lost, sent=rt.sent - sent_before)
                sent_before = rt.sent
                summaries.append(s)
                print(fmt_summary(s, vm.labels), flush=True)
                win.reset(int(newest["ran_tti"]))
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
        ctl.close()
        if log and log.n:
            os.makedirs(out_dir, exist_ok=True)
            try:
                os.chmod(out_dir, 0o777)  # runs/ is a host directory: let the host user add files next to the run
            except OSError:
                pass
            path = log.save(meta=dict(scheduler=pol.describe(), args=vars(args)))
            with open(os.path.join(out_dir, "summary.json"), "w") as f:
                json.dump(dict(scheduler=pol.describe(), args=vars(args), windows=summaries), f, indent=1,
                          default=float)
            print(f"[sched] saved {log.n} TTIs to {path}", flush=True)


def fmt_summary(s, labels):
    per = " ".join(f"{labels.get(k, f'du{k}')} {v:5.1f}" for k, v in sorted(s["per_ue_mbps"].items(),
                                                                          key=lambda kv: labels.get(kv[0], "")))
    return (f"[sched {s['scheduler']}] tti={s['ran_tti']} | {s['total_mbps']:5.1f} Mbps ({per}) | "
            f"DL HIT {s['dl_hit_pct']:5.1f}% of {s['dl_slots']} | report->action p50 {s['lat_p50_us']:.0f} "
            f"p99 {s['lat_p99_us']:.0f} us | slack p1 {s['slack_p1_us']:.0f} us | RB use {s['rb_util']:.2f} | "
            f"lazy-RIC {s['lazy_ric']} lost {s['lost']}")


def handle(cmd, args, ue_map, st, rt, log, rep, pol_ref):
    """Apply one control command; returns the reply text. pol_ref[0] is replaced on a scheduler switch."""
    parts = cmd.split()
    if not parts:
        return "empty command"
    op, rest = parts[0].lower(), parts[1:]
    tti = int(rep["ran_tti"]) if rep is not None else -1
    try:
        if op == "scheduler" and rest:
            ns = argparse.Namespace(**vars(args))
            ns.scheduler = rest[0]
            if len(rest) > 1:
                if rest[0] == "rl":
                    ns.model = rest[1]
                else:
                    ns.weights = " ".join(rest[1:])
            pol_ref[0] = policies.make(ns.scheduler, ns, ue_map)
        elif op == "weights" and rest:
            pol_ref[0] = policies.make("fixed", argparse.Namespace(**{**vars(args), "weights": " ".join(rest)}),
                                       ue_map)
        elif op == "lead" and rest:
            rt.lead = max(1, int(rest[0]))
        elif op == "mode" and rest:
            st["wc"] = rest[0].startswith("work")
        elif op == "alloc" and rest and rest[0] in ("single", "shared"):
            st["alloc"] = rest[0]
        elif op == "hold" and rest:
            st["hold"] = max(0, int(rest[0]))
        elif op in ("min-share", "min_share") and rest:
            st["min_share"] = float(rest[0])
        elif op == "status":
            pass
        else:
            return f"unknown command: {cmd} (scheduler <name> [weights|model] | weights .. | alloc single|shared | " \
                   f"lead N | mode strict|work-conserving | hold N | min-share x | status)"
    except ValueError as e:
        return f"error: {e}"
    if log and op != "status":
        log.event(tti, cmd)
    mode = "work-conserving" if st["wc"] else "strict"
    return (f"ok at RANtime {tti}: {pol_ref[0].describe()} | alloc {st['alloc']} | lead {rt.lead} | {mode} | "
            f"hold {st['hold']} | min-share {st['min_share']} | sent {rt.sent} lazy-RIC {rt.lazy_ric} lost {rt.lost}")


if __name__ == "__main__":
    main()
