#!/usr/bin/env python3
"""Configs for the mobility-trace test: replay a recorded channel, static RLC cap per run.

    python3 buffer-control-experiments/make_expT.py  -> configs/expT_<trace>_<sdus>sdu.json

Caps are SDU counts x 1470 B: 512 -> 735k, 1024 -> 1470k, 2048 -> 2940k, 4096 -> 5880k.
Each run: cap and trace loaded at t=0 (the trace starts at its second 0 on load), one bulk
CUBIC download from t=3 for the whole trace. No video flow: at the traces' low MCS the link
carries only a few Mbit/s. Latency comes from the download's TCP RTT and the RLC queue delay.
"""
import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SDU_BYTES = 1470
SDUS = [512, 1024, 2048, 4096]
TRACES = ["ped", "veh"]
CC = "cubic"


def trace_len(name):
    return sum(1 for _ in csv.DictReader(open(os.path.join(HERE, "traces", f"{name}.csv"))))


def build(trace, sdus):
    cap = f"{sdus * SDU_BYTES // 1024}k"
    n = trace_len(trace)
    end = 3 + n
    return {
        "name": f"expT_{trace}_{sdus}sdu",
        "title": f"Trace '{trace}' ({n} s), static cap {sdus} SDU x {SDU_BYTES} B = {cap}, {CC}",
        "duration": end, "settle": 3, "window": [8, end],
        "ues": [{"label": "UE", "container": "durue1", "du_index": "auto"}],
        "flows": [{"name": "bulk", "ue": 0, "cc": CC, "start": 3}],
        "events": [{"t": 0, "bufcap": cap}, {"t": 0, "radio": f"trace {trace}"},
                   {"t": end - 1, "radio": "good"}, {"t": end - 1, "bufcap": "off"}],
    }


if __name__ == "__main__":
    for tr in TRACES:
        for s in SDUS:
            c = build(tr, s)
            json.dump(c, open(os.path.join(HERE, "configs", c["name"] + ".json"), "w"), indent=1)
            print(c["name"], c["events"][0]["bufcap"], "duration", c["duration"])
