# Run the second example (with *jrt-controller*)

In this example we will use xRAN codelets from the [example](./example_no_jrtc.md) without *jrt-controller*, and we will feed their input into a sample *jrt-controller* app. 

**Important:** There are several artifacts of the build that have to be rebuilt for different environments.   
If you switch from the example that doesn't use *jrt-controller* to the example that does, you need to make sure all of them are rebuilt. 
We advise you to do a clean clone of the repo in that case.

To build jrt-controller, follow these [instructions](https://github.com/microsoft/jrt-controller/blob/main/README.md)


### Configuring the components

To use *jrt-controller* with the examples in this repo, we need to set the env variable `USE_JRTC=1`. 
The default environment options are in file __".env"__. 
If you wish to override any of these variables, create a separate file called __".env.local"__, with variable which should be overwritten, for example:-

```sh
USE_JRTC=1
```

These overrides will occur when __set_vars.sh__ is executed.
You also need to define a path to your instance of jrt-controller, e.g.:

```sh
export JRTC_PATH=~/jrt-controller/
```

and then set the environment variables as described [here](./baremetal.md#set-required-environment-variables)


Next, you need to modify the srsRAN config file with the following section:
```yaml
jbpf:
  jbpf_run_path: "/tmp"
  jbpf_namespace: "jbpf"
  jbpf_enable_ipc: 1
  jbpf_standalone_io_out_ip: "127.0.0.1"
  jbpf_standalone_io_out_port: 20788
  jbpf_standalone_io_in_port: 30400
  jbpf_standalone_io_policy: 0
  jbpf_standalone_io_priority: 0
  jbpf_io_mem_size_mb: 1024
  jbpf_ipc_mem_name: "jrt_controller"
  jbpf_enable_lcm_ipc: 1
  jbpf_lcm_ipc_name: "jbpf_lcm_ipc"
  jbpf_agent_cpu: 0
  jbpf_agent_policy: 1
  jbpf_agent_priority: 30
  jbpf_maint_cpu: 0
  jbpf_maint_policy: 0
  jbpf_maint_priority: 0
```



#### Running the example

To run the example, we need 5 terminals. 
In each of these, you need to set the following environment variables:

```sh
export JRTC_PATH=~/jrt-controller/
export SRSRAN_DIR=~/srsRAN_Project_jbpf/
export SRSRANAPP_DIR=~/jrtc-apps/
export JRTC_APPS=$SRSRANAPP_DIR/jrtc_apps/

source $SRSRANAPP_DIR/set_vars.sh
```
The example uses sample path values. 
Please edit for your system.
Next, run the following in the terminals. 

##### Terminal 1

Run *jrt-controller*:

```sh
cd $JRTC_PATH/
source setup_jrtc_env.sh

cd $JRTC_PATH/out/bin
./jrtc
```

##### Terminal 2

Run *srsRAN*: 
```sh
cd  $SRSRAN_DIR/build/apps/gnb
sudo ./gnb -c modified_conf_to_include_jbpf.yml
```

##### Terminal 3

Run the *jrt-controller* decoder (see [here](https://github.com/microsoft/jrt-controller/blob/main/docs/understand_example_apps.md) for more info): 

```sh
cd $JRTC_PATH/sample_apps/advanced_example
./run_decoder.sh
```

##### Terminal 4

Run the *jbpf* reverse proxy (see [here](https://github.com/microsoft/jbpf/tree/main/examples/reverse_proxy) for more info):
```sh
sudo -E $SRSRAN_DIR/out/bin/srsran_reverse_proxy --host-port 30450 --address "/tmp/jbpf/jbpf_lcm_ipc"
```

##### Terminal 5

Load the *jbpf* codelets and *jrt-controller* apps for *srsRAN*:

First build the codelets:
```sh
cd $SRSRANAPP_DIR/codelets
./make.sh -o cleanall
./make.sh
```

**Load and unload the [xran_packets](../jrtc_apps/xran_packets/) examples:**

Before loading the codelets, make sure that following ports are configured for decoder and app in deployment.yaml. 


*$JRTC_APPS/xran_packets/deployment.yaml*

```
decoder:
  - type: decodergrpc
    port: 20789

app:
  - name: app1
    path: ${JRTC_APPS}/xran_packets/xran_packets.py
    type: python
    port: 3001
```

To load the deployment: 
```
cd $JRTC_APPS
./load.sh -y xran_packets/deployment.yaml
```

**Expected output:**

On successful execution, we should see the following print in srsRAN logs - `Codeletset is loaded OK`

Sample decoder output: 

```
INFO[0211] REC: {"timestamp":"1745610684298261504","ulPacketStats":{"dataPacketStats":{"PacketCount":0,"PrbCount":"0","packetInterArrivalInfo":{"hist":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}}},"dlPacketStats":{"dataPacketStats":{"PacketCount":60384,"PrbCount":"16484832","packetInterArrivalInfo":{"hist":[45213,74,1,0,0,0,578,14317,0,0,0,0,0,201,0,0]}},"ctrlPacketStats":{"PacketCount":9252,"packetInterArrivalInfo":{"hist":[7000,879,44,8,12,2,0,0,0,0,1106,0,100,101,0,0]}}}}  streamUUID=001013d8-2e92-aa15-1cfa-732f0a2f6ec2
```

Sample output on jrtc terminal:

```
Hi App 1: timestamp: 1745610625939554560
DL Ctl: 9252 [7002, 872, 36, 16, 17, 1, 0, 0, 0, 0, 1106, 0, 101, 101, 0, 0]
DL Data: 60300 16461900 [45136, 83, 3, 3, 0, 0, 448, 14426, 0, 0, 0, 0, 0, 201, 0, 0]
```


To unload the deployment:

```
./unload.sh -y xran_packets/deployment.yaml
```

**Load and unload of [fapi](../jrtc_apps/fapi/) examples**

Again, make sure that decoder port is configured correctly in in fapi deployment.yaml. 

*$JRTC_APPS/fapi/deployment.yaml*
```
decoder:
  - type: decodergrpc
    port: 20789
```


To load the deployment:
```sh

cd $JRTC_APPS
./load.sh -y $JRTC_APPS/fapi/deployment.yaml
```

Unload the deployment
```sh

./unload.sh -y $JRTC_APPS/fapi/deployment.yaml
```
