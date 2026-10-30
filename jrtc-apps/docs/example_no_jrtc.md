# Run the first example without *jrt-controller*

We will run the first example without *jrt-controller*. 

To start without *jrt-controller*, we need to set the env variable `USE_JRTC=0`. 
The default environment options are in file __".env"__. 
If you wish to override any of these variables, create a separate file called __".env.local"__, with variable which should be overwritten, for example:-

```sh
USE_JRTC=0
```

Now, let's build the codelets first:

```sh
cd codelets
./make.sh
```

*Note: when this is run you'll see output similar to the following* ...

```sh
ERROR: /nanopb/pb.h:216: Syntax error at '1'
ERROR: /nanopb/pb.h:384: Syntax error at 'sizeof'
ERROR: /usr/include/sys/cdefs.h:298: Syntax error at '\n'
ERROR: /usr/include/sys/cdefs.h:325: Syntax error at '\n'
...
...
WARNING: Could not parse macro "#define t_ta_hist_init_default { 0 , 0 }"
WARNING: Could not parse macro "#define t_pwr_hist_init_default { 0 , 0 }"
...
...
ERROR: Undef "NULL" depends on an unknown identifier "NULL". Undef "NULL" will not be output
ERROR: Undef "NULL" depends on an unknown identifier "NULL". Undef "NULL" will not be output
INFO: Status: Writing to fapi_gnb_rach_stats.py.
INFO: Status: Wrapping complete.
--------- fapi_gnb_rach_stats_collect.cpp ----------------------------------------------
clang++ -O2 -target bpf -Wall -std=gnu++17 -DJBPF_EXPERIMENTAL_FEATURES -DJBPF_DEBUG_ENABLED -D__x86_64__ -fpermissive -Wno-incompatible-pointer-types -Wno-pedantic  -I/src/out/inc -I/src/include -I/nanopb -I/src/external -I/src/external/fmt/include -I/usr/include/c++/13.2.0 -I/usr/include/c++/13.2.0/x86_64-pc-linux-gnu  -c fapi_gnb_rach_stats_collect.cpp -o fapi_gnb_rach_stats_collect.o
/src/out/bin/srsran_verifier_cli fapi_gnb_rach_stats_collect.o || echo "fapi_gnb_rach_stats_collect.cpp: Failed verification"

48:53: Code is unreachable after 48:53
56:61: Code is unreachable after 56:61
60:177: Code is unreachable after 60:177
94:61: Code is unreachable after 94:61
151:61: Code is unreachable after 151:61

1,0.077681
Program terminates within 2067 instructions
```
These errors toward the top bcan actually be ignored.  They are output by the "ctypesgen" command, but are not issues that have any impact.  The codelet's compilation/verification result is shown in the lines ...
```sh
1,0.016758
Program terminates within 333 instructions
``` 
When this starts with "1," it means it is successful.  In failures cases, it will start with "0,".




Next, you need to open 3 terminal windows, one for the srsRAN, one for a message decoder, and one for loading/unloading codelets. 

In each window, set up the environment variables as described [here](../README.md#Preparing-the-Environment).

## Terminal 1

In the srsRAN terminal, start the RAN as shown [here](../README.md#Start-the-srsRAN).

## Terminal 2

In the message decoder window, you need to run the decoder application. 
This is an application to which schemas will be loaded, and it will printed the protobuf decode of messages it receives.
```sh
cd utils
./run_jbpf_decoder.sh
```
For more info on the message decoder, see [here](https://github.com/microsoft/jbpf-protobuf/blob/main/examples/first_example_standalone/README.md).

## Terminal 3

Finally, in the last window, load the schemas and codelets (if you run your srsRAN with sudo, you'll need to run the above command with sudo):
```sh
cd utils
sudo -E ./load.sh -c $JBPF_CODELETS/xran_packets/xran_packets.yaml
```

Expected output:

In the srsRAN window you should see a bunch of messages, including the final confirmation: 
```
Codeletset is loaded OK`
```

In the decoder window, you should see an output like this:
```
INFO[0078] {"timestamp":"1740588245768711680","ulPacketStats":{"dataPacketStats":{"PacketCount":11016,"PrbCount":"716496","packetInterArrivalInfo":{"hist":[0,0,422,2157,1874,4960,1444,0,0,0,1,1,0,54,59,44]}}},"dlPacketStats":{"dataPacketStats":{"PacketCount":60300,"PrbCount":"6391800","packetInterArrivalInfo":{"hist":[45126,96,3,0,0,0,6223,8651,0,0,0,0,0,201,0,0]}},"ctrlPacketStats":{"PacketCount":9692,"packetInterArrivalInfo":{"hist":[7240,971,28,18,16,1,0,0,0,0,1162,58,154,44,0,0]}}}}  streamUUID=75888bf1-b719-d53d-b10c-bccc9719021f
```

This prints various statistics about the xRAN fronthaul packets (see the [collection](codelets/xran_packets/xran_packets_collect.c) and [reporting](codelets/xran_packets/xran_packets_report.c) codelets for more details). 

To unload the codelets, type:
```sh
cd utils
sudo -E ./unload.sh -c $JBPF_CODELETS/xran_packets/xran_packets.yaml
```

