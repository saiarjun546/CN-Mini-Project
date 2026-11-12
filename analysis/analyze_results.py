"""Analysis of the real time-sync experiments (Member 3).

Reads  results/final/all-sync-measurements.csv  (made by experiments/consolidate_results.py)
Writes results/analysis/  : metrics tables (CSV) and graphs (PNG).

Usage (from the repo root):
    python analysis/analyze_results.py
    python analysis/analyze_results.py <input_csv> <output_dir>

Conventions (from client/time_client.py):
    RTT    = (T4 - T1) - (T3 - T2)
    offset = ((T2 - T1) + (T3 - T4)) / 2      (server clock - client clock)
    sync_error = |estimated offset - actual offset|
A request with status FAILED got no valid reply within the 2 s timeout, so it has
no RTT or error. Those are counted in the success rate, not in the averages.
"""
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
ORANGE, AQUA = "#eb6834", "#1baf7a"          # fixed colour per mode, everywhere
COLOR = {"noqos": ORANGE, "qos": AQUA}
LABEL = {"noqos": "No priority (no QoS)", "qos": "Sync prioritized (QoS)"}
MODES = ["noqos", "qos"]

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "text.color": INK, "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.edgecolor": AXIS,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "axes.titlelocation": "left", "axes.titlesize": 12, "axes.titlecolor": INK,
    "legend.frameon": False, "legend.fontsize": 9, "lines.linewidth": 1.8,
})


