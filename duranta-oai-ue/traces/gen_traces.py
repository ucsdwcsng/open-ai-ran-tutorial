#!/usr/bin/env python3
"""Generate SNR traces for the UE's trace replay (chan_sim trace_ctl=), and check trace files.

    python3 gen_traces.py --all              # every recipe in scenarios.conf -> lib/<name>.csv
    python3 gen_traces.py --only trace_3     # one recipe (repeatable)
    python3 gen_traces.py --list
    python3 gen_traces.py --check FILE.csv [--allow-low-snr]   # what setup / trace_ctl.sh run first

A trace is CSV: header "t_s,dl_snr_db,ul_snr_db" (more columns are ignored by the UE; these files
add d_m), t from 0 in strictly increasing steps, step-hold between rows, looping at the end.

Two recipes (scenarios.conf, one line each: name mode key=value ...):

  walk    2D random walk around the gNB at a constant speed: the heading drifts (heading_sd_deg per
          sqrt(s)), the UE stays inside the ring min_m..max_m, SNR = snr_ref -
          path_loss_exp*log10(d/ref_m) + shadowing, the shadowing log-normal and correlated over the
          distance travelled (decorr_m). Floors as the UE's model (dl_snr_min 8 dB, ul_snr_min 5 dB)
          keep a UE away from release.
  bounce  the UE's built-in model (radio/zmq/chan_sim.cpp), ported exactly: start distance, speed,
          bouncing between min_distance and max_distance, 10 ms shadowing from the same hash.
          check_parity.py compares it with the C++ code.
  cqi     a CQI(t) process in [lo, hi] (CQI 4..15 by default), turned into DL SNR through cqi_calib.csv, the
          SNR at which this UE reports each CQI on the AWGN channel. UL is held at ul_snr so CSI reports and
          HARQ-ACKs get through. process= walk | band | iid | triangle | replay (a recorded CQI trace, e.g. the
          EdgeRIC NSDI'24 car / drone / robot / turntable traces in src_cqi/). The rows add a cqi column.
  staircase  DL SNR from snr_lo to snr_hi in snr_step dB, dwell_s each (UL fixed): replayed on one UE, it
          measures the SNR -> CQI map that cqi_calib.csv holds (edgeric-rt/tools/calibrate_cqi.py).
"""
import argparse
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
RECIPES = os.path.join(HERE, "scenarios.conf")
HEADER = "t_s,dl_snr_db,ul_snr_db"
M64 = (1 << 64) - 1

# ---- bounce: the UE's model, line for line (chan_sim.cpp distance_at / loss_db_at / jitter_db_at)

BOUNCE_DEFAULTS = dict(distance=100.0, speed=0.0, min_distance=0.0, max_distance=0.0, dl_snr_ref=25.0,
                       ul_snr_ref=25.0, path_loss_exp=37.6, dl_snr_min=8.0, ul_snr_min=5.0, jitter_db=0.0,
                       seed=0)


def _next_rand(s):
    z = (s + 0x9E3779B97F4A7C15) & M64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
    return z ^ (z >> 31)


def _hash_gauss(k, seed):
    a, b = (k * 2 + 1) & M64, (k * 2 + 2) & M64
    a ^= (seed * 0x9E3779B97F4A7C15) & M64
    b ^= (seed * 0x9E3779B97F4A7C15) & M64
    a, b = _next_rand(a), _next_rand(b)
    u1 = max((a >> 11) * 2.0 ** -53, 1e-12)
    u2 = (b >> 11) * 2.0 ** -53
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)


def bounce_distance(p, t):
    if p["speed"] == 0.0:
        return max(p["distance"], 1.0)
    d = p["distance"] + p["speed"] / 3.6 * t
    if p["max_distance"] > p["min_distance"]:
        span = p["max_distance"] - p["min_distance"]
        x = math.fmod(d - p["min_distance"], 2 * span)
        if x < 0:
            x += 2 * span
        d = p["min_distance"] + (x if x <= span else 2 * span - x)
    return max(d, 1.0)


