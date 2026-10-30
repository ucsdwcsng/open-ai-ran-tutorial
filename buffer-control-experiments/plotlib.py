"""Timeline plot for Demo 3 (used by snapshot.py).

Three panels on one time axis, cap phases shaded and named along the top:
  1. Latency: each flow's TCP RTT (video bold, download thin) and the gNB's RLC
     queuing delay (dotted) -- shows that both flows sit behind the same queue.
  2. Throughput per flow.
  3. RLC buffer: occupancy (fill) against the cap in force (step line).
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# categorical slots 1-2 (validated reference palette) + neutral ink
C_VIDEO, C_BULK = "#2a78d6", "#eb6834"
INK, INK2, INK3, GRID, BAND = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df", "#f1f0ec"
FLOW_STYLE = {"video": dict(color=C_VIDEO, lw=2.2, zorder=4),
              "bulk": dict(color=C_BULK, lw=1.3, zorder=3)}
CALL_LIMIT_MS = 150      # ITU-T G.114: one-way mouth-to-ear delay above ~150 ms degrades calls


def style():
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": INK2, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True,
                         "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
                         "axes.spines.top": False, "axes.spines.right": False})


def timeline(path, title, flows, qbytes, qlat, caps, t_end, note=None):
    """flows: {name: {"label", "rtt": [(t, ms)], "tput": [(t, Mbit/s)]}}, t in seconds from start.
    qbytes/qlat: [(t, v)]. caps: [(t, label, limit_kB or None)] in time order."""
    style()
    xs = lambda s: [t for t, _ in s]
    ys = lambda s, k=1.0: [v * k for _, v in s]
    fig, ax = plt.subplots(3, 1, figsize=(12, 9.5), sharex=True,
                           gridspec_kw={"height_ratios": [1.5, 1, 1]})
    for name, f in flows.items():
        st = FLOW_STYLE.get(name, dict(color=INK2, lw=1.2))
        ax[0].plot(xs(f["rtt"]), ys(f["rtt"]), label=f["label"], **st)
        ax[1].plot(xs(f["tput"]), ys(f["tput"]), label=f["label"], **st)
    if qlat:
        ax[0].plot(xs(qlat), ys(qlat), color=INK3, lw=1.2, ls=(0, (1, 1.5)), zorder=2,
                   label="RLC queuing delay at the gNB")
    ax[0].axhline(CALL_LIMIT_MS, color=INK2, lw=1, ls="--", zorder=1)
    ax[0].text(0.003, CALL_LIMIT_MS, f" {CALL_LIMIT_MS} ms: calls start to feel laggy",
               transform=ax[0].get_yaxis_transform(), va="bottom", fontsize=8.5, color=INK2)
    ax[0].set_ylabel("Latency (ms)")
    ax[0].legend(loc="upper right", frameon=False, fontsize=9)
    ax[1].set_ylabel("Throughput (Mbit/s)")
    ax[1].legend(loc="upper right", frameon=False, fontsize=9)

    ax[2].fill_between(xs(qbytes), ys(qbytes, 1e-3), color="#c9c8c2", lw=0, step="post",
                       label="RLC queue occupancy")
    step_x, step_y = [], []
    for k, (t, _, lim) in enumerate(caps):
        te = caps[k + 1][0] if k + 1 < len(caps) else t_end
        if lim is not None:
            step_x += [t, te]
            step_y += [lim * 1.024, lim * 1.024]   # kB (1024) -> kB (1000) axis
        else:
            step_x += [t, te]
            step_y += [float("nan")] * 2
    ax[2].plot(step_x, step_y, color=INK, lw=1.8, label="cap in force (bufcap)")
    ax[2].set_yscale("symlog", linthresh=10)
    ax[2].set_ylabel("RLC buffer (kB, log)")
    ax[2].set_xlabel("Time (s)")
    ax[2].legend(loc="upper right", frameon=False, fontsize=9)

    bands = [(t, lab) for k, (t, lab, _) in enumerate(caps) if k == 0 or lab != caps[k - 1][1]]
    for k, (t, lab) in enumerate(bands):
        te = bands[k + 1][0] if k + 1 < len(bands) else t_end
        for a_ in ax:
            if k % 2:
                a_.axvspan(t, te, color=BAND, lw=0, zorder=0)
            a_.axvline(t, color=INK3, lw=0.8, ls=":", zorder=1)
        ax[0].text((t + te) / 2, 1.02, lab, transform=ax[0].get_xaxis_transform(),
                   ha="center", va="bottom", fontsize=10, color=INK, fontweight="bold")
    for a_ in ax[:2]:
        a_.set_ylim(bottom=0)
    ax[2].set_ylim(bottom=0)
    ax[2].set_xlim(0, t_end)
    fig.suptitle(title, color=INK, fontsize=13, x=0.01, ha="left", y=0.995)
    if note:
        fig.text(0.01, 0.955, note, fontsize=9.5, color=INK2, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.94 if note else 0.965))
    fig.savefig(path, dpi=130)
    plt.close(fig)
