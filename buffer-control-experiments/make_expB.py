#!/usr/bin/env python3
"""Generate the static-vs-dynamic buffer configs for experiment B from ONE radio schedule.

    python3 buffer-control-experiments/make_expB.py  -> configs/expB_static.json, configs/expB_dynamic.json

Both runs use the same radio schedule. Static keeps BASE_CAP throughout. Dynamic adds the
bytes TCP must park when the rate drops:  cap = BASE_CAP + (R_normal - R_now) x RTT.
Ordering per step: channel getting worse -> raise the cap 4 s early, then drop the MCS;
channel getting better -> raise the MCS first, then lower the cap. The radio switches at the
same times in both configs.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_CAP_KB = 128
RTT_S = 0.030
RATE = {"good": 32.5, "16": 16.5, "10": 9.5, "5": 4.6}      # Mbit/s, calibrated 2026-09-22
WARMUP_S, STEP_S = 30, 20
SCHEDULE = ["16", "10", "5", "10", "16", "good"]            # 6 x 20 s = 120 s


def dyn_cap_kb(level):
    extra = (RATE["good"] - RATE[level]) * 1e6 / 8 * RTT_S / 1024
    return int(round(BASE_CAP_KB + extra))


def build(dynamic):
    events, prev = [], "good"
    for i, lvl in enumerate(SCHEDULE):
        t = WARMUP_S + i * STEP_S
        radio = {"t": t, "radio": lvl}
        if not dynamic:
            events.append(radio)
        else:
            worse = RATE[lvl] < RATE[prev]
            # worse: cap goes up 4 s early, so the radio switches at the same t as in the static run
            cap = {"t": t - 4 if worse else t, "bufcap": f"{dyn_cap_kb(lvl)}k"}
            events += [cap, radio] if worse else [radio, cap]
        prev = lvl
    end = WARMUP_S + len(SCHEDULE) * STEP_S
    events.append({"t": end + 3, "bufcap": "off"})
    kind = "dynamic" if dynamic else "static"
    caps = ", ".join(f"{l}:{dyn_cap_kb(l)}k" for l in ["good", "16", "10", "5"])
    return {
        "name": f"expB_{kind}",
        "title": (f"Exp B, {kind} buffer: " + (f"cap follows the radio ({caps})" if dynamic
                  else f"cap fixed at {BASE_CAP_KB}k") + "; radio 16→10→5→10→16→normal, 20 s each"),
        "duration": end + 8, "settle": 5, "window": [WARMUP_S, end],
        "ues": [{"label": "UE", "container": "durue1", "du_index": "auto"}],
        "flows": [{"name": "video", "ue": 0, "cc": "cubic", "rate": "2M", "start": 0},
                  {"name": "bulk", "ue": 0, "cc": "cubic", "start": 3}],
        # cap loaded before traffic starts, so there is no bloat-then-cut transient
        "events": [{"t": 0, "bufcap": f"{BASE_CAP_KB}k"}] + events,
    }


if __name__ == "__main__":
    for dyn in (False, True):
        c = build(dyn)
        path = os.path.join(HERE, "configs", c["name"] + ".json")
        json.dump(c, open(path, "w"), indent=1)
        print(path)
        for e in c["events"]:
            print("   ", e)
