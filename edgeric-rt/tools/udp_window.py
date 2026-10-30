#!/usr/bin/env python3
"""UDP delivered at the UEs over a time window, from two snapshots of the UE-side iperf3 server logs.

traffic_nue.sh runs one iperf3 server per UE netns with a one-second report interval (/tmp/ues/dl_ue<n>.log in the
UE container). A snapshot is every interval line of every log, each file introduced by '## <path>':

    kubectl exec -n ran srs-gnb-du1-0 -c durue1 -- bash -c \
      'for f in /tmp/ues/dl_ue*.log; do echo "## $f"; grep -E "sec .*bits/sec" $f | grep -vE "receiver|sender"; done'

    python3 tools/udp_window.py start.txt end.txt [--skip 2]

prints the mean Mbit/s of the one-second intervals that appeared between the two snapshots, per UE and in total.
Standard library only (runs on the host or in the edgeric container).
"""

import argparse
import re
import sys

UNIT = {"K": 1e-3, "M": 1.0, "G": 1e3, "": 1e-6}


def read(path):
    logs, cur = {}, None
    with open(path) as f:
        for line in f:
            if line.startswith("## "):
                m = re.search(r"dl_ue(\d+)\.log", line)
                cur = int(m.group(1)) if m else None
                logs.setdefault(cur, [])
            elif cur is not None:
                m = re.search(r"([\d.]+)\s+([KMG]?)bits/sec", line)
                if m:
                    logs[cur].append(float(m.group(1)) * UNIT[m.group(2)])
    return logs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--skip", type=int, default=2, help="intervals to drop at the start of the window")
    a = ap.parse_args(argv)
    s, e = read(a.start), read(a.end)
    total = 0.0
    parts = []
    for ue in sorted(e):
        new = e[ue][len(s.get(ue, [])) + a.skip:]
        mean = sum(new) / len(new) if new else 0.0
        total += mean
        parts.append(f"ue{ue} {mean:.2f}")
        print(f"ue{ue} {mean:.3f} Mbit/s over {len(new)} s")
    print(f"total {total:.3f} Mbit/s ({', '.join(parts)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
