# The jbpf OCUDU gNB with the UHD driver (USRP B2xx / X3xx / N3xx), for the over-the-air run with
# COTS UEs (docs/run_ota_cots.md). Same build as ocudu-jbpf/docker/Dockerfile
# plus -DENABLE_UHD=ON. ZMQ stays on, and the ocudu-jbpf tree carries the ipc:// patch, so this one
# image also runs the ZMQ system in place of :ipc.
#
#   docker build -t ocudu-gnb-jbpf:uhd -f ota/gnb.Dockerfile ocudu-jbpf
#   docker build -t ocudu-gnb-jbpf:uhd -f ota/gnb.Dockerfile --build-arg JOBS=16 ocudu-jbpf   # shared host
#
# UHD is Ubuntu 24.04's 4.6.0. The B2xx FPGA images are baked in, so a B210 always loads images that
# match this libuhd. An X310 or N310 keeps its FPGA image on the device, and that image has to be one
# UHD 4.6 accepts (uhd_usrp_probe in this container says so).
FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates cmake ninja-build make clang lld \
      libsctp-dev libyaml-cpp-dev libmbedtls-dev libfftw3-dev \
      libgtest-dev libzmq3-dev libuhd-dev pkg-config build-essential \
      libboost-dev libboost-program-options-dev libboost-filesystem-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . /ocudu
WORKDIR /ocudu/build
RUN CC=clang CXX=clang++ cmake -GNinja -DCMAKE_BUILD_TYPE=Release \
      -DENABLE_JBPF=on -DINITIALIZE_SUBMODULES=OFF -DENABLE_ZEROMQ=ON \
      -DENABLE_UHD=ON -DENABLE_DPDK=OFF -DBUILD_TESTS=OFF ..
# Empty JOBS = every core.
ARG JOBS=
# jbpf 3p deps are ExternalProjects (IMPORTED .a) -- build them first so Ninja sees them on disk.
RUN ninja -j"${JOBS:-$(nproc)}" ubpf jbpf-mimalloc jbpf_libck
RUN ninja -j"${JOBS:-$(nproc)}" gnb
# Fail here, not on the testbed, if CMake silently dropped the UHD driver.
RUN ls -la /ocudu/build/apps/gnb/gnb && ldd /ocudu/build/apps/gnb/gnb | grep libuhd

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      libsctp1 libyaml-cpp0.8 libmbedtls14t64 libmbedcrypto7t64 libmbedx509-1t64 \
      libfftw3-single3 libfftw3-double3 libatomic1 libzmq5 \
      libboost-program-options1.83.0 libboost-filesystem1.83.0 \
      libuhd4.6.0t64 uhd-host usbutils ca-certificates \
      iproute2 iputils-ping iperf3 net-tools \
    && uhd_images_downloader -t b2xx \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /ocudu/build/apps/gnb/gnb /usr/local/bin/gnb
CMD ["sleep","infinity"]
