FROM ghcr.io/microsoft/jrt-controller/jrt-controller-azurelinux:latest AS jrt_controller_azurelinux
FROM ghcr.io/microsoft/jrtc-apps/srs-jbpf-sdk:latest

LABEL org.opencontainers.image.source="https://github.com/microsoft/jrtc-apps"
LABEL org.opencontainers.image.authors="Microsoft Corporation"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.description="SDK for SRSRAN with JBPF and JRTC"

COPY --from=jrt_controller_azurelinux /jrtc /jrtc
WORKDIR /jrtc/build
RUN rm -rf /jrtc/build/*
RUN cmake .. && make jbpf_io jrtc_router_stream_id jrtc
ENV PATH="$PATH:/jrtc/out/bin"

ENTRYPOINT [ "/jrtc/deploy/entrypoint.sh", "jrtc-ctl" ]
