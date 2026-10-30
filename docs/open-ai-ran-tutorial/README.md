# OpenAI-RAN tutorial — RAN buffer control

Hands-on demos of the `rlc_dl_ctrl` control hook: a jbpf codelet resizing the gNB's downlink
RLC queue at runtime, driven from an xApp. The point each demo makes is the same one — a deep
buffer costs latency that a loss-based sender will happily consume, and capping it is a lever
you can pull per bearer, live, without touching the scheduler.

Sections 1–5 of the top-level [README](../../README.md) build the testbed. Everything here
assumes that is already done.

## The demos

| | [demo1](demo1/README.md) | [demo2](demo2/README.md) |
|---|---|---|
| **Shows** | bufferbloat, and one knob that fixes it | the same knob applied *per UE* |
| **UEs** | 1, ZMQ direct mode (no broker) | 2, ZMQ broker mode, one netns each |
| **Traffic** | a 2 Mbit/s "video call" + a bulk download on the same UE | an independent download per UE — **BBR on UE1, CUBIC on UE2** |
| **Control** | `bufcap` — fixed caps, cell-wide, swapped from a script | `bufctl` — interactive, addressed per `(UE, DRB)` |
| **Driven by** | `buffer-control-experiments/bufcap.sh load 256k` | `jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py` |
| **Runtime** | ~10 min from a stopped cluster | ~12 min |

**demo1** is the argument in its simplest form. One UE, a call sharing a bearer with a
download; the call's RTT climbs to ~600 ms because the download fills the queue. Stepping the
cap down (256k → 128k → 64k → 16k) walks you from "latency fixed, throughput intact" to
"cap below the bandwidth-delay product, throughput collapses". The interesting result is how
wide that useful middle is.

**demo2** makes it per-user, and adds a second variable. Two bearers in the same cell, and
because the two flows use different congestion control they respond differently to the *same*
limit: CUBIC was relying on the deep buffer, so it loses the most throughput and gains the most
latency; BBR paces to its own estimate and barely notices. Two things fall out — a policy can
be set for one user without touching the other, and one cell-wide buffer setting cannot be
right for everyone.

Read demo1 first; demo2 assumes it.

## Quick path (single UE, no walk-through)

Three scripts, run **from the repo base directory**. They derive every path from their own
location, so any clone path and any user works.

```bash
cd "$REPO_ROOT"

bash scripts/setup_bufctl_1ue_demo.sh   # pipeline only: gNB + UE + telemetry, NO traffic (~4-5 min)
bash scripts/traffic.sh start           # then whatever application data you want
bash scripts/status.sh                  # read-only: what is up, attached, or stale
bash scripts/stop_demo.sh               # stop everything and reclaim /tmp/gnb.log
```

Bring-up and traffic are deliberately separate so you can drive your own data on a known-good
pipeline. `bash scripts/restart_demo.sh [seconds]` does both if you just want the stock demo.
`traffic.sh` takes `--dir dl|ul`, `--cc cubic|bbr|reno`, `--time`, `--rate`, `--streams` and
`--udp` — see [`scripts/README.md`](../../scripts/README.md).

`restart_demo.sh` checks its prerequisites *before* stopping anything, so a clone missing the
`ocudu-jbpf` submodule fails immediately rather than tearing down a working pipeline.

Then drive the buffer control by hand:

```bash
python3 jrtc-apps/jrtc_apps/bufctl/bufctl_cli.py
```

```
bufctl> scan                    # discover bearers
bufctl> list                    # UE / DRB / current vs configured limits
bufctl> set 0 1 sdu 512         # clamp the DL SDU queue for UE 0, DRB 1
bufctl> set 0 1 byte 200000     # or clamp by bytes
bufctl> reset 0 1               # back to the configured size
bufctl> quit                    # unloads the codeletset
```

Watch it on Grafana at `:30490` ("RAN User-Plane Telemetry (per-packet)"): RLC occupancy and
queuing latency drop within a few 134 ms buckets while throughput holds.

## Prerequisites and overrides

**Do sections 1–5 once per server.** These scripts drive an existing deployment; they do not
create one. You need the submodules (§1), the cluster (§2), the Open5GS core (§3), the RAN pod
and jrt-controller Helm release (§4) and the built images (§5).

Install the §4 release with **your own** `REPO_ROOT` — `/apps` and `/codelets` are hostPath
mounts rendered from it, and that is what makes these scripts drive *your* checkout.

