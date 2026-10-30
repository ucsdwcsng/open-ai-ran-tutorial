#!/usr/bin/env python3
"""Static vs dynamic RLC cap under a changing channel (PDCP discard timer = infinity).

    python3 buffer-control-experiments/make_expD.py   -> configs/expD_<scenario>_<policy>.json

Scenarios (channel, replayed in the gNB by badchan, wall-clock timed):
  fade  MCS <= 10 for 200 ms every 1 s, 120 s
  ped   Amarisoft pedestrian trace (81 s)       veh  vehicular trace (100 s)
Policies: static caps, and "dyn" = bufdyn (cap = mu(MCS) x 50 ms, padded after every drop).
One CUBIC download per run, no video flow, so the window's download is the total TCP throughput.
"""
import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCEN = {
    "fade": {"radio": "10 fade 200 1000", "len": 120,
             "policies": ["64k", "128k", "203k", "289k", "dyn"]},
    "ped": {"radio": "trace ped", "len": None,
            "policies": ["128k", "735k", "1470k", "2940k", "5880k", "dyn"]},
    "veh": {"radio": "trace veh", "len": None,
            "policies": ["128k", "735k", "1470k", "2940k", "5880k", "dyn"]},
}
SDU_NAME = {"735k": "512 SDU", "1470k": "1024 SDU", "2940k": "2048 SDU", "5880k": "4096 SDU"}


def trace_len(name):
    return sum(1 for _ in csv.DictReader(open(os.path.join(HERE, "traces", f"{name}.csv"))))


def build(scen, pol):
    sc = SCEN[scen]
    n = sc["len"] or trace_len(scen)
    end = 3 + n
    label = "dynamic (mu x 50 ms + pad)" if pol == "dyn" else f"static {pol}" + (
        f" = {SDU_NAME[pol]}" if pol in SDU_NAME else "")
    return {
        "name": f"expD_{scen}_{pol}",
        "title": f"{scen}: {label}, CUBIC, PDCP discard off",
        "scenario": scen, "policy": pol,
        "duration": end, "settle": 3, "window": [8, end],
        "ues": [{"label": "UE", "container": "durue1", "du_index": "auto"}],
        "flows": [{"name": "bulk", "ue": 0, "cc": "cubic", "start": 3}],
        "events": [{"t": 0, "bufcap": pol}, {"t": 0, "radio": sc["radio"]},
                   {"t": end - 1, "radio": "good"}, {"t": end - 1, "bufcap": "off"}],
    }


if __name__ == "__main__":
    names = []
    for scen, sc in SCEN.items():
        for pol in sc["policies"]:
            c = build(scen, pol)
            json.dump(c, open(os.path.join(HERE, "configs", c["name"] + ".json"), "w"), indent=1)
            names.append(c["name"])
    print(len(names), "configs:", " ".join(names))
