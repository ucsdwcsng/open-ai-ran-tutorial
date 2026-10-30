#!/bin/bash
# Per-UE netns so each OAI UE's oaitun_ue1 is isolated; veth carries ZMQ to the broker (pod netns).
set -e
for n in 1 2 3 4; do
  ip netns del ue$n 2>/dev/null || true
  ip link del v${n}h 2>/dev/null || true
  ip netns add ue$n
  ip link add v${n}h type veth peer name v${n}g
  ip link set v${n}g netns ue$n
  ip addr add 10.201.$n.1/24 dev v${n}h
  ip link set v${n}h up
  ip netns exec ue$n ip addr add 10.201.$n.2/24 dev v${n}g
  ip netns exec ue$n ip link set v${n}g up
  ip netns exec ue$n ip link set lo up
  echo "ue$n: host 10.201.$n.1 <-> netns 10.201.$n.2"
done
