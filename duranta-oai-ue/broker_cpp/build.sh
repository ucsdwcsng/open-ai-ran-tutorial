#!/bin/bash
# Build zmq_broker for the grbroker container (Ubuntu 22.04, libzmq5), in a matching container.
#   bash duranta-oai-ue/broker_cpp/build.sh      -> duranta-oai-ue/broker_cpp/zmq_broker
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
docker run --rm -v "$HERE":/src -w /src ubuntu:22.04 bash -c \
  "apt-get update -qq >/dev/null && apt-get install -y -qq g++ libzmq3-dev >/dev/null 2>&1 &&
   g++ -O3 -march=native -std=c++17 -Wall -Wextra zmq_broker.cpp -lzmq -o zmq_broker &&
   chown $(id -u):$(id -g) zmq_broker"
echo "built $HERE/zmq_broker"
