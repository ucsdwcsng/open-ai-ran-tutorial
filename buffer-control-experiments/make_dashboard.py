#!/usr/bin/env python3
"""Create (or replace) the Grafana dashboard for experiment A.

    python3 buffer-control-experiments/make_dashboard.py
    -> http://localhost:30490/d/expA-bufcap

Panels, top to bottom:
  now:        video-call RTT · download throughput · cap in force
  latency:    video RTT vs download RTT vs RLC queuing delay, 150 ms reference
  throughput: per flow
  buffer:     RLC queue occupancy (bucket avg + max) vs the cap in force
Every `bufcap.sh load/off/unload` shows as an annotation line across all panels.
Data: flowmon.py (flow_*), upt codelets (upt_rlc_*), bufcap.sh (bufcap_limit_kb).
The RLC panels take the max over UEs: only the UE carrying the flows (traffic_A.sh --ue, default 1)
has DRB1 packets, so this is its queue whatever DU index the gNB gave it.
"""
import base64
import json
import urllib.request

GRAFANA = "http://localhost:30490"
DS = {"type": "prometheus", "uid": "victoriametrics"}
VIDEO, BULK, GREY, INK = "#2a78d6", "#eb6834", "#8a8984", "#0b0b0b"


def rlc(metric):
    return f'max without (ue) ({metric}{{bearer="DRB1"}})'


def target(expr, legend, ref):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def color(name, c, **extra):
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]
    props += [{"id": k, "value": v} for k, v in extra.items()]
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def ts(title, y, h, targets, unit, overrides, desc="", x=0, w=24, **fc):
    # same drawing style as the upt-userplane dashboard: light area fill, no gap bridging
    defaults = {"unit": unit, "min": 0, "custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never",
                                                     "spanNulls": False}}
    defaults["custom"].update(fc.pop("custom", {}))
    defaults.update(fc)
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides},
            # same legend as the upt-userplane dashboard: mean and max of each series
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True,
                                   "calcs": ["mean", "max"]},
                        "tooltip": {"mode": "multi", "sort": "none"}}}


def stat(title, x, expr, unit, steps, desc="", decimals=0, w=8):
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": 0, "w": w, "h": 3},
            "targets": [target(expr, title, "A")],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background",
                        "graphMode": "area", "textMode": "value"}}


panels = [
    stat("Video call RTT now", 0, 'avg_over_time(flow_rtt_ms{flow="video"}[3s])', "ms",
         [{"color": "green", "value": None}, {"color": "#eda100", "value": 100}, {"color": "red", "value": 150}],
         "TCP RTT of the 2 Mbit/s paced video flow. Above ~150 ms a call feels laggy."),
    stat("Download throughput now", 8, 'avg_over_time(flow_tput_mbps{flow="bulk"}[3s])', "Mbits",
         [{"color": "#2a78d6", "value": None}], decimals=1),
    stat("Buffer cap in force", 16, 'last_over_time(bufcap_limit_kb[24h]) * 1024', "bytes",
         [{"color": "#52514e", "value": None}],
         "Set by bufcap.sh. No cap = the gNB's configured RLC queue (OCUDU default 6172672 bytes).", w=4),
    stat("Channel: DL MCS ceiling", 20, 'last_over_time(badchan_mcs_cap[24h]) or vector(28)', "none",
         [{"color": "red", "value": None}, {"color": "#eda100", "value": 11}, {"color": "green", "value": 28}],
         "Set by badchan.sh (experiment B). 28 = normal channel; lower = worse channel.", w=4),
    ts("Latency: video call vs download vs gNB queue", 3, 8, [
        target('flow_rtt_ms{flow="video"}', "video call RTT", "A"),
        target('flow_rtt_ms{flow="bulk"}', "download RTT", "B"),
        target(rlc('upt_rlc_queue_latency_ms'), "RLC queuing delay at the gNB", "C"),
    ], "ms", [
        color("video call RTT", VIDEO, **{"custom.lineWidth": 3}),
        color("download RTT", BULK, **{"custom.lineWidth": 1}),
        color("RLC queuing delay at the gNB", GREY,
              **{"custom.lineStyle": {"fill": "dot", "dash": [2, 4]}, "custom.lineWidth": 2}),
    ], "Both flows share the UE's single RLC queue, so they see the same queuing delay. "
       "The dashed line is 150 ms.",
       x=0, w=12,
       thresholds={"mode": "absolute", "steps": [{"color": "transparent", "value": None},
                                                 {"color": "red", "value": 150}]},
       custom={"thresholdsStyle": {"mode": "dashed"}}),
    ts("Throughput per flow", 3, 8, [
        target('avg_over_time(flow_tput_mbps{flow="bulk"}[1s])', "download", "A"),
        target('avg_over_time(flow_tput_mbps{flow="video"}[1s])', "video call", "B"),
    ], "Mbits", [color("download", BULK), color("video call", VIDEO)], x=12, w=12),
    ts("RLC buffer: occupancy vs cap in force", 11, 7, [
        target(rlc('upt_rlc_buffer_bytes_avg'), "RLC queue occupancy (avg)", "A"),
        target(rlc('upt_rlc_buffer_bytes_max'), "RLC queue occupancy (max)", "C"),
        target('last_over_time(bufcap_limit_kb[24h]) * 1024', "cap in force", "B"),
    ], "bytes", [
        color("RLC queue occupancy (avg)", "#8a8984", **{"custom.fillOpacity": 35, "custom.lineWidth": 1}),
        color("RLC queue occupancy (max)", "#8a8984",
              **{"custom.lineStyle": {"fill": "dash", "dash": [6, 4]}, "custom.lineWidth": 1}),
        color("cap in force", INK, **{"custom.lineInterpolation": "stepAfter", "custom.lineWidth": 2}),
    ], "Per-134 ms-bucket average (filled) and max (dashed), as on the upt dashboard. "
       "Once a cap is loaded the max should touch it and the average sit below it.")
]

dash = {
    "uid": "expA-bufcap", "title": "Exp A/B: buffer knob and bad channel", "tags": ["scout", "control"],
    "time": {"from": "now-5m", "to": "now"}, "refresh": "1s", "schemaVersion": 39,
    "annotations": {"list": [{
        "name": "bufcap changes", "enable": True, "iconColor": "#0b0b0b",
        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "target": {"type": "tags", "tags": ["bufcap"], "limit": 100, "matchAny": True}},
        {"name": "channel changes", "enable": True, "iconColor": "red",
         "datasource": {"type": "grafana", "uid": "-- Grafana --"},
         "target": {"type": "tags", "tags": ["badchan"], "limit": 100, "matchAny": True}}]},
    "panels": panels,
}
req = urllib.request.Request(f"{GRAFANA}/api/dashboards/db", method="POST",
                             data=json.dumps({"dashboard": dash, "overwrite": True}).encode(),
                             headers={"Content-Type": "application/json",
                                      "Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()})
print(json.load(urllib.request.urlopen(req)))
print(f"-> {GRAFANA}/d/expA-bufcap")