def bounce_snr(p, t):
    """(dl, ul, d) at t, as chan_sim::link computes them for the base channel."""
    d = bounce_distance(p, t)
    loss = p["path_loss_exp"] * math.log10(d / max(p["distance"], 1.0))
    jit = 0.0
    if p["jitter_db"] > 0:
        x = t / 0.010
        k = int(math.floor(max(x, 0.0)))
        f = x - k
        g0, g1 = _hash_gauss(k, p["seed"]), _hash_gauss(k + 1, p["seed"])
        jit = p["jitter_db"] * (g0 + (g1 - g0) * f)
    loss = loss - jit
    return max(p["dl_snr_ref"] - loss, p["dl_snr_min"]), max(p["ul_snr_ref"] - loss, p["ul_snr_min"]), d


def gen_bounce(p, duration, step):
    n = int(round(duration / step))
    for k in range(n):
        t = k * step
        dl, ul, d = bounce_snr(p, t)
        yield t, dl, ul, d


# ---- walk: 2D random walk around the gNB

WALK_DEFAULTS = dict(seed=1, speed_kmh=30.0, start_m=60.0, min_m=30.0, max_m=120.0, ref_m=50.0,
                     dl_snr_ref=25.0, ul_snr_ref=25.0, path_loss_exp=37.6, shadow_db=4.0, decorr_m=20.0,
                     heading_sd_deg=30.0, dl_snr_min=8.0, ul_snr_min=5.0)


def gen_walk(p, duration, step):
    rng = random.Random(p["seed"])
    v = p["speed_kmh"] / 3.6
    r0 = min(max(p["start_m"], p["min_m"]), p["max_m"])
    a = rng.uniform(0, 2 * math.pi)
    x, y = r0 * math.cos(a), r0 * math.sin(a)
    heading = rng.uniform(0, 2 * math.pi)
    turn = math.radians(p["heading_sd_deg"]) * math.sqrt(step)  # heading diffuses: sd per sqrt(s)
    shadow = rng.gauss(0, p["shadow_db"])
    n = int(round(duration / step))
    for k in range(n):
        d = max(math.hypot(x, y), 1.0)
        loss = p["path_loss_exp"] * math.log10(d / p["ref_m"]) - shadow
        yield (k * step, max(p["dl_snr_ref"] - loss, p["dl_snr_min"]), max(p["ul_snr_ref"] - loss, p["ul_snr_min"]),
               d)
        # move
        heading += rng.gauss(0, turn)
        nx, ny = x + v * step * math.cos(heading), y + v * step * math.sin(heading)
        r = math.hypot(nx, ny)
        if r > p["max_m"] or r < p["min_m"]:
            # bounce off the ring: reflect the radial part of the heading, stay on the old point
            ux, uy = x / max(d, 1e-9), y / max(d, 1e-9)
            hx, hy = math.cos(heading), math.sin(heading)
            dot = hx * ux + hy * uy
            heading = math.atan2(hy - 2 * dot * uy, hx - 2 * dot * ux)
        else:
            x, y = nx, ny
        # shadowing decorrelates with the distance travelled (Gudmundson)
        rho = math.exp(-v * step / p["decorr_m"]) if p["decorr_m"] > 0 else 0.0
        shadow = rho * shadow + math.sqrt(max(1 - rho * rho, 0.0)) * rng.gauss(0, p["shadow_db"])


# ---- cqi: a CQI process mapped to DL SNR through the measured SNR -> CQI calibration

CQI_DEFAULTS = dict(process="walk", lo=4.0, hi=15.0, dwell_s=0.1, p_move=0.6, period_s=20.0, src="",
                    src_dt_ms=1.0, map="rescale", jitter_db=0.3, ul_snr=20.0, snr_floor=0.3, seed=1)
