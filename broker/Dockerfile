FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gnuradio \
        gnuradio-dev \
        python3-zmq \
        python3-numpy \
        iproute2 \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
CMD ["sleep","infinity"]
