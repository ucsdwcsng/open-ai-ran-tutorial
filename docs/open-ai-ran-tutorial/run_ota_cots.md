# Run over the air with COTS UEs

The same OCUDU jbpf gNB, Open5GS core and telemetry as the [ZMQ system](run_system.md), with a real
radio: a **USRP** drives the cell, and the UEs are **phones or 5G modems** with programmable SIMs.
Everything stays in k3d. The gNB container reaches the USRP directly: a B210 through the kernel's
live USB device tree, an X310 or N310 through a host NIC moved into the gNB pod.

- **Topology:** USRP ↔ gNB container `ocudujbpf` (pod `srs-gnb-du1-0`) ↔ Open5GS; UEs over the air.
- **Cell:** n78, 3489.42 MHz, 20 MHz, 30 kHz SCS, TDD 7 DL : 3 UL, PLMN 99970, TAC 1, PCI 1, SISO.
  The same cell as the ZMQ system, without its ZMQ- and OAI-UE-specific settings.
- **Telemetry:** the same Grafana dashboard and dashboard xApp. Each UE is followed from its SIM's
  IMSI down to its C-RNTI.
- **Runtime:** ~10 min the first time (image build), then ~3–4 min per bring-up.

> **Status (2026-10-09):** built and checked without hardware:
> - the `:uhd` image;
> - all three radio configs: the gNB parses each one and stops at `No devices found`;
> - `setup_ota.sh`'s SDR access, SDR probe, subscriber and gNB-start stages, in a stand-in
>   container and a scratch database. The telemetry stages are the ZMQ setup's.
>
> Not yet run against a USRP and a phone; the first hardware run should replace this note with its
> real output.

> **Spectrum.** 3489.42 MHz is licensed spectrum. Transmit over the air only under a licence (for
> example an FCC experimental licence) or inside a shielded enclosure. Otherwise cable the SDR to
> the UE through 40–60 dB of attenuation and start with a low `tx_gain`.

## Quick start

With the image, the gNB pod, the SIMs and the SDR in place (steps 1–6):

```bash
export PATH="$HOME/.local/bin:$PATH"; export KUBECONFIG="$(k3d kubeconfig write janus-cluster)"

bash scripts/setup_ota.sh --check                       # image, configs, and the SDR answers
bash scripts/setup_ota.sh --telemetry --ues 2           # step 7: UEs out of airplane mode when it asks
bash scripts/gnb_console.sh                             # one row per UE every second: CQI, MCS, BLER
#   traffic: step 8      Grafana: http://localhost:30490/d/upt-userplane  (admin / admin)
bash scripts/stop_demo.sh                               # cell off air
```

For an X310 or N310, add `--sdr x310 --sdr-nic <host NIC>` or `--sdr n310 --sdr-nic <host NIC>`.

## What it uses

| File | Role |
|---|---|
| `ota/gnb.Dockerfile` | the jbpf gNB with the UHD driver (and ZMQ), UHD 4.6 with the B2xx FPGA images → `ocudu-gnb-jbpf:uhd` |
| `ota/gnb_ota_tdd_n78_20mhz.yml` | cell, core and telemetry: everything except the radio |
| `ota/ru_b210.yml`, `ota/ru_x310.yml`, `ota/ru_n310.yml` | the radio, one file per SDR: device, sample rate, gains, clock |
| `ota/sims.conf` | SIMs to add to the core when they don't use the testbed's IMSIs and keys |
| `scripts/setup_ota.sh` | bring-up: stop → subscribers → [`jrtc-0`] → SDR access and probe → gNB → [xApps] → wait for the UEs |
| `scripts/core_ue_info.sh` | each UE's IMSI, IP and NGAP IDs from the core → the dashboard xApp; `--tcp-map` for `tcp_probe.py` |
| `scripts/gnb_console.sh` | the gNB console: per-UE metrics every second, and the SDR's late / underflow / overflow counts |
| `scripts/stop_demo.sh` | stops traffic and the gNB, which takes the cell off air |

