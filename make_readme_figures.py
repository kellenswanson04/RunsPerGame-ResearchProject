"""
Build README figures from the team-season output CSVs.

Run after runs_per_game_analysis.py:

    python make_readme_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
FIG = ROOT / "figures"
MIN_GAMES = 20

NAVY = "#1f4e79"
GREEN = "#2e7d4f"
GOLD = "#c47b17"
GRAY = "#5c6570"
RED = "#9b2c2c"

METRIC_LABELS = {
    "ops": "OPS",
    "obp": "OBP",
    "slg": "SLG",
    "avg": "AVG",
    "ebh_per_game": "EBH / game",
    "hr_per_game": "HR / game",
    "hitter_bb_pct": "Hitter BB%",
    "first_pitch_strike_pct": "1st-pitch strike seen",
    "hitter_k_pct": "Hitter K%",
    "chase_pct": "Chase%",
    "runs_allowed_per_game": "RA / game",
    "zone_pct": "Zone% seen",
    "parkfactor_100": "Park factor",
    "sb_per_game": "SB / game",
    "opp_runs_allowed_per_game": "Opp RA / game",
    "opp_avg_runs_per_game": "Opp RPG",
    "in_zone_swing_pct": "In-zone swing%",
    "runs_allowed": "Runs allowed (total)",
}


def style_axes(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(colors=GRAY)
    ax.xaxis.label.set_color(GRAY)
    ax.yaxis.label.set_color(GRAY)
    ax.title.set_color("#1a1a1a")


def save(fig: plt.Figure, name: str) -> Path:
    FIG.mkdir(exist_ok=True)
    path = FIG / name
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Wrote {path}")
    return path


def correlation_chart(corr: pd.DataFrame) -> None:
    plot = corr.loc[corr["metric"] != "runs_allowed"].copy()
    plot["label"] = plot["metric"].map(lambda m: METRIC_LABELS.get(m, m))
    plot = plot.sort_values("pearson_r_vs_rpg")
    colors = [GREEN if r >= 0 else RED for r in plot["pearson_r_vs_rpg"]]

    fig, ax = plt.subplots(figsize=(9.2, 7.2))
    ax.barh(plot["label"], plot["pearson_r_vs_rpg"], color=colors, height=0.72)
    ax.axvline(0, color="#333333", linewidth=0.8)
    ax.set_xlabel("Pearson r vs runs per game")
    ax.set_title("What correlates with NCAA D1 runs per game (2023–2026)")
    ax.set_xlim(-0.65, 1.05)
    style_axes(ax)
    fig.text(
        0.01,
        -0.02,
        "Source: TrackMan D1 pitch logs · 842 team-seasons with 20+ games of 6+ innings.",
        color=GRAY,
        fontsize=8,
    )
    save(fig, "corr_vs_rpg.png")


def top5_vs_average_chart(vs_avg: pd.DataFrame) -> None:
    keep = [
        "ops",
        "slg",
        "ebh_per_game",
        "hr_per_game",
        "hitter_bb_pct",
        "hitter_k_pct",
        "chase_pct",
        "sb_per_game",
        "parkfactor_100",
        "runs_allowed_per_game",
    ]
    plot = vs_avg.set_index("metric").loc[keep]
    labels = [plot.loc[m, "label"] for m in keep]
    rpg = plot["top5_rpg_pct_vs_avg"].to_numpy()
    adj = plot["top5_adj_pct_vs_avg"].to_numpy()
    y = np.arange(len(labels))
    h = 0.38

    fig, ax = plt.subplots(figsize=(9.4, 6.8))
    ax.barh(y + h / 2, rpg, h, color=NAVY, label="Top 5% raw RPG")
    ax.barh(y - h / 2, adj, h, color=GREEN, label="Top 5% adj RPG")
    ax.axvline(0, color="#333333", linewidth=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Percent difference vs D1 average")
    ax.set_title("Top 5% scoring seasons vs D1 average")
    ax.legend(frameon=False, loc="lower right")
    style_axes(ax)
    fig.text(
        0.01,
        -0.02,
        "Positive = higher than the 842-season mean. K% and chase being negative is good for an offense.",
        color=GRAY,
        fontsize=8,
    )
    save(fig, "top5_vs_average.png")


def scatter_rpg_vs_adj(stats: pd.DataFrame, top_rpg: pd.DataFrame, top_adj: pd.DataFrame) -> None:
    q = stats.loc[
        (stats["games_used"] >= MIN_GAMES)
        & stats["runs_per_game"].notna()
        & stats["adj_runs_per_game"].notna()
    ].copy()
    rpg_keys = set(zip(top_rpg["college_name"], top_rpg["season"]))
    adj_keys = set(zip(top_adj["college_name"], top_adj["season"]))

    def bucket(row) -> str:
        key = (row["college_name"], row["season"])
        in_r, in_a = key in rpg_keys, key in adj_keys
        if in_r and in_a:
            return "both"
        if in_r:
            return "raw"
        if in_a:
            return "adj"
        return "other"

    q["bucket"] = q.apply(bucket, axis=1)
    fig, ax = plt.subplots(figsize=(8.6, 8.0))
    other = q.loc[q["bucket"] == "other"]
    ax.scatter(
        other["runs_per_game"],
        other["adj_runs_per_game"],
        s=18,
        c="#c8cdd3",
        alpha=0.7,
        label="Other (n={})".format(len(other)),
        zorder=1,
    )
    both = q.loc[q["bucket"] == "both"]
    ax.scatter(
        both["runs_per_game"],
        both["adj_runs_per_game"],
        s=42,
        c=GREEN,
        label="Top 5% of both (n={})".format(len(both)),
        zorder=3,
    )
    raw = q.loc[q["bucket"] == "raw"]
    ax.scatter(
        raw["runs_per_game"],
        raw["adj_runs_per_game"],
        s=42,
        c=GOLD,
        label="Top 5% raw only (n={})".format(len(raw)),
        zorder=4,
    )
    adj = q.loc[q["bucket"] == "adj"]
    ax.scatter(
        adj["runs_per_game"],
        adj["adj_runs_per_game"],
        s=42,
        c=NAVY,
        label="Top 5% adj only (n={})".format(len(adj)),
        zorder=4,
    )
    lim = (3.5, 11.5)
    ax.plot(lim, lim, color="#888888", linewidth=0.8, linestyle="--", label="Raw = adjusted")
    ax.set_xlim(*lim)
    ax.set_ylim(*lim)
    ax.set_xlabel("Runs per game")
    ax.set_ylabel("Park-adjusted runs per game")
    ax.set_title("Raw vs park-adjusted runs per game")
    ax.legend(frameon=False, loc="upper left")
    style_axes(ax)
    fig.text(
        0.01,
        -0.02,
        "Points above the dashed line scored more than their parks would imply. Hitter-park teams fall below it.",
        color=GRAY,
        fontsize=8,
    )
    save(fig, "rpg_vs_adj_scatter.png")


def parkfactor_chart(stats: pd.DataFrame) -> None:
    q = stats.loc[
        (stats["games_used"] >= MIN_GAMES)
        & stats["runs_per_game"].notna()
        & stats["parkfactor_100"].notna()
    ]
    fig, ax = plt.subplots(figsize=(8.6, 6.4))
    ax.scatter(q["parkfactor_100"], q["runs_per_game"], s=18, c=NAVY, alpha=0.45)
    x = q["parkfactor_100"].to_numpy()
    y = q["runs_per_game"].to_numpy()
    mask = np.isfinite(x) & np.isfinite(y)
    coef = np.polyfit(x[mask], y[mask], 1)
    xs = np.linspace(x[mask].min(), x[mask].max(), 50)
    ax.plot(xs, coef[1] + coef[0] * xs, color=GOLD, linewidth=1.6, label="Linear fit")
    r = float(np.corrcoef(x[mask], y[mask])[0, 1])
    ax.set_xlabel("Home park factor (100 = average)")
    ax.set_ylabel("Runs per game")
    ax.set_title(f"Home park factor vs raw RPG  (r = {r:.2f})")
    ax.legend(frameon=False)
    style_axes(ax)
    fig.text(
        0.01,
        -0.02,
        "Park factor is a modest correlate of raw scoring. It is not the main driver of 9+ RPG seasons.",
        color=GRAY,
        fontsize=8,
    )
    save(fig, "parkfactor_vs_rpg.png")


def main() -> None:
    stats = pd.read_csv(ROOT / "team_season_offensive_stats.csv")
    vs_avg = pd.read_csv(ROOT / "top5pct_vs_average.csv")
    top_rpg = pd.read_csv(ROOT / "top5pct_rpg.csv")
    top_adj = pd.read_csv(ROOT / "top5pct_adj_rpg.csv")
    corr_path = ROOT / "rpg_metric_correlations_new.csv"
    if not corr_path.exists():
        corr_path = ROOT / "rpg_metric_correlations.csv"
    corr = pd.read_csv(corr_path)

    correlation_chart(corr)
    top5_vs_average_chart(vs_avg)
    scatter_rpg_vs_adj(stats, top_rpg, top_adj)
    parkfactor_chart(stats)


if __name__ == "__main__":
    main()