If your cluster is not named `janus-cluster`, export `K3D_CLUSTER` (the scripts read that, not
§1's `CLUSTER`) or set `KUBECONFIG` yourself. Both are honoured and `KUBECONFIG` wins:

```bash
export K3D_CLUSTER=my-cluster     # or: export KUBECONFIG=/path/to/kubeconfig
```

`bufctl_cli.py` takes the same override as `BUFCTL_CLUSTER`, plus `BUFCTL_NS` and `BUFCTL_POD`
if your namespace or jrt-controller pod differ from `ran` / `jrtc-0`.

After a **cluster stop/start** three non-persistent things must be restored — the ephemeral
containers, the files staged into them, and the SMF↔UPF N4 association. Each demo's README
carries that sequence (demo1: steps 2, 3 and 5).

## Upgrading an existing deployment

If the cluster, core and Helm release are already up from an earlier checkout, `git pull` alone
is **not** enough. Three pieces are not repo content: the gNB image, the built codelet objects,
and the submodule working tree.

```bash
cd "$REPO_ROOT"
git pull                                    # or: git fetch && git reset --hard origin/open-ai-ran-tutorial
git submodule update --init --recursive     # a pull does NOT move a submodule working tree

# 1. RAN source changed -> rebuild and re-import the gNB image (~15 min)
( cd ocudu-jbpf && ./build.sh )
k3d image import ocudu-gnb-jbpf:local -c "$CLUSTER"

# 2. codelet .o files are gitignored, so a pull brings none.
#    demo1 needs bufcap, demo2 needs bufctl; both need upt. Full list: README section 5d.
( cd jrtc-apps/codelets && for d in upt bufctl bufcap badchan; do ./make.sh -d "$d"; done )

# 3. re-create the gNB ephemeral container so it runs the NEW image (README section 6a)
kubectl patch pod -n ran srs-gnb-du1-0 --subresource ephemeralcontainers --type strategic \
  -p "$(cat "$REPO_ROOT/ocudu-jbpf/deploy/ephem_jbpf.json")"

bash scripts/restart_demo.sh
```

**The gNB rebuild is mandatory, not optional.** The buffer-control work changed the RAN itself:
`jbpf_rlc_ctrl_info` gained `cur_sdu_limit`, `new_sdu_limit`, `cfg_byte_limit` and
`cfg_sdu_limit`; the SDU queue gained `clamp_byte_limit`/`clamp_sdu_limit` and an SDU-count
gate in `write()`; and `rlc_tx_am_entity` gained `handle_jbpf_rlc_ctrl()`. The codelet carries
its **own** copy of that struct — it builds against the SDK image, not the RAN source — so the
layout is a wire contract. Against a gNB built before the change the codelet writes
`new_sdu_limit` and reads `cfg_*` past the end of the older struct. It does not degrade
gracefully. `ocudu-jbpf` must sit at the commit this branch records.

**Ephemeral containers are not persistent.** `ocudujbpf`, `durue1` and `grbroker` are layered on
with `kubectl debug` / an `ephemeralcontainers` patch and cannot be replaced in place, so a new
image only takes effect once that container is re-created (step 3, or a pod delete followed by
§6a). A pod restart also wipes anything staged into `/tmp`, which is why the bring-up scripts
re-stage the UE config every run.

No Helm reinstall is needed as long as the release already points at **your** `REPO_ROOT`;
`status.sh` confirms that by saying nothing about `TREE MISMATCH`.

## Two things to know

**Unload when you are done commanding.** While a control codeletset is loaded its hook runs on
*every* downlink packet. `bufctl_cli.py` loads on start and unloads on quit, including on a
crash; `status.sh` prints `loads`/`removes` so you can confirm. Applied limits **survive** the
unload, so `reset` (or `bufcap.sh off`) first if you want the configured sizes back.

**`/apps` and `/codelets` belong to whoever installed the Helm release.** They are hostPath
mounts rendered from that installer's `REPO_ROOT` (`__REPO_ROOT__` in
`jrtc-apps/containers/Helm/k3d-values.yaml`). On your own cluster, install per §4 and they
point at your checkout. On a **shared** host they still point at the original installer's tree,
so commands from another clone get written to a file no xApp reads. `status.sh` and
`bufctl_cli.py` both detect this and print `TREE MISMATCH`; run the CLI from the tree they
name, or re-install the release with your own `REPO_ROOT`.

## See also

| Document | Covers |
|---|---|
| [`docs/control-codelets.md`](../control-codelets.md) | all four control hooks, their contexts and conflicts |
| [`rlc-buffer-control-hook.md`](../scout/rlc-buffer-control-hook.md) | `rlc_dl_ctrl` in depth: mechanism, API, clamping |
| [`buffer-control-experiments/EXPERIMENTS.md`](../../buffer-control-experiments/EXPERIMENTS.md) | the experiments and measured results |
| [`buffer-control-experiments/HOW_TO_RUN.md`](../../buffer-control-experiments/HOW_TO_RUN.md) | commands-only guide for experiments A and B |
| [`scripts/README.md`](../../scripts/README.md) | every script, and the automated sweeps |
