#!/usr/bin/env python3
"""Check that gen_traces.py's bounce mode computes what the UE's channel model computes.

    python3 check_parity.py

Compiles a small driver against the UE's chan_sim.cpp (duranta-ue/radio/zmq, needs g++), runs
chsim::link -- the code the UE runs -- for every bounce recipe in scenarios.conf and a few edge
cases, and compares its DL/UL SNR and distance with gen_traces.bounce_snr at the same air times.
Exit 1 if any value differs by more than 1e-6.
"""
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True  # no __pycache__ next to the traces
import gen_traces as g  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ZMQ = os.path.join(HERE, "..", "duranta-ue", "radio", "zmq")
TOL = 1e-6

# Reads "spec t1 t2 ..." per line; prints "t_air dl_snr ul_snr distance" per time, from chsim::link
DRIVER = r"""
#include "chan_sim.h"
#include <cstdio>
#include <iostream>
#include <sstream>

int main()
{
  constexpr double FS = 1e4;
  constexpr uint64_t ORIGIN = 100;
  std::string line;
  while (std::getline(std::cin, line)) {
    std::istringstream in(line);
    std::string spec;
    in >> spec;
    chsim::config c;
    const std::string err = chsim::parse(spec, c);
    if (!err.empty()) {
      std::fprintf(stderr, "%s: %s\n", spec.c_str(), err.c_str());
      return 1;
    }
    chsim::link dl(chsim::link::dir::dl, c, FS, ORIGIN), ul(chsim::link::dir::ul, c, FS, ORIGIN);
    for (int i = 0; i < 3; i++) { // 1.5 s of signal locks the reference
      std::vector<chsim::cf> a(5000, chsim::cf(0.1f, 0)), b = a;
      dl.process(a.data(), a.size(), ORIGIN);
      ul.process(b.data(), b.size(), ORIGIN);
    }
    double t;
    while (in >> t) {
      const uint64_t t0 = ORIGIN + (uint64_t)std::llround(t * FS);
      chsim::cf a(0.1f, 0), b = a;
      dl.process(&a, 1, t0);
      ul.process(&b, 1, t0);
      std::printf("%.17g %.17g %.17g %.17g\n", dl.st.t_air.load(), dl.st.dl_snr_db.load(), ul.st.ul_snr_db.load(),
                  dl.st.distance_m.load());
    }
  }
  return 0;
}
"""

# Beyond the recipes (ped_3kmh already starts outside its bounce range): fast folding, moving away for
# ever (floors), distances under 1 m, a 64-bit seed
EXTRA = [
    dict(distance=50, speed=200, min_distance=40, max_distance=60, jitter_db=6, seed=12345),
    dict(distance=30, speed=50, dl_snr_ref=20, ul_snr_ref=18, dl_snr_min=6, ul_snr_min=3),
    dict(distance=0.5, speed=0.5, jitter_db=1, seed=7),
    dict(distance=100, speed=120, min_distance=10, max_distance=400, path_loss_exp=30, jitter_db=8,
         seed=18446744073709551615),
]


def spec_of(p):
    keys = ("distance", "speed", "min_distance", "max_distance", "dl_snr_ref", "ul_snr_ref", "path_loss_exp",
            "dl_snr_min", "ul_snr_min", "jitter_db", "seed")
    return ",".join(f"{k}={p[k]!r}" for k in keys) + ",ul_noise_dbfs=-65"


def main():
    cases = [(name, p) for name, (mode, p) in g.read_recipes().items() if mode == "bounce"]
    for i, extra in enumerate(EXTRA):
        p = dict(g.BOUNCE_DEFAULTS)
        p.update(extra)
        cases.append((f"edge_{i + 1}", p))
    # 300 s on the 10 ms grid, then off-grid instants (1.31 ms apart) that land between shadowing knots
    times = [k * 0.01 for k in range(30000)] + [0.0037 + k * 0.0131 for k in range(5000)]

    with tempfile.TemporaryDirectory() as tmp:
        src, exe = os.path.join(tmp, "driver.cpp"), os.path.join(tmp, "driver")
        with open(src, "w") as f:
            f.write(DRIVER)
        subprocess.run(["g++", "-std=c++17", "-O2", "-I", ZMQ, src, os.path.join(ZMQ, "chan_sim.cpp"), "-o", exe],
                       check=True)
        stdin = "".join(spec_of(p) + " " + " ".join(f"{t:.6f}" for t in times) + "\n" for _, p in cases)
        out = subprocess.run([exe], input=stdin, capture_output=True, text=True, check=True).stdout.split("\n")

    worst, n = 0.0, 0
    for name, p in cases:
        case_worst = 0.0
        for _ in times:
            t, dl, ul, d = (float(v) for v in out[n].split())
            n += 1
            pdl, pul, pd = g.bounce_snr(p, t)
            case_worst = max(case_worst, abs(dl - pdl), abs(ul - pul), abs(d - pd))
        print(f"{name:<12} {spec_of(p)}: max |diff| {case_worst:.1e}")
        worst = max(worst, case_worst)
    ok = worst <= TOL
    print(f"{len(cases)} channels x {len(times)} instants: max |diff| {worst:.1e} -> {'OK' if ok else 'MISMATCH'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
