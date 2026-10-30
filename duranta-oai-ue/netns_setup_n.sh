#!/bin/bash
# Per-UE netns for N UEs (netns_setup.sh is the fixed 4-UE version).
#   bash netns_setup_n.sh <N>
# UE i gets netns ue<i> and a veth pair: v<i>h (pod netns, 10.201.<i>.1) <-> v<i>g (ue<i>, 10.201.<i>.2).
# ZMQ between the broker and UE i runs over that pair; the UE's oaitun_ue1 stays isolated in ue<i>.
# Also clears any ue<i>/v<i>h left over from an earlier, larger run.
set -e
N="${1:?usage: netns_setup_n.sh <num_ues>}"
mkdir -p /var/run/netns
for n in $(seq 1 64); do
  ip netns del ue$n 2>/dev/null || true
  ip link del v${n}h 2>/dev/null || true
done
for n in $(seq 1 "$N"); do
  ip netns add ue$n
  ip link add v${n}h type veth peer name v${n}g
  ip link set v${n}g netns ue$n
  ip addr add 10.201.$n.1/24 dev v${n}h
  ip link set v${n}h up
  ip netns exec ue$n ip addr add 10.201.$n.2/24 dev v${n}g
  ip netns exec ue$n ip link set v${n}g up
  ip netns exec ue$n ip link set lo up
  echo "ue$n: pod 10.201.$n.1 <-> netns 10.201.$n.2"
done