The gNB loads two config files: `gnb -c gnb_ota_tdd_n78_20mhz.yml -c ru_<sdr>.yml`. A later file
overrides an earlier one, and so do command-line options. So a gain can be tried without editing
anything: `GNB_ARGS="ru_sdr --tx_gain 60" bash scripts/setup_ota.sh ...`.

## Prerequisites

- **Host setup.** README sections 1–5 done once on this host (cluster, core, RAN Helm release).
- **SDR.**
  - A USRP **B210** on a USB 3 port of this host, or an **X310 / N310** cabled to a 10 GbE port.
  - Two antennas for 3.3–3.8 GHz on the first RF channel: `TX/RX` transmits, `RX2` receives.
  - A GPSDO or 10 MHz reference is optional. COTS UEs lock more reliably to a disciplined clock.
- **UEs.** 5G SA UEs that support n78, or n77 (3489.42 MHz is in both).
  - Modems such as the Quectel RM5xx are the easiest to force into SA.
  - Phones vary: many only use SA on carriers they recognise, so check that yours offers an SA or
    "NR only" mode.
- **SIMs.** Programmable USIMs with their ADM1 key, for example sysmocom sysmoISIM-SJA2/SJA5, plus a
  PC/SC card reader and [pySim](https://gitea.osmocom.org/sim-card/pysim).
- **Host tuning.** On a shared host, agree on these first:
  - CPU governor at `performance`: `sudo cpupower frequency-set -g performance`.
  - For an X310 / N310, large socket buffers. They are global, so they are set on the host, not in
    the pod: `sudo sysctl -w net.core.rmem_max=33554432 net.core.wmem_max=33554432`.
- **User.** Run everything as your own user from the repo root, **not** with sudo.

## 0. Shell setup

Every new terminal:

```bash
cd <your clone>
export PATH="$HOME/.local/bin:$PATH"
export KUBECONFIG="$(k3d kubeconfig write janus-cluster)"
```

## 1. Build and load the UHD gNB image

The `:local` and `:ipc` gNB images are built without UHD. This one adds it:

```bash
docker build -t ocudu-gnb-jbpf:uhd -f ota/gnb.Dockerfile ocudu-jbpf      # ~5 min; --build-arg JOBS=16 on a shared host
k3d cluster start janus-cluster                                         # if it is stopped
k3d image import ocudu-gnb-jbpf:uhd -c janus-cluster
```

The build fails rather than produce a gNB without the UHD driver. The image also has the ZMQ
driver and the `ipc://` patch, so it should run the ZMQ system too. That hasn't been tested;
`:ipc` remains the tested image for it.

Re-run the import whenever you rebuild the image, then redo step 3.

## 2. Fix N4 (SMF → UPF)

After every cluster restart, as in [step 2 of the ZMQ system](run_system.md#2-fix-n4-smf--upf). Skip
it and the UEs register but pass no data.

## 3. Fresh gNB pod with the UHD gNB container

Ephemeral containers cannot be replaced, so the pod is recreated with `ocudujbpf` on the `:uhd`
image. This stops whatever runs on the pod, so on a shared testbed check first.

```bash
kubectl delete pod -n ran srs-gnb-du1-0
until kubectl wait -n ran --for=condition=Ready pod/srs-gnb-du1-0 --timeout=300s 2>/dev/null; do sleep 3; done
kubectl patch pod -n ran srs-gnb-du1-0 --subresource ephemeralcontainers --type strategic \
  -p "$(sed 's/ocudu-gnb-jbpf:local/ocudu-gnb-jbpf:uhd/' ocudu-jbpf/deploy/ephem_jbpf.json)"
sleep 15
kubectl get pod -n ran srs-gnb-du1-0 -o jsonpath='{range .status.ephemeralContainerStatuses[*]}{.name}={.state}{"\n"}{end}'   # ocudujbpf running
```

The over-the-air run needs only `ocudujbpf`. To keep the ZMQ system available on the same pod, also
add `grbroker` and `durue1` with the two `kubectl debug` lines of
[the ZMQ system's step 3](run_system.md#3-fresh-gnb-pod-with-the-broker-ue-and-gnb-containers).

## 4. Program the SIMs

The cell and core serve PLMN 99970, so every IMSI must start with `99970`. The last 9 digits of the
IMSI are the UE's number in Grafana (999700000000003 → UE 3).

**Simplest: use the testbed's subscribers.** Program SIM *k* as IMSI `99970000000000k` with the
testbed's keys. The setup always provisions 999700000000001–008 with these, so the core needs
nothing:

```bash
# pySim, card in reader 0; ADM1 and ICCID come with the card. -t sysmoISIM-SJA2 for SJA2 cards.
./pySim-prog.py -p 0 -t sysmoISIM-SJA5 -a <ADM1> -s <ICCID> -x 999 -y 70 \
  -i 999700000000001 -k 00112233445566778899aabbccddeeff -o 63bfa50ee6523365ff14c1f45f88737d
./pySim-read.py -p 0          # check IMSI and HPLMN
```

**Your own keys:** program the IMSI (still `99970…`) and keep the card's Ki/OPc. Then list each SIM
in a copy of [`ota/sims.conf`](../../ota/sims.conf), one `IMSI Ki OPc` line per SIM, and pass it as
`--sims <file>`. The setup adds each SIM to Open5GS, or updates the keys of an IMSI that already
exists.

Leave SUCI concealment off (the null scheme, the sysmoISIM default). `core_ue_info.sh` reads the IMSI
out of the SUCI to follow the UE in Grafana.

## 5. Prepare the UEs

Put each UE in **airplane mode** first. The cell goes on air the moment the gNB starts, and the
dashboard xApp can only follow a UE that attaches after it is loaded. The setup tells you when.

**Android phone:**
- Insert the SIM and turn Wi-Fi off.
- Add an APN with name and APN `internet` (Settings → Network → SIM → Access point names).
- Set the network type to 5G SA or NR only. On many phones it's under `*#*#4636#*#*` → Phone
  information → preferred network type.
- For traffic, install iperf3 (Termux: `pkg install iperf3`, or an iperf3 app).

**Quectel RM5xx modem** (AT port, e.g. `/dev/ttyUSB2`):

```
AT+QNWPREFCFG="mode_pref",NR5G            NR only
AT+QNWPREFCFG="nr5g_disable_mode",2       NSA off, SA on
AT+QNWPREFCFG="nr5g_band",78
AT+CGDCONT=1,"IPV4V6","internet"
AT+CFUN=0                                 airplane mode until the setup asks; then AT+CFUN=1
AT+QENG="servingcell"                     after attach: NR5G-SA, PCI 1, ARFCN 632628
```

## 6. Connect the SDR

**B210:**
- Plug it into a **USB 3** port and fit the antennas on RF A (`TX/RX`, `RX2`).
- `lsusb | grep -i ettus` on the host lists it.
- Nothing else to do. The container's `/dev` is a copy taken when the k3d node started, so it
  misses a USRP plugged in later, and a B210 that re-enumerates after its firmware load. The setup
  therefore mounts the kernel's live `/dev/bus/usb` in the gNB container. No cluster rebuild is needed.

**X310 / N310:**
- Cable a 10 GbE port of this host to the SDR's SFP+ port. The radio files expect the factory
  addresses: X310 `192.168.40.2` (XG image, port 0), N310 `192.168.20.2` (port 1). Edit `addr=` in
  `ota/ru_<sdr>.yml` for others.
- The pod network (flannel, MTU 1450, NAT) cannot carry UHD's jumbo frames. So `--sdr-nic <NIC>`
  moves that NIC into the gNB pod and sets MTU 9000 and `192.168.40.1/24` (X310) or
  `192.168.20.1/24` (N310); `--sdr-ip` overrides the address.
- The move needs the docker group, not sudo. The NIC goes back to the host when the pod is deleted.
- The device's FPGA image must suit UHD 4.6. If the check below reports an FPGA compatibility error,
  update the device from the same container, so the versions match:
  ```bash
  kubectl exec -n ran srs-gnb-du1-0 -c ocudujbpf -- bash -c \
    'uhd_images_downloader -t x310 && uhd_image_loader --args type=x300,addr=192.168.40.2,fpga=XG'
  ```
  Then power-cycle the X310. An N310 is updated as a whole filesystem; follow Ettus' N3xx
  instructions for UHD 4.6.

Then check, with no gNB running (a running gNB holds the radio):

```bash
bash scripts/setup_ota.sh --check                                     # B210
bash scripts/setup_ota.sh --sdr x310 --sdr-nic ens5f0np0 --check      # X310: moves the NIC, then probes
```

`--check` ends with `CHECK OK.` once the SDR answers. It stops nothing; besides probing, it only
mounts the live USB tree or moves the NIC.

## 7. Bring up the gNB and attach the UEs

```bash
bash scripts/setup_ota.sh --telemetry --ues 2                                    # B210
bash scripts/setup_ota.sh --sdr n310 --sdr-nic ens5f0np0 --telemetry --ues 2     # N310
```

The stages:

1. Stops any run: traffic, the gNB, and ZMQ UEs and broker if present.
2. Provisions subscribers 999700000000001–008, plus the `--sims` file.
3. With `--telemetry`, starts a fresh `jrtc-0`.
4. Gives the gNB container the SDR and probes it with `uhd_find_devices`.
5. Starts the gNB and waits for `==== gNB started ===`, after the N2 connection to the AMF. It
   prints the cell line (`Cell pci=1, bw=20 MHz, 1T1R, dl_arfcn=632628 (n78) …`).
6. With `--telemetry`, loads the codelets, the `upt` xApp and the dashboard xApp.
7. Prints the prompt below and waits up to 5 min (`UE_WAIT=<s>` changes that) for `--ues N` UEs to
   register with a PDU session:

```
  >>> The cell is on air. Take the UE(s) out of airplane mode now. <<<
```

Then take each UE out of airplane mode. A phone may take a minute to find the new network. Manual
network selection (`999 70`) speeds it up. With `--telemetry`, the setup ends with each UE's IMSI,
IP and NGAP IDs, which also go to the dashboard xApp; without it, with each UE's IP and number.

Watch the cell with `bash scripts/gnb_console.sh`: one row per UE every second, with CQI, MCS, BLER
and rates. It also shows lines like `Late: 3; Underflow: 0; Overflow: 0;`, which mean the gNB or the
SDR link missed real time (Troubleshooting).

The setup starts a fresh gNB every time, which drops the UEs. Most reattach on their own; toggle
airplane mode on any that don't.

## 8. Traffic

iperf3 servers on the UPF, one port per UE. The UE runs the client against `10.45.0.1`, the UPF's
address on the UE subnet:

```bash
UPF=$(kubectl get pods -n open5gs --no-headers -o custom-columns=:.metadata.name | grep '^open5gs-upf-')
kubectl exec -n open5gs "$UPF" -c open5gs-upf -- sh -c 'for p in 5201 5202 5203 5204; do iperf3 -s -p $p -D; done'
```

On UE *k*, downlink with `-R`, uplink without:

```bash
iperf3 -c 10.45.0.1 -p 520<k> -R -t 60      # DL
iperf3 -c 10.45.0.1 -p 520<k> -t 60         # UL
```

For a modem, run this on its PC. If that PC has another default route, add
`sudo ip route add 10.45.0.1/32 dev <modem interface>` first. `stop_demo.sh` stops the servers.

## 9. Telemetry

As in the ZMQ system's [step 7](run_system.md#7-telemetry-grafana-and-the-dashboard-xapp): the
Grafana dashboard <http://localhost:30490/d/upt-userplane>, the dashboard xApp's JSON in `jrtc-0`'s
log, and the TCP row once `tcp_probe.py` runs with the core's map:

```bash
MAP=$(bash scripts/core_ue_info.sh --tcp-map); echo "$MAP"
setsid nohup python3 telemetry/tcp_probe.py --map "$MAP" >/tmp/tcp_probe.log 2>&1 </dev/null &
```

A UE that registered before the xApps loaded keeps its native series names (`UE du2`, `UE cu3`).
Toggle its airplane mode to pick up its identity.

## 10. Stop

```bash
bash scripts/stop_demo.sh          # traffic, probes, gNB: the cell goes off air
```

Put the UEs back in airplane mode, or they keep searching for the cell. A NIC moved into the pod
stays there across runs and goes back to the host when the pod is deleted. Back to the ZMQ system:
recreate the pod as in [its step 3](run_system.md#3-fresh-gnb-pod-with-the-broker-ue-and-gnb-containers).

## Gotchas

**Airplane mode until the prompt.** The cell is up before the xApps load. A UE that attaches in that
window is served, but Grafana cannot follow its identity until it attaches again.

**One gNB per pod.** The over-the-air gNB and the ZMQ gNB are the same `ocudujbpf` container, so
each bring-up stops the other mode's run.

**The inactivity timer is 2 h.** A phone stays connected while idle, so it keeps its C-RNTI and its
Grafana identity. With the default 2 min it would go idle and come back as a new UE context.

**Gains are analog dB now.** The ZMQ config's `tx_gain: -12` was a digital scale. Over the air,
`tx_gain` / `rx_gain` set the USRP's amplifiers. The digital back-off for the signal's
peak-to-average ratio stays at the gNB default (`amplitude_control.tx_gain_backoff`, 12 dB).

**A B210 needs USB 3.** On USB 2 it cannot sustain 23.04 MS/s. `uhd_usrp_probe` in the gNB
container shows `Operating over USB 3` when it is right.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `runs ocudu-gnb-jbpf:… which has no UHD driver` | Steps 1 and 3: build and import `:uhd`, recreate the pod. |
| `no b210 answered` | USB 3 port, power, `lsusb \| grep -i ettus` on the host. Unplug and replug, then `--check` again. |
| `the gNB pod cannot reach the x310 / n310` | `--sdr-nic` names the NIC cabled to the SDR; link up (`ethtool <nic>`); SDR address as in `ota/ru_<sdr>.yml`. |
| `OCUDU ERROR: Unable to create radio session` | The SDR is held by another process (a gNB still running) or `device_args` don't match the device. The lines above it name the UHD error. |
| `Expected FPGA compatibility number …` | The X310 / N310 image doesn't suit UHD 4.6: step 6. |
| `Late` / `Underflow` / `Overflow` counts on the console | CPU: performance governor, nothing heavy on the host. B210: a USB 3 port of its own. X310 / N310: MTU 9000 on the pod NIC and the host socket buffers. |
| The UE never sees the network | SA / NR-only mode; n78 support (else `band: 77` in `ota/gnb_ota_tdd_n78_20mhz.yml`); antennas on `TX/RX` and `RX2`; `tx_gain`. Manual network search should list `999 70`. |
| The UE sees `999 70` but registration fails | IMSI / Ki / OPc on the SIM and in the core differ: `kubectl logs -n open5gs deploy/open5gs-amf --tail=50`. A `MAC failure` means wrong keys; `Cannot find SUCI` means the IMSI is not provisioned. |
| `!! 1/2 UEs registered` | Toggle airplane mode on the missing UE. `bash scripts/core_ue_info.sh` lists what the core has. |
| Registered, no data | N4: step 2. The APN / DNN must be `internet`. |
| High BLER, low MCS | Gains (raise `rx_gain` for UL, `tx_gain` for DL), distance, antennas. Without a GPSDO, the CFO compensation in `ru_<sdr>.yml` must stay on. |
