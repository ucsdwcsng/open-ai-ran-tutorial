"""UE names (ue1..ueN, as in setup_zmq_chan_demo.sh and the traces tables) -> C-RNTI -> du_ue_index.

scripts/ue_rnti_map.sh writes <run_dir>/ue_map.txt from the C-RNTI each OAI UE logs at RRC setup:

    ue1 0x4601
    ue2 0x4602

Reports carry (du_ue_index, rnti) for every UE, so a name resolves to the du_ue_index of the current report.
Without the file, UEs are taken in du_ue_index order (ue1 = lowest index), which is the attach order.
"""

import os
import re


class UeMap:
    def __init__(self, path=None):
        self.path = path
        self.by_name = {}
        self.mtime = None
        self._pos_key = None
        self._pos = []
        self.reload()

    def reload(self):
        if not self.path or not os.path.exists(self.path):
            return
        mtime = os.path.getmtime(self.path)
        if mtime == self.mtime:
            return
        by_name = {}
        with open(self.path) as f:
            for line in f:
                parts = line.split("#", 1)[0].split()
                if len(parts) >= 2 and re.fullmatch(r"ue\d+", parts[0].lower()):
                    by_name[parts[0].lower()] = int(parts[1], 0)
        self.by_name = by_name
        self.mtime = mtime

    def du_index(self, name, ues):
        """du_ue_index of UE `name` in the report's UE list, or None."""
        name = name.lower()
        rnti = self.by_name.get(name)
        if rnti is not None:
            for u in ues:
                if int(u["rnti"]) == rnti:
                    return int(u["du_ue_index"])
            return None
        m = re.fullmatch(r"ue(\d+)", name)
        if not m:
            return None
        order = sorted(int(u["du_ue_index"]) for u in ues)
        k = int(m.group(1)) - 1
        return order[k] if 0 <= k < len(order) else None

    def ordered(self, ues):
        """du_ue_index of the report's UEs, in ue1, ue2, ... order."""
        if self.by_name:
            rnti_to_idx = {int(u["rnti"]): int(u["du_ue_index"]) for u in ues}
            names = sorted(self.by_name, key=lambda n: int(n[2:]))
            out = [rnti_to_idx[self.by_name[n]] for n in names if self.by_name[n] in rnti_to_idx]
            rest = sorted(i for i in rnti_to_idx.values() if i not in out)
            return out + rest
        return sorted(int(u["du_ue_index"]) for u in ues)

    def positions(self, ues):
        """Indices into the report's UE array, in ue1, ue2, ... order. Cached while the UE set and the map file are
        unchanged, so the per-TTI path pays two byte copies instead of a sort."""
        key = (ues["rnti"].tobytes(), ues["du_ue_index"].tobytes(), self.mtime)
        if key != self._pos_key:
            idx = [int(i) for i in ues["du_ue_index"]]
            self._pos = [idx.index(i) for i in self.ordered(ues)]
            self._pos_key = key
        return self._pos

    def name_of(self, du_index, ues):
        for pos, idx in enumerate(self.ordered(ues)):
            if idx == du_index:
                if self.by_name:
                    rnti = next(int(u["rnti"]) for u in ues if int(u["du_ue_index"]) == idx)
                    for n, r in self.by_name.items():
                        if r == rnti:
                            return n
                return f"ue{pos + 1}"
        return f"du{du_index}"
