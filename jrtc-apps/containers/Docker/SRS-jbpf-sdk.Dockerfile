# Shell command to build this image:
# docker build -t ghcr.io/microsoft/jrtc-apps/srs-jbpf-proxy:latest -f SRS-jbpf-proxy.Dockerfile .
# docker push ghcr.io/microsoft/jrtc-apps/srs-jbpf-proxy:latest

ARG BASE_IMAGE_TAG=latest
ARG SRS_JBPF_IMAGE_TAG=latest
ARG JBPF_PROTOBUF_BUILDER_IMAGE=jbpf_protobuf_cli       
ARG JBPF_PROTOBUF_BUILDER_IMAGE_TAG=latest

FROM ${JBPF_PROTOBUF_BUILDER_IMAGE}:${JBPF_PROTOBUF_BUILDER_IMAGE_TAG} AS jbpf_protobuf_builder
FROM ghcr.io/microsoft/jrtc-apps/srs-jbpf:${SRS_JBPF_IMAGE_TAG} AS srsran

FROM ghcr.io/microsoft/jrtc-apps/base/srs:${BASE_IMAGE_TAG}

LABEL org.opencontainers.image.source="https://github.com/microsoft/jrtc-apps"
LABEL org.opencontainers.image.authors="Microsoft Corporation"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.description="SDK for SRSRAN with JBPF"

RUN echo "*** Installing packages"
RUN tdnf upgrade tdnf --refresh -y && tdnf -y update
RUN tdnf -y install yaml-cpp-static boost-devel clang


RUN echo "*** Installing relevatn jbpf binaries"
COPY --from=srsran /src/out /src/out
COPY --from=srsran /src/include /src/include
COPY --from=srsran /src/external /src/external
COPY --from=srsran /usr/lib /usr/lib
COPY --from=srsran /usr/local/lib /usr/local/lib

RUN rm -f /usr/local/lib/librte*

RUN echo "*** Installing relevant jbpf_protobuf binaries"
COPY --from=jbpf_protobuf_builder /jbpf-protobuf/3p/nanopb /nanopb
COPY --from=jbpf_protobuf_builder jbpf-protobuf/out/bin/jbpf_protobuf_cli /usr/local/bin/jbpf_protobuf_cli

RUN tdnf install -y gcc gcc-c++ make python3 python3-pip
RUN pip install ctypesgen
RUN python3 -m pip install -r /nanopb/requirements.txt

ENV JBPF_PROTOBUF_CLI_BIN=/usr/local/bin/jbpf_protobuf_cli
ENV NANO_PB=/nanopb

ENV LD_LIBRARY_PATH=/usr/local/lib/:/usr/lib

ENV JBPF_OUT_DIR=/src/out
ENV SRSRAN_INC_DIR=/src/include
ENV SRSRAN_EXTERNAL_DIR=/src/external
ENV CPP_INC=/usr/include/c++/13.2.0
ENV VERIFIER_BIN=/src/out/bin/srsran_verifier_cli

WORKDIR /out

ENTRYPOINT ["/bin/bash"]
