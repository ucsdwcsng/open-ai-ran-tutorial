# Codelet load / unload — why the forwarder exists, and how to debug failures

Codeletsets are loaded and unloaded with `jrtc-ctl`, run inside `jrtc-0`. This document covers
the one structural quirk that shapes the whole workflow, and the failures you will actually hit.

Command reference for day-to-day use is README §8. This is the *why* and the debugging.

## The quirk: `jrtc-ctl` hardcodes `127.0.0.1`

`jrtc-ctl` talks to the jbpf agent at a hardcoded `127.0.0.1:30450`. In Kubernetes the agent's
reverse proxy is reachable by **service name**, not localhost, so a load gets as far as
registering the app and then dies:

```
INFO[0000] loaded app                        id=6 startTime="..."
TRAC[0000] sending http request              method=POST url="http://127.0.0.1:30450"
Error: Post "http://127.0.0.1:30450": dial tcp 127.0.0.1:30450: connect: connection refused
```

Note what that leaves behind: the **app is registered but its codelets are not deployed**. A
partial load, not a clean failure.

The fix is `telemetry/fwd30450.py` (older copy: `ocudu-jbpf/deploy/fwd.py`) — a small forwarder
run inside `jrtc-0` that bridges `127.0.0.1:30450` to
`srs-gnb-du1-proxy.ran.svc.cluster.local:30450`. With it running, `jrtc-ctl` works unmodified.
**Every bring-up script starts it**, and README §8's first gotcha ("`fwd.py` must be running or
the load fails `connection refused`") is this section in one line.

```bash
kubectl cp telemetry/fwd30450.py ran/jrtc-0:/tmp/fwd30450.py -c jrtc
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c \
  'pkill -x python3 2>/dev/null; nohup setsid python3 -u /tmp/fwd30450.py >/tmp/fwd30450.log 2>&1 </dev/null & disown
   sleep 2; cat /tmp/fwd30450.log'
```

Confirm it is listening (`76F2` is 30450 in hex):

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'grep -qi 76F2 /proc/net/tcp && echo LISTENING || echo DOWN'
```

## Endpoints and containers

Set `KUBECONFIG` first in any new shell — do not hardcode a path:

```bash
export KUBECONFIG="$(k3d kubeconfig write janus-cluster)"
```

| Service | Address |
|---|---|
| JRTC control API | `jrtc-service.ran.svc.cluster.local:3001` (in-pod: `127.0.0.1:3001`) |
| jbpf reverse proxy | `srs-gnb-du1-proxy.ran.svc.cluster.local:30450` |
| JRTC decoder | `jrtc-decoder.ran.svc.cluster.local:20789` |

`srs-gnb-du1-0` has three standing containers — `troubleshooter`, `gnb`, `srs-gnb-proxy` — plus
the ephemeral ones added at bring-up (`ocudujbpf`, `durue1`, `grbroker`). The OCUDU jbpf gNB
runs in **`ocudujbpf`**, not `gnb`.

## Normal load / unload

```bash
JRTC() { kubectl exec -n ran jrtc-0 -c jrtc -- bash -c \
  "export JRTC_APPS=/apps JBPF_CODELETS=/codelets; /jrtc/out/bin/jrtc-ctl $*"; }

JRTC 'load   -c /apps/upt/deployment_demo.yaml'
JRTC 'unload -c /apps/upt/deployment_demo.yaml'
```

`JRTC_APPS=/apps` and `JBPF_CODELETS=/codelets` are required; both are hostPath mounts of the
repo, so `/apps/x` is `jrtc-apps/jrtc_apps/x` on the host.

### What success looks like

1. `loaded app id=N`
2. `successfully upserted proto package` (one per package)
3. `successfully associated stream ID with proto package`
4. `201 Created` from the reverse proxy — the codelets actually deployed
5. the app appears in `curl http://127.0.0.1:3001/app`

Verify from **both** ends — the xApp side says what was requested, the gNB says what was applied:

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- curl -s http://127.0.0.1:3001/app | grep -oE '"id":[0-9]+'
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- \
  grep -aoE "Registered codelet [a-z_0-9]+ to hook [a-z_0-9]+" /tmp/gnb.stdout | tail
```

## Common failures

### `400 Bad Request` on `POST /app`

Almost always **the app is already loaded**. The proto packages upsert fine and only the app
registration fails, which is the tell. Unload first, then reload — or just leave it, since the
running instance is the one you wanted.

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- curl -s http://127.0.0.1:3001/app | grep -oE '"id":[0-9]+'
```

The controller log names it outright: `Python App <name> ... is already loaded`.

### `connection refused` on `127.0.0.1:30450`

The forwarder is not running. Start it (above). Remember the load was **partial** — the app may
be registered with no codelets behind it, so unload before retrying.

### Unload fails but the app disappeared

