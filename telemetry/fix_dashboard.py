#!/usr/bin/env python3
# fix_dashboard.py -- repair the provisioned upt dashboard:
#  p2  RLC latency          : mean green solid, p95 YELLOW dashed
#  p3  RLC occupancy        : legend "UE {ue}" (drop label_replace); avg + max(YELLOW dashed)
#  p1  GTP throughput       : avg + rolling-peak(YELLOW)
#  p11 TCP throughput       : avg + rolling-peak(YELLOW)
#  p12 TCP RTT              : legend "UE {ue}" (drop label_replace); mean + max(YELLOW dashed)
import json, subprocess, copy, sys

cm = json.loads(subprocess.check_output(["kubectl","get","cm","-n","ran","grafana-upt-dashboard","-o","json"]))
key = list(cm["data"])[0]; d = json.loads(cm["data"][key])
YELLOW="#F2CC0C"; GREEN="#56A64B"

def tgt(expr, refid, leg="", instant=False):
    t={"expr":expr,"refId":refid,"legendFormat":leg,"datasource":{"type":"prometheus","uid":"$ds"}}
    if instant: t.update(instant=True, range=False, format="table")
    return t
def solid(name,color): return {"matcher":{"id":"byName","options":name},"properties":[{"id":"color","value":{"mode":"fixed","fixedColor":color}},{"id":"custom.lineStyle","value":{"fill":"solid"}}]}
def dash(name,color):  return {"matcher":{"id":"byName","options":name},"properties":[{"id":"color","value":{"mode":"fixed","fixedColor":color}},{"id":"custom.lineStyle","value":{"fill":"dash","dash":[10,10]}},{"id":"custom.lineWidth","value":2}]}

panels={p.get("id"):p for p in d["panels"]}
# --- keep a template datasource uid from an existing target ---
DS=None
for p in d["panels"]:
    for t in p.get("targets",[]):
        if isinstance(t.get("datasource"),dict) and t["datasource"].get("uid"): DS=t["datasource"]["uid"]; break
    if DS: break
def T(expr,refid,leg="",instant=False):
    t=tgt(expr,refid,leg,instant);
    if DS: t["datasource"]={"type":"prometheus","uid":DS}
    return t

def fix(pid, targets, overrides=None, title=None):
    p=panels[pid]; p["targets"]=targets
    if overrides is not None: p["fieldConfig"]["overrides"]=overrides
    if title: p["title"]=title
    p["options"].setdefault("legend",{}).update({"placement":"bottom","displayMode":"list","showLegend":True})

fix(2, [T('upt_rlc_queue_latency_ms{ue=~"$ue",bearer=~"$bearer"}',"MEAN","UE {{ue}} mean"),
        T('upt_rlc_pkt_lat_p95_ms{ue=~"$ue"}',"P95","UE {{ue}} p95")],
    [solid("UE 0 mean",GREEN), dash("UE 0 p95",YELLOW)])
fix(3, [T('upt_rlc_buffer_bytes_avg{ue=~"$ue",bearer=~"$bearer"}',"AVG","UE {{ue}} avg"),
        T('upt_rlc_buffer_bytes_max{ue=~"$ue",bearer=~"$bearer"}',"MAX","UE {{ue}} max")],
    [solid("UE 0 avg",GREEN), dash("UE 0 max",YELLOW)], "RLC Buffer Occupancy — avg (solid) vs max (dashed)")
fix(12,[T('tcp_rtt_ms{ue=~"$ue"}',"RTT","UE {{ue}} mean"),
        T('tcp_rtt_max_ms{ue=~"$ue"}',"MAX","UE {{ue}} max")],
    [solid("UE 0 mean",GREEN), dash("UE 0 max",YELLOW)], "TCP RTT — mean (solid) vs max (dashed)")
fix(1, [T('upt_gtp_arrival_mbps{ue=~"$ue",bearer=~"$bearer"}',"AVG","UE {{ue}} {{bearer}}"),
        T('max_over_time(upt_gtp_arrival_mbps{ue=~"$ue",bearer=~"$bearer"}[10s])',"PK","UE {{ue}} peak")],
    [solid("UE 0 DRB1",GREEN), dash("UE 0 peak",YELLOW)])
fix(11,[T('tcp_throughput_mbps{ue=~"$ue"}',"TP","UE {{ue}}"),
        T('max_over_time(tcp_throughput_mbps{ue=~"$ue"}[10s])',"PK","UE {{ue}} peak")],
    [dash("UE 0 peak",YELLOW)])

# Panels 30-33 belonged to the retired sweep harness; drop them if present.
d["panels"]=[q for q in d["panels"] if q.get("id") not in (30,31,32,33)]

d.pop("id",None)
cm2={"apiVersion":cm["apiVersion"],"kind":cm["kind"],"metadata":{"name":cm["metadata"]["name"],"namespace":cm["metadata"]["namespace"]},"data":{key:json.dumps(d)}}
open("/tmp/dash_new.json","w").write(json.dumps(cm2))
open(sys.argv[1] if len(sys.argv)>1 else "/tmp/upt-dashboard.json","w").write(json.dumps(d,indent=2))
subprocess.run(["kubectl","replace","-f","/tmp/dash_new.json"],check=True)
print("patched panels 1,2,3,11,12; dropped dead sweep panels 30-33; reload ~30-60s")