STAIR_DEFAULTS = dict(snr_lo=0.0, snr_hi=24.0, snr_step=1.0, dwell_s=2.0, ul_snr=20.0)
STRING_KEYS = ("process", "src", "map")
CALIB = os.path.join(HERE, "cqi_calib.csv")


def load_calib():
    """{cqi: DL SNR (dB) at the middle of the SNR band in which this UE reports that CQI}."""
    calib = {}
    with open(CALIB) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("cqi"):
                continue
            c, snr = line.split(",")[:2]
            calib[int(c)] = float(snr)
    return calib


def _read_src_cqi(path):
    vals = []
    with open(path if os.path.isabs(path) else os.path.join(HERE, path)) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                vals.append(int(float(line)))
    return vals


def cqi_process(p, duration, rng):
    """CQI values, one per dwell_s."""
    lo, hi = int(p["lo"]), int(p["hi"])
    n = max(1, int(round(duration / p["dwell_s"])))
    proc = p["process"]
    if proc in ("walk", "band"):
        c = rng.randint(lo, hi)
        for _ in range(n):
            yield c
            if rng.random() < p["p_move"]:
                c = min(hi, max(lo, c + rng.choice((-1, 1))))
    elif proc == "iid":
        for _ in range(n):
            yield rng.randint(lo, hi)
    elif proc == "triangle":
        for k in range(n):
            ph = (k * p["dwell_s"] / p["period_s"]) % 1.0
            x = 2 * ph if ph < 0.5 else 2 - 2 * ph
            yield int(round(lo + x * (hi - lo)))
    elif proc == "replay":
        src = _read_src_cqi(p["src"])
        per = max(1, int(round(p["dwell_s"] * 1000 / p["src_dt_ms"])))
        smin, smax = min(src), max(src)
        for k in range(min(n, len(src) // per)):
            win = sorted(src[k * per:(k + 1) * per])
            c = win[len(win) // 2]
            if p["map"] == "rescale":
                c = lo + (c - smin) * (hi - lo) / max(smax - smin, 1)
            yield int(round(min(hi, max(lo, c))))
    else:
        sys.exit(f"cqi: unknown process {proc} (walk band iid triangle replay)")


def gen_cqi(p, duration, step):
    rng = random.Random(p["seed"])
    calib = load_calib()
    for k, c in enumerate(cqi_process(p, duration, rng)):
        snr = calib[c] + (rng.uniform(-p["jitter_db"], p["jitter_db"]) if p["jitter_db"] > 0 else 0.0)
        yield k * p["dwell_s"], max(snr, p["snr_floor"]), p["ul_snr"], c


def gen_staircase(p, duration, step):
    snr, t = p["snr_lo"], 0.0
    while snr <= p["snr_hi"] + 1e-9:
        yield t, snr, p["ul_snr"], snr
        snr += p["snr_step"]
        t += p["dwell_s"]


# ---- recipes and files

def read_recipes():
    recipes = {}
    with open(RECIPES) as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name, mode, *kv = line.split()
            defaults = dict(walk=WALK_DEFAULTS, bounce=BOUNCE_DEFAULTS, cqi=CQI_DEFAULTS, staircase=STAIR_DEFAULTS)
            if mode not in defaults:
                sys.exit(f"{RECIPES}:{n}: mode must be walk, bounce, cqi or staircase, got {mode}")
            p = dict(defaults[mode])
            p.update(duration_s=300.0, step_s=0.01)
            for item in kv:
                k, _, v = item.partition("=")
                if k not in p:
                    sys.exit(f"{RECIPES}:{n}: unknown key {k} for {mode}")
                p[k] = v if k in STRING_KEYS else int(v) if k == "seed" else float(v)
            recipes[name] = (mode, p)
    return recipes


def write_trace(name, mode, p):
    gen = dict(walk=gen_walk, bounce=gen_bounce, cqi=gen_cqi, staircase=gen_staircase)[mode]
    last = dict(cqi="cqi", staircase="step_db").get(mode, "d_m")
    os.makedirs(LIB, exist_ok=True)
    path = os.path.join(LIB, name + ".csv")
    with open(path, "w") as f:
        f.write(HEADER + f",{last}\n")
        for t, dl, ul, d in gen(p, p["duration_s"], p["step_s"]):
            f.write(f"{t:.10g},{dl:.2f},{ul:.2f},{d:.1f}\n" if last != "cqi" else f"{t:.10g},{dl:.2f},{ul:.2f},{d}\n")
    return path


def check_trace(path, allow_low=False):
    """The problems with a trace file: format errors the UE would reject, and SNRs below 0 dB."""
    problems = []
    try:
        with open(path) as f:
            head = f.readline().strip()
            if not (head == HEADER or head.startswith(HEADER + ",")):
                return [f"header must start with '{HEADER}'"]
            prev, rows, low_dl, low_ul = None, 0, 0, 0
            for n, line in enumerate(f, 2):
                if not line.strip():
                    continue
                fields = line.strip().split(",")
                try:
                    t, dl, ul = (float(v) for v in fields[:3])
                except ValueError:
                    return [f"line {n}: expected numbers t_s,dl_snr_db,ul_snr_db"]
                if not all(math.isfinite(v) for v in (t, dl, ul)):
                    return [f"line {n}: not finite"]
                if (prev is None and t != 0) or (prev is not None and t <= prev):
                    return [f"line {n}: t_s must start at 0 and increase"]
                prev, rows = t, rows + 1
                low_dl += dl < 0
                low_ul += ul < 0
    except OSError as e:
        return [str(e)]
    if rows == 0:
        problems.append("no rows")
    if not allow_low and (low_dl or low_ul):
        problems.append(f"{low_dl} rows with DL SNR < 0 dB and {low_ul} with UL SNR < 0 dB: below 0 dB the OAI UE "
                        "reports CQI 15 (signed/unsigned wrap in its CSI code), and a released UE never reconnects "
                        "(--allow-low-snr to replay anyway)")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true", help="write every recipe in scenarios.conf")
    ap.add_argument("--only", action="append", default=[], metavar="NAME", help="write one recipe (repeatable)")
    ap.add_argument("--list", action="store_true", help="list the recipes")
    ap.add_argument("--check", metavar="CSV", help="validate a trace file; exit 1 with the reason if it is bad")
    ap.add_argument("--allow-low-snr", action="store_true", help="with --check: accept SNRs below 0 dB")
    a = ap.parse_args()

    if a.check:
        problems = check_trace(a.check, a.allow_low_snr)
        for p in problems:
            print(f"{a.check}: {p}", file=sys.stderr)
        sys.exit(1 if problems else 0)
    recipes = read_recipes()
    if a.list:
        for name, (mode, p) in recipes.items():
            print(f"{name:<16} {mode:<6} " + " ".join(f"{k}={v:g}" for k, v in p.items()))
        return
    names = list(recipes) if a.all else a.only
    if not names:
        ap.error("nothing to do: --all, --only NAME, --list or --check FILE")
    for name in names:
        if name not in recipes:
            sys.exit(f"no recipe '{name}' in {RECIPES}")
        mode, p = recipes[name]
        path = write_trace(name, mode, p)
        rows = [l.split(",") for l in open(path).readlines()[1:]]
        dl = [float(r[1]) for r in rows]
        extra = ""
        if mode == "cqi":
            cq = [int(r[3]) for r in rows]
            hist = " ".join(f"{c}:{cq.count(c)}" for c in range(min(cq), max(cq) + 1))
            extra = f", CQI {min(cq)}..{max(cq)} [{hist}]"
        print(f"{os.path.relpath(path, HERE)}: {len(dl)} rows, "
              f"DL SNR {min(dl):.1f}..{max(dl):.1f} dB (mean {sum(dl) / len(dl):.1f}){extra}")


if __name__ == "__main__":
    main()