```
INFO[0000] unloaded app
Error: Delete "http://127.0.0.1:30450/...": connect: connection refused
```

App gone, codelets still attached in the gNB. Either restart the forwarder and unload again, or
remove the codeletset directly:

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- \
  curl -s -X DELETE http://srs-gnb-du1-proxy.ran.svc.cluster.local:30450/<codeletset_id>
```

Unloading also fails if the **gNB is already dead** — `jrtc-ctl` needs a live agent to detach
from. Unload before killing the radio; `scripts/stop_demo.sh` does them in that order.

### Telemetry stops mid-run, and reloading xApps does not help

Check whether `jrtc-0` restarted underneath you:

```bash
kubectl get pod -n ran jrtc-0 -o jsonpath='{.status.containerStatuses[*].restartCount}{"\n"}'
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -c "Error reserving buffer" /tmp/gnb.stdout
```

A non-zero restart count plus a climbing `Error reserving buffer` count means the gNB still
holds jbpf IPC channels pointing at the dead controller's shared memory. Codelets stay listed
as registered and telemetry is generated and discarded. **No amount of xApp reloading fixes
this** — the gNB must restart. The bring-up scripts avoid it by recreating `jrtc-0` *first*,
so the IPC peer is always fresh.

### Stream associations disappear

```
INFO association removed        protoMsg=... streamUUID=...
DEBU no association found for stream UUID
```

The jbpf agent stopped, or jbpf IPC is off in the gNB config. Check the running config's
`jbpf_enable_ipc` and that the LCM socket exists:

```bash
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- grep -A12 "^jbpf:" /tmp/gnb_ocudu_jbpf.yml
kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- ls -la /tmp/jbpf/
```

`jbpf_enable_ipc: 1` and `jbpf_enable_lcm_ipc: 1` are both required. The config is staged to
`/tmp/` by the bring-up scripts, so fix it in `ocudu-jbpf/deploy/gnb_ocudu_jbpf.yml` and
re-run the script rather than editing in place.

### Other

| Symptom | Cause |
|---|---|
| `Empty reply from server` | wrong endpoint or malformed payload |
| load succeeds, no codelets in the gNB log | the codeletset's `.o`/serde is missing from `/codelets/<dir>` in the **`ocudujbpf`** container |
| a second codelet on a control hook fails | control hooks are exclusive — see [`docs/control-codelets.md`](control-codelets.md) |

### Wedged app loader

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- curl -s -X DELETE http://127.0.0.1:3001/app/<n>
```

Avoid rapid load/unload churn; it can wedge the loader and stress the jbpf LCM.

---

## Appendix: manual codeletset deployment (historical)

Before the forwarder existed, the workaround was to POST the codeletset descriptor straight at
the reverse proxy, bypassing `jrtc-ctl`. **You should not need this** — it is recorded because
it shows what `jrtc-ctl` actually sends, which is useful when a load fails in a way the logs do
not explain.

```bash
kubectl exec -n ran jrtc-0 -c jrtc -- bash -c 'cat > /tmp/codelet_payload.json << "EOF"
{
  "codelet_descriptor": [
    { "codelet_path": "/codelets/xran_packets/xran_packets_collect.o",
      "hook_name": "capture_xran_packet", "codelet_name": "collector", "priority": 1 },
    { "codelet_path": "/codelets/xran_packets/xran_packets_report.o",
      "hook_name": "report_stats", "codelet_name": "reporter", "priority": 2,
      "linked_maps": [ { "linked_codelet_name": "collector",
                         "linked_map_name": "output_tmp_map", "map_name": "output_tmp_map" } ],
      "out_io_channel": [ { "name": "output_map",
                            "stream_id": "000813d82e92aa151cfa732f0a2f6ec2",
                            "serde": { "file_path": "/codelets/xran_packets/xran_packet_info:packet_stats_serializer.so" } } ] }
  ],
  "codeletset_id": "xran_packets"
}
EOF'

# deploy  -> expect 201 Created
kubectl exec -n ran jrtc-0 -c jrtc -- curl -s -X POST -H "Content-Type: application/json" \
  http://srs-gnb-du1-proxy.ran.svc.cluster.local:30450 --data @/tmp/codelet_payload.json
# remove  -> expect 200 OK
kubectl exec -n ran jrtc-0 -c jrtc -- \
  curl -s -X DELETE http://srs-gnb-du1-proxy.ran.svc.cluster.local:30450/xran_packets
```

Setting `jbpf.device.host` to the proxy service name in a deployment YAML does *not* help on its
own — `jrtc-ctl`'s hardcoded default wins. That is the whole reason the forwarder exists.

## Related

- README §8 — load/unload command reference
- [`docs/control-codelets.md`](control-codelets.md) — the four control hooks and their exclusivity
- README §6, *after a cluster (re)start* — bring-up after a cluster restart
