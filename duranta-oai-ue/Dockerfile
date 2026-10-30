# Duranta OAI nr-UE (the OCUDU tutorial's supported UE) with the ZMQ radio.
# Mainline oai-nr-ue has OCUDU interop bugs (DCI 1_1 crash, re-RA loop); the
# Duranta fork fixes them. Build nr-uesoftmodem + oai_zmqdevif from the
# duranta-ue submodule (github.com/ucsdwcsng/open-ai-ran-duranta-ue); build context = this dir.
FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive TZ=UTC
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates sudo build-essential cmake ninja-build \
      pkg-config libzmq3-dev python3 xxd && rm -rf /var/lib/apt/lists/*
COPY duranta-ue /oai
WORKDIR /oai/cmake_targets
# install OAI build deps (apt-based; -y via the script)
RUN ./build_oai -I --install-optional-packages || ./build_oai -I
# build nr-UE with the ZMQ radio
RUN ./build_oai --nrUE -w ZMQ --ninja

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      libzmq5 libsctp1 iproute2 iputils-ping iperf3 net-tools libyaml-cpp0.8 \
      libconfig9 libblas3 liblapack3 libatlas3-base \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /oai/cmake_targets/ran_build/build/nr-uesoftmodem /opt/oai-nr-ue/bin/nr-uesoftmodem
COPY --from=build /oai/cmake_targets/ran_build/build/*.so* /usr/local/lib/
RUN ldconfig || true
CMD ["sleep","infinity"]