def load(path):
    d = pd.read_csv(path)
    for c in ["rtt_ms", "sync_error_ms", "actual_offset_ms", "estimated_offset_ms",
              "offered_traffic_mbps"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["ok"] = d["status"] == "SUCCESS"
    d["abs_error_ms"] = d["sync_error_ms"].abs()
    return d


def summarize(g):
    s = g[g.ok]
    return pd.Series({
        "requests": len(g),
        "successful": int(g.ok.sum()),
        "failed": int((~g.ok).sum()),
        "success_rate_pct": 100 * g.ok.mean(),
        "rtt_mean_ms": s.rtt_ms.mean(),
        "rtt_median_ms": s.rtt_ms.median(),
        "rtt_p95_ms": s.rtt_ms.quantile(0.95),
        "jitter_ms": s.rtt_ms.std(),
        "error_mean_ms": s.abs_error_ms.mean(),
        "error_median_ms": s.abs_error_ms.median(),
        "error_max_ms": s.abs_error_ms.max(),
    })


def save(fig, out, name):
    fig.tight_layout()
    fig.savefig(os.path.join(out, name), dpi=160)
    plt.close(fig)


def line_by_mode(d, col, ylabel, title, out, name, log=False, note=None):
    """One line per mode against offered traffic. Missing points = no successful sync."""
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    levels = sorted(d.offered_traffic_mbps.unique())
    for m in MODES:
        g = d[d["mode"] == m].groupby("offered_traffic_mbps")
        y = g[col].apply(lambda s: s.mean())
        ax.plot(y.index, y.values, color=COLOR[m], label=LABEL[m], marker="o", markersize=7,
                markeredgecolor=SURFACE, markeredgewidth=2)
    if log:
        ax.set_yscale("log")
    ax.set_xticks(levels)
    ax.set_xlabel("Background traffic offered (Mbps) on a 2 Mbps bottleneck")
    ax.set_ylabel(ylabel)
    ax.set_title(title, pad=10)
    ax.legend(loc="best")
    if note:
        ax.annotate(note, xy=(0.99, 0.02), xycoords="axes fraction", ha="right", fontsize=8,
                    color=MUTED)
    save(fig, out, name)


def plot_success(cong, out):
    t = cong.groupby(["mode", "offered_traffic_mbps"]).ok.mean().mul(100).rename("v").reset_index()
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    for m in MODES:
        s = t[t["mode"] == m]
        ax.plot(s.offered_traffic_mbps, s.v, color=COLOR[m], label=LABEL[m], marker="o",
                markersize=7, markeredgecolor=SURFACE, markeredgewidth=2)
    ax.set_xticks(sorted(t.offered_traffic_mbps.unique()))
    ax.set_ylim(-3, 105)
    ax.set_xlabel("Background traffic offered (Mbps) on a 2 Mbps bottleneck")
    ax.set_ylabel("Sync requests answered (%)")
    ax.set_title("Reliability of time sync under congestion", pad=10)
    ax.legend(loc="lower left")
    save(fig, out, "1_success_rate_vs_traffic.png")


def plot_prio(pri, out):
    """Two clients (h2, h4) at the same time: with vs without priority."""
    s = pri.groupby(["client", "mode"]).apply(summarize, include_groups=False).reset_index()
    clients = sorted(s.client.unique())
    fig, axes = plt.subplots(1, 3, figsize=(11, 4.2))
    panels = [("success_rate_pct", "Requests answered (%)", "Success rate"),
              ("rtt_mean_ms", "Mean RTT (ms)", "Round-trip delay"),
              ("error_mean_ms", "Mean absolute error (ms)", "Sync error")]
    x = np.arange(len(clients))
    w = 0.36
    for ax, (col, yl, ttl) in zip(axes, panels):
        for i, m in enumerate(MODES):
            vals = [s[(s.client == c) & (s["mode"] == m)][col].iloc[0] for c in clients]
            bars = ax.bar(x + (i - 0.5) * w, vals, w, color=COLOR[m], label=LABEL[m],
                          edgecolor=SURFACE, linewidth=2)
            for b, v in zip(bars, vals):
                ax.text(b.get_x() + b.get_width() / 2, 0 if np.isnan(v) else v,
                        "none" if np.isnan(v) else f"{v:.0f}", ha="center", va="bottom",
                        fontsize=8, color=INK2)
        ax.set_xticks(x, clients)
        ax.set_xlabel("Client")
        ax.set_ylabel(yl)
        ax.set_title(ttl, pad=8)
        ax.grid(axis="x", visible=False)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", ncol=2, bbox_to_anchor=(0.99, 0.99))
    fig.suptitle("Two clients syncing at once, 3 Mbps background traffic", x=0.01, ha="left",
                 fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(os.path.join(out, "4_two_clients_priority.png"), dpi=160)
    plt.close(fig)


def plot_error_vs_rtt(d, out):
    """Sync error can never exceed about RTT/2, so error tracks RTT."""
    s = d[d.ok]
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    for m in MODES:
        g = s[s["mode"] == m]
        ax.scatter(g.rtt_ms, g.abs_error_ms, s=22, color=COLOR[m], alpha=0.7, label=LABEL[m],
                   edgecolors=SURFACE, linewidths=0.8)
    mx = s.rtt_ms.max()
    ax.plot([0, mx], [0, mx / 2], color=MUTED, linewidth=1, linestyle="--")
    ax.annotate("error = RTT / 2 (worst case)", xy=(mx * 0.62, mx * 0.62 / 2), xytext=(0, 8),
                textcoords="offset points", fontsize=8, color=MUTED)
    ax.set_xlabel("Round-trip delay (ms)")
    ax.set_ylabel("Absolute sync error (ms)")
    ax.set_title("Sync error grows with round-trip delay", pad=10)
    ax.legend(loc="upper left")
    save(fig, out, "5_error_vs_rtt.png")


def plot_box(cong, out):
    """Sync error distribution at each traffic level, both modes side by side."""
    s = cong[cong.ok]
    levels = sorted(cong.offered_traffic_mbps.unique())
    fig, ax = plt.subplots(figsize=(8, 4.4))
    w = 0.34
    for i, m in enumerate(MODES):
        pos, data = [], []
        for k, lv in enumerate(levels):
            v = s[(s["mode"] == m) & (s.offered_traffic_mbps == lv)].abs_error_ms.dropna()
            if len(v):
                pos.append(k + (i - 0.5) * w * 1.15)
                data.append(v)
        if not data:
            continue
        bp = ax.boxplot(data, positions=pos, widths=w, patch_artist=True, manage_ticks=False,
                        medianprops=dict(color=INK, linewidth=1.5),
                        whiskerprops=dict(color=MUTED), capprops=dict(color=MUTED),
                        flierprops=dict(marker="o", markersize=3, markeredgecolor=MUTED,
                                        markerfacecolor="none", alpha=0.6))
        for p in bp["boxes"]:
            p.set_facecolor(COLOR[m])
            p.set_edgecolor(SURFACE)
        ax.plot([], [], color=COLOR[m], linewidth=8, label=LABEL[m])
    ax.set_yscale("log")
    ax.set_xticks(range(len(levels)), [f"{lv:g}" for lv in levels])
    ax.set_xlabel("Background traffic offered (Mbps) on a 2 Mbps bottleneck")
    ax.set_ylabel("Absolute sync error (ms, log scale)")
    ax.set_title("Sync error distribution (successful syncs only)", pad=10)
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper left")
    ax.annotate("No box = no sync request was answered", xy=(0.99, 0.02),
                xycoords="axes fraction", ha="right", fontsize=8, color=MUTED)
    save(fig, out, "6_error_boxplot.png")


def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else "results/final/all-sync-measurements.csv"
    out = sys.argv[2] if len(sys.argv) > 2 else "results/analysis"
    os.makedirs(out, exist_ok=True)
    d = load(inp)
    cong = d[d.experiment == "congestion"]
    pri = d[d.experiment == "prioritization"]

    mc = cong.groupby(["mode", "offered_traffic_mbps"]).apply(summarize, include_groups=False).round(2)
    mp = pri.groupby(["mode", "client"]).apply(summarize, include_groups=False).round(2)
    mc.to_csv(os.path.join(out, "metrics_congestion.csv"))
    mp.to_csv(os.path.join(out, "metrics_prioritization.csv"))

    note = "Missing point = no sync request was answered"
    plot_success(cong, out)
    line_by_mode(cong[cong.ok], "rtt_ms", "Mean round-trip delay (ms, log scale)",
                 "Round-trip delay under congestion", out, "2_rtt_vs_traffic.png", log=True,
                 note=note)
    line_by_mode(cong[cong.ok], "abs_error_ms", "Mean absolute sync error (ms, log scale)",
                 "Sync error under congestion", out, "3_error_vs_traffic.png", log=True, note=note)
    plot_prio(pri, out)
    plot_error_vs_rtt(d, out)
    plot_box(cong, out)

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print("CONGESTION (client h2 alone)\n", mc.to_string())
    print("\nPRIORITIZATION (h2 and h4 together, 3 Mbps)\n", mp.to_string())
    print(f"\nSaved tables and graphs to {out}")


if __name__ == "__main__":
    main()
