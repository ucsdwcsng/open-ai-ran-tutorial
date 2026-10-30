- [1. Introduction](#1-introduction)
- [2. Getting Started](#2-getting-started)
  - [2.1. Prerequisites](#21-prerequisites)
  - [2.2. Preparing the Environment](#22-preparing-the-environment)
    - [2.2.1. Initialize submodules:](#221-initialize-submodules)
    - [2.2.2. Set required environment variables:](#222-set-required-environment-variables)
    - [2.2.3. There are two ways to start the srsRAN deployment:](#223-there-are-two-ways-to-start-the-srsran-deployment)
    - [2.2.4. Start the srsRAN](#224-start-the-srsran)
      - [2.2.4.1. Deploy RAN without jrtc:](#2241-deploy-ran-without-jrtc)
      - [2.2.4.2. Deploy RAN with jrtc:](#2242-deploy-ran-with-jrtc)
  - [2.3. Running the Examples](#23-running-the-examples)
- [3. Hooks](#3-hooks)
- [4. License](#4-license)


# 1. Introduction

This project provides a collection of sample applications for instrumenting **srsRAN** using the  
[jbpf](https://github.com/microsoft/jbpf) and [jrt-controller](https://github.com/microsoft/jrt-controller) frameworks.

# 2. Getting Started

The simplest way to get started is by using Kubernetes. This is the default installation method and helps eliminate any dependency-related issues.
However, we also support bare-metal installations.
For bare-metal setup instructions, please follow [these instructions](docs/baremetal.md) 

## 2.1. Prerequisites

Before starting, ensure that **Kubernetes** and **Helm** are installed and configured. 
If you are not an expert in Kubernetes, you can try one of the simple, single-node setups, such as [k3d](https://k3s.io/). 
This guide assumes **srsRAN** will be deployed using **Helm**.

## 2.2. Preparing the Environment

### 2.2.1. Initialize submodules:

```bash
cd ~/jbpf_apps
./init_submodules.sh
```

### 2.2.2. Set required environment variables:

```bash
source set_vars.sh
```

### 2.2.3. There are two ways to start the srsRAN deployment:
   - with JRTC - this will create two pods, one for srsRAN, and one for JRTC.  The JRTC pod has two containers; one for the *jrt-controller* and one running the *jrt-decoder*.
   - without JRTC - This will just create a single pod, for srsRAN.

### 2.2.4. Start the srsRAN

Move to the ***jrtc-apps/containers/Helm*** directory and make sure that parameters related to local setup, such as Core IP, RRH, Local MAC Address, and VLAN ID are correctly configured.
These parameters are supplied from the Helm chart `values.yaml` (the default values are [here](./containers/Helm/values.yaml)). 

The easiest way to configure parameters related to local setup is to supply them via seperate yaml file - lets say `config.yaml`. Here is an example `config.yaml`. Please note that `config.yaml` will overwrite parameters in the `values.yaml` file.

  ```
  duConfigs:
    du1:
      cells:
        cell1:
          cellID: 1
          ruNAME: "RRH"
          ruLocalMAC: "00:11:22:33:0a:a6" 
          ruRemoteMAC: "6c:ad:ad:00:0a:a6"
          ruVLAN: "1"
          physicalCellID: 1

  cell_cfg:
    plmn: "00101"

  ngcParams:
    coreIP: 192.168.101.50 
    tac: "000001" 
    plmn: "00101" 
  ```


 Once the parameters are configured correctly we can deploy srsRAN

  
#### 2.2.4.1. Deploy RAN without jrtc:

  ```
  ./install.sh -h . -f config.yaml
  ```

  Expected output:

  ```bash
  kubectl get pods -n ran

  NAME            READY   STATUS    RESTARTS   AGE
  srs-gnb-du1-0   3/3     Running   0          11s
  ```

#### 2.2.4.2. Deploy RAN with jrtc:

  ```
  cd ~/jrtc-apps/containers/Helm
  ./install.sh -h . -f config.yaml -f jrtc.yaml
  ```
 
  Expected output:

  ```bash
  kubectl get pods -n ran

  NAME            READY   STATUS    RESTARTS   AGE
  jrtc-0          2/2     Running   0          11s
  srs-gnb-du1-0   3/3     Running   0          11s
  ```

---

## 2.3. Running the Examples

This project includes two examples:

- [Example 1](./docs/example_no_jrtc.md):  
  Demonstrates data collection without using *jrt-controller*. It runs the [xran_packets app](./jrtc_apps/xran_packets/) that collects fronthaul statistics.  Data is streamed to a local decoder and printed on-screen.

- [Example 2](./docs/example_w_jrtc.md):  
  Demonstrates data collection using *jrt-controller*. It also runs the [xran_packets app](./jrtc_apps/xran_packets/) that collects fronthaul statistics. Data is transferred from srsRAN to *jrt-controller* via shared memory.

- [Example 3](./docs/example_dashboard.md): 
  This is an advanced example that can be used to create a dashboard collecting various statistics from RAN (including throughput, latency, loss, at various layers). 
  The application comprises of a jrt-controller app and multiple codelets. 

---

# 3. Hooks and Codelets

The available Jbpf hooks are described [here](./docs/srsran_hooks.md). Some of the codelets are documented [here](./docs/codelets.md).


# 4. Frequently Asked Questions

**Q:** Can codelets and apps be loaded from a different server or even a different folder? 

**A:** No. At the moment, the [install.sh](./containers/Helm/install.sh) of the Helm chart maps local volumes `codelets_vol_mount` and `codelets_vol_mount` into the srsRAN and jrtc pods respectively, using a predefined folder structure. The loading process is triggered through REST but the apps and codelet source code and object files are loaded from the local volumes, not sent over REST. This is currently done for simplicity, but one can easily modify this design to fully support loading through REST API. 



# 5. License

This project is licensed under the [MIT License](LICENSE.md).

---