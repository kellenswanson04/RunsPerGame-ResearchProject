"""
Stream the TrackMan master pitch CSVs (without loading them whole) and build
team-season offensive stats for every school in trackman_d1_cube_index.csv.

Writes:
  team_season_offensive_stats.csv  – one row per school x season
  rpg_metric_correlations.csv      – Pearson/Spearman correlation of each
                                     metric with runs per game
  high_rpg_team_seasons.csv        – team-seasons at 9.0+ RPG
  top5pct_rpg.csv                  – top 5% of raw runs per game
  top5pct_adj_rpg.csv              – top 5% of park-adjusted runs per game
  top5pct_vs_average.csv           – mean stats vs qualified-season average
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent

MASTER_FILES = [
    ROOT / "Master232425.csv",
    ROOT / "Master2026.csv",
]
INDEX_PATH = ROOT / "trackman_d1_cube_index.csv"
PARK_PATH = ROOT / "parkfactor.csv"

USECOLS = [
    "Date",
    "PAofInning",
    "PitchofPA",
    "PitcherTeam",
    "BatterTeam",
    "Inning",
    "Top/Bottom",
    "PitchCall",
    "KorBB",
    "PlayResult",
    "RunsScored",
    "PlateLocHeight",
    "PlateLocSide",
    "HomeTeam",
    "AwayTeam",
    "GameUID",
]

CHUNKSIZE = 250_000
MIN_INNINGS = 6
MIN_GAMES_FOR_CORR = 20
HIGH_RPG = 9.0

# Approximate TrackMan strike zone (feet).
ZONE_SIDE = 0.83
ZONE_BOT = 1.5
ZONE_TOP = 3.5

SWING_CALLS = {
    "strikeswinging",
    "foulball",
    "foulballfieldable",
    "foulballnotfieldable",
    "foultip",
    "inplay",
}
STRIKE_LIKE_CALLS = SWING_CALLS | {"strikecalled"}
WALK_KORBB = {"walk", "intentionalwalk"}
HIT_RESULTS = {"single", "double", "triple", "homerun"}
OUT_NOT_AB = {"sacrifice"}


def season_from_date_str(value) -> int | None:
    """Map a pitch date to an NCAA season year (fall games belong to next spring)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    if len(text) < 7:
        return None
    try:
        year = int(text[0:4])
        month = int(text[5:7])
    except ValueError:
        return None
    return year + 1 if month >= 8 else year


def norm_text(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def norm_key(value) -> str:
    return norm_text(value).lower()


def load_park_factors(path: Path) -> dict[str, float]:
    """One ParkFactor_100 per TrackMan team code (stadium with the most home games)."""
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    if "parkfactor_100" not in cols or "principalhometeam" not in cols:
        raise ValueError("parkfactor.csv must include PrincipalHomeTeam and ParkFactor_100")
    pf_col = cols["parkfactor_100"]
    team_col = cols["principalhometeam"]
    games_col = cols.get("homegames")
    out = df.copy()
    out["_team"] = out[team_col].astype(str).str.strip()
    out["_pf"] = pd.to_numeric(out[pf_col], errors="coerce")
    out = out[(out["_team"] != "") & (out["_team"].str.lower() != "nan") & out["_pf"].gt(0)]
    if games_col:
        out["_hg"] = pd.to_numeric(out[games_col], errors="coerce").fillna(0)
        out = out.sort_values("_hg", ascending=False)
    out = out.drop_duplicates("_team", keep="first")
    return dict(zip(out["_team"], out["_pf"].astype(float)))


def load_index(path: Path) -> pd.DataFrame:
    idx = pd.read_csv(path)
    idx["trackman_code"] = idx["trackman_code"].astype(str).str.strip()
    idx["college_name"] = idx["college_name"].astype(str).str.strip()
    idx["full_college_name"] = idx["full_college_name"].astype(str).str.strip()
    return idx.drop_duplicates(subset=["trackman_code"], keep="first")


def new_pitch_bucket() -> dict:
    return {
        "pitches": 0,
        "zone_pitches": 0,
        "in_zone": 0,
        "out_zone": 0,
        "chases": 0,
        "in_zone_swings": 0,
        "first_pitches": 0,
        "first_pitch_strikes": 0,
        "sb": 0,
        "cs": 0,
    }


def new_pa_bucket() -> dict:
    return {
        "pa": 0,
        "ab": 0,
        "hits": 0,
        "singles": 0,
        "doubles": 0,
        "triples": 0,
        "hr": 0,
        "tb": 0,
        "bb": 0,
        "k": 0,
        "hbp": 0,
        "sf_sh": 0,
        "reach_ob": 0,
    }


def add_pa_outcome(bucket: dict, play: str, korbb: str, pitch_call: str) -> None:
    if play in {"stolenbase", "caughtstealing"}:
        return

    is_k = korbb == "strikeout"
    is_bb = korbb in WALK_KORBB or pitch_call == "ballintentional"
    is_hbp = pitch_call == "hitbypitch"
    is_hit = play in HIT_RESULTS
    is_sac = play in OUT_NOT_AB
    is_ball_in_play_out = play in {"out", "error", "fielderschoice"}

    if not (is_k or is_bb or is_hbp or is_hit or is_sac or is_ball_in_play_out):
        return

    bucket["pa"] += 1
    if is_k:
        bucket["k"] += 1
        bucket["ab"] += 1
        return
    if is_bb:
        bucket["bb"] += 1
        bucket["reach_ob"] += 1
        return
    if is_hbp:
        bucket["hbp"] += 1
        bucket["reach_ob"] += 1
        return
    if is_sac:
        bucket["sf_sh"] += 1
        return

    bucket["ab"] += 1
    if play == "single":
        bucket["hits"] += 1
        bucket["singles"] += 1
        bucket["tb"] += 1
        bucket["reach_ob"] += 1
    elif play == "double":
        bucket["hits"] += 1
        bucket["doubles"] += 1
        bucket["tb"] += 2
        bucket["reach_ob"] += 1
    elif play == "triple":
        bucket["hits"] += 1
        bucket["triples"] += 1
        bucket["tb"] += 3
        bucket["reach_ob"] += 1
    elif play == "homerun":
        bucket["hits"] += 1
        bucket["hr"] += 1
        bucket["tb"] += 4
        bucket["reach_ob"] += 1
    elif play == "error" or play == "fielderschoice":
        bucket["reach_ob"] += 1


def rate(n: float, d: float) -> float:
    if d == 0:
        return float("nan")
    return n / d


def process_masters(paths: list[Path]) -> tuple[dict, dict, dict, dict, set[int]]:
    """
    Single streaming pass.

    Returns:
      game_meta[game_uid] = {season, home, away, max_inning}
      game_runs[(game_uid, team)] = runs
      pitch_stats[(team, season)] = pitch/zone/sb counters
      pa_last[pa_key] = (pitchofpa, team, season, play, korbb, pitchcall)
      seasons
    """
    game_meta: dict[str, dict] = {}
    game_runs: dict[tuple[str, str], int] = defaultdict(int)
    pitch_stats: dict[tuple[str, int], dict] = defaultdict(new_pitch_bucket)
    pa_last: dict[tuple, tuple] = {}
    seasons: set[int] = set()

    for path in paths:
        if not path.exists():
            print(f"Skipping missing file: {path}", file=sys.stderr)
            continue
        print(f"\nReading {path.name} ({path.stat().st_size / 1e6:.0f} MB) ...")
        start = time.time()
        rows_seen = 0
        reader = pd.read_csv(
            path,
            usecols=lambda c: c in USECOLS,
            chunksize=CHUNKSIZE,
            dtype={"GameUID": "string", "Date": "string"},
            low_memory=False,
        )
        for chunk_i, chunk in enumerate(reader, start=1):
            rows_seen += len(chunk)
            consume_chunk(chunk, game_meta, game_runs, pitch_stats, pa_last, seasons)
            elapsed = time.time() - start
            print(
                f"  {path.name} chunk {chunk_i:>4}  "
                f"rows={rows_seen:>10,}  games={len(game_meta):,}  "
                f"pas={len(pa_last):,}  {elapsed:.1f}s",
                flush=True,
            )
        print(f"Finished {path.name} in {time.time() - start:.1f}s")

    return game_meta, game_runs, pitch_stats, pa_last, seasons


def consume_chunk(
    chunk: pd.DataFrame,
    game_meta: dict,
    game_runs: dict,
    pitch_stats: dict,
    pa_last: dict,
    seasons: set[int],
) -> None:
    chunk = chunk.rename(columns={"Top/Bottom": "half"})
    for col in USECOLS:
        if col == "Top/Bottom":
            col = "half"
        if col not in chunk.columns:
            chunk[col] = pd.NA

    chunk = chunk[chunk["GameUID"].notna() & (chunk["GameUID"].astype(str).str.len() > 0)]
    if chunk.empty:
        return

    chunk["season"] = chunk["Date"].map(season_from_date_str)
    chunk = chunk[chunk["season"].notna()]
    if chunk.empty:
        return
    chunk["season"] = chunk["season"].astype(int)
    seasons.update(chunk["season"].unique().tolist())

    chunk["BatterTeam"] = chunk["BatterTeam"].map(norm_text)
    chunk["HomeTeam"] = chunk["HomeTeam"].map(norm_text)
    chunk["AwayTeam"] = chunk["AwayTeam"].map(norm_text)
    chunk["play"] = chunk["PlayResult"].map(norm_key)
    chunk["korbb"] = chunk["KorBB"].map(norm_key)
    chunk["pcall"] = chunk["PitchCall"].map(norm_key)
    chunk["half"] = chunk["half"].map(norm_text)
    chunk["RunsScored"] = pd.to_numeric(chunk["RunsScored"], errors="coerce").fillna(0).astype(int)
    chunk["Inning"] = pd.to_numeric(chunk["Inning"], errors="coerce")
    chunk["PAofInning"] = pd.to_numeric(chunk["PAofInning"], errors="coerce")
    chunk["PitchofPA"] = pd.to_numeric(chunk["PitchofPA"], errors="coerce")
    chunk["PlateLocHeight"] = pd.to_numeric(chunk["PlateLocHeight"], errors="coerce")
    chunk["PlateLocSide"] = pd.to_numeric(chunk["PlateLocSide"], errors="coerce")

    # Game participants / innings.
    for row in (
        chunk.groupby("GameUID", sort=False)
        .agg(
            season=("season", "first"),
            home=("HomeTeam", "first"),
            away=("AwayTeam", "first"),
            max_inning=("Inning", "max"),
        )
        .itertuples()
    ):
        uid = str(row.Index)
        meta = game_meta.get(uid)
        max_inn = 0 if pd.isna(row.max_inning) else int(row.max_inning)
        if meta is None:
            game_meta[uid] = {
                "season": int(row.season),
                "home": row.home,
                "away": row.away,
                "max_inning": max_inn,
            }
        else:
            if max_inn > meta["max_inning"]:
                meta["max_inning"] = max_inn
            if not meta["home"] and row.home:
                meta["home"] = row.home
            if not meta["away"] and row.away:
                meta["away"] = row.away

    # Runs scored on each pitch, attributed to the batting team.
    run_add = (
        chunk.loc[chunk["RunsScored"] > 0]
        .groupby(["GameUID", "BatterTeam"], sort=False)["RunsScored"]
        .sum()
    )
    for (uid, team), runs in run_add.items():
        if not team:
            continue
        game_runs[(str(uid), team)] += int(runs)

    # Stolen bases / caught stealing (any pitch, not just last of PA).
    sb = chunk.loc[chunk["play"] == "stolenbase"]
    if not sb.empty:
        for team, season, n in (
            sb.groupby(["BatterTeam", "season"], sort=False).size().reset_index(name="n").itertuples(index=False)
        ):
            if team:
                pitch_stats[(team, int(season))]["sb"] += int(n)
    cs = chunk.loc[chunk["play"] == "caughtstealing"]
    if not cs.empty:
        for team, season, n in (
            cs.groupby(["BatterTeam", "season"], sort=False).size().reset_index(name="n").itertuples(index=False)
        ):
            if team:
                pitch_stats[(team, int(season))]["cs"] += int(n)

    # Pitch-level count / zone control (hitter's perspective).
    valid_loc = chunk["PlateLocHeight"].notna() & chunk["PlateLocSide"].notna()
    loc = chunk.loc[valid_loc]
    if not loc.empty:
        in_zone = (
            loc["PlateLocSide"].abs().le(ZONE_SIDE)
            & loc["PlateLocHeight"].ge(ZONE_BOT)
            & loc["PlateLocHeight"].le(ZONE_TOP)
        )
        swung = loc["pcall"].isin(SWING_CALLS)
        loc = loc.assign(in_zone=in_zone, swung=swung)
        chase_mask = loc["swung"] & ~loc["in_zone"]
        iz_swing_mask = loc["swung"] & loc["in_zone"]
        chase_n = loc.loc[chase_mask].groupby(["BatterTeam", "season"], sort=False).size()
        iz_n = loc.loc[loc["in_zone"]].groupby(["BatterTeam", "season"], sort=False).size()
        iz_swing_n = loc.loc[iz_swing_mask].groupby(["BatterTeam", "season"], sort=False).size()
        pitch_n = loc.groupby(["BatterTeam", "season"], sort=False).size()
        out_n = loc.loc[~loc["in_zone"]].groupby(["BatterTeam", "season"], sort=False).size()
        for key, n in pitch_n.items():
            team, season = key
            if not team:
                continue
            b = pitch_stats[(team, int(season))]
            b["pitches"] += int(n)
            b["zone_pitches"] += int(n)
            b["in_zone"] += int(iz_n.get(key, 0))
            b["out_zone"] += int(out_n.get(key, 0))
            b["chases"] += int(chase_n.get(key, 0))
            b["in_zone_swings"] += int(iz_swing_n.get(key, 0))

    fp = chunk.loc[chunk["PitchofPA"] == 1]
    if not fp.empty:
        fp_n = fp.groupby(["BatterTeam", "season"], sort=False).size()
        fp_k = (
            fp.loc[fp["pcall"].isin(STRIKE_LIKE_CALLS)]
            .groupby(["BatterTeam", "season"], sort=False)
            .size()
        )
        for key, n in fp_n.items():
            team, season = key
            if not team:
                continue
            b = pitch_stats[(team, int(season))]
            b["first_pitches"] += int(n)
            b["first_pitch_strikes"] += int(fp_k.get(key, 0))

    # Last pitch of each plate appearance in this chunk; keep the max PitchofPA globally.
    pa_rows = chunk.dropna(subset=["Inning", "PAofInning", "PitchofPA"])
    if pa_rows.empty:
        return
    pa_rows = pa_rows.copy()
    pa_rows["Inning"] = pa_rows["Inning"].astype(int)
    pa_rows["PAofInning"] = pa_rows["PAofInning"].astype(int)
    pa_rows["PitchofPA"] = pa_rows["PitchofPA"].astype(int)
    idx = pa_rows.groupby(
        ["GameUID", "Inning", "half", "PAofInning"], sort=False
    )["PitchofPA"].idxmax()
    last = pa_rows.loc[idx]
    for row in last.itertuples(index=False):
        team = row.BatterTeam
        if not team:
            continue
        key = (str(row.GameUID), int(row.Inning), row.half, int(row.PAofInning))
        pitch_n = int(row.PitchofPA)
        prev = pa_last.get(key)
        if prev is None or pitch_n >= prev[0]:
            pa_last[key] = (
                pitch_n,
                team,
                int(row.season),
                row.play,
                row.korbb,
                row.pcall,
            )


def game_park_factor(home_team: str, pf_map: dict[str, float]) -> float:
    """Park factor for the stadium the game was played in (home team's park). Neutral = 100."""
    pf = pf_map.get(home_team)
    if pf is None or pf <= 0:
        return 100.0
    return float(pf)


def adjust_runs(runs: int, park_factor: float) -> float:
    return runs * 100.0 / park_factor


def build_team_games(game_meta: dict, game_runs: dict, pf_map: dict[str, float]) -> pd.DataFrame:
    rows = []
    for uid, meta in game_meta.items():
        home = meta["home"]
        away = meta["away"]
        if not home or not away or home == away:
            continue
        home_runs = int(game_runs.get((uid, home), 0))
        away_runs = int(game_runs.get((uid, away), 0))
        qualified = meta["max_inning"] >= MIN_INNINGS
        park_factor = game_park_factor(home, pf_map)
        rows.append(
            {
                "game_uid": uid,
                "season": meta["season"],
                "team": home,
                "opponent": away,
                "is_home": True,
                "runs": home_runs,
                "runs_allowed": away_runs,
                "adj_runs": adjust_runs(home_runs, park_factor),
                "park_factor": park_factor,
                "max_inning": meta["max_inning"],
                "qualified": qualified,
            }
        )
        rows.append(
            {
                "game_uid": uid,
                "season": meta["season"],
                "team": away,
                "opponent": home,
                "is_home": False,
                "runs": away_runs,
                "runs_allowed": home_runs,
                "adj_runs": adjust_runs(away_runs, park_factor),
                "park_factor": park_factor,
                "max_inning": meta["max_inning"],
                "qualified": qualified,
            }
        )
    return pd.DataFrame(rows)


def aggregate_offense(
    index: pd.DataFrame,
    games: pd.DataFrame,
    pitch_stats: dict,
    pa_last: dict,
    seasons: set[int],
    pf_map: dict[str, float],
) -> pd.DataFrame:
    pa_totals: dict[tuple[str, int], dict] = defaultdict(new_pa_bucket)
    for _key, (_n, team, season, play, korbb, pcall) in pa_last.items():
        add_pa_outcome(pa_totals[(team, season)], play, korbb, pcall)

    # Season-level RPG / RA for every tracked team (used for opponent strength).
    qual = games.loc[games["qualified"]].copy()
    team_season_all = (
        qual.groupby(["team", "season"], sort=False)
        .agg(
            games_used=("game_uid", "nunique"),
            runs_scored=("runs", "sum"),
            runs_allowed=("runs_allowed", "sum"),
            adj_runs_scored=("adj_runs", "sum"),
        )
        .reset_index()
    )
    team_season_all["rpg"] = team_season_all["runs_scored"] / team_season_all["games_used"]
    team_season_all["rag"] = team_season_all["runs_allowed"] / team_season_all["games_used"]
    team_season_all["adj_rpg"] = team_season_all["adj_runs_scored"] / team_season_all["games_used"]
    rpg_map = {
        (r.team, int(r.season)): (
            r.rpg,
            r.rag,
            r.adj_rpg,
            int(r.games_used),
            int(r.runs_scored),
            int(r.runs_allowed),
        )
        for r in team_season_all.itertuples(index=False)
    }

    games_found_map = (
        games.groupby(["team", "season"], sort=False)["game_uid"].nunique().to_dict()
    )

    # Opponent strength: game-weighted mean of opponent season RPG and RA/G.
    if not qual.empty:
        qual["opp_rpg"] = [
            rpg_map.get((opp, int(season)), (np.nan, np.nan, 0, 0, 0))[0]
            for opp, season in zip(qual["opponent"], qual["season"])
        ]
        qual["opp_rag"] = [
            rpg_map.get((opp, int(season)), (np.nan, np.nan, 0, 0, 0))[1]
            for opp, season in zip(qual["opponent"], qual["season"])
        ]
        sos = (
            qual.groupby(["team", "season"], sort=False)
            .agg(
                opp_avg_runs_per_game=("opp_rpg", "mean"),
                opp_runs_allowed_per_game=("opp_rag", "mean"),
            )
            .reset_index()
        )
        sos_map = {
            (r.team, int(r.season)): (r.opp_avg_runs_per_game, r.opp_runs_allowed_per_game)
            for r in sos.itertuples(index=False)
        }
    else:
        sos_map = {}

    season_list = sorted(seasons)
    rows = []
    for _, school in index.iterrows():
        code = school["trackman_code"]
        for season in season_list:
            key = (code, season)
            found = int(games_found_map.get(key, 0))
            rpg, rag, adj_rpg, used, runs, ra = rpg_map.get(
                key, (np.nan, np.nan, np.nan, 0, 0, 0)
            )
            pa = pa_totals.get(key, new_pa_bucket())
            pit = pitch_stats.get(key, new_pitch_bucket())
            opp_rpg, opp_rag = sos_map.get(key, (np.nan, np.nan))

            ab = pa["ab"]
            plate = pa["pa"]
            obp_d = pa["ab"] + pa["bb"] + pa["hbp"] + pa["sf_sh"]
            avg = rate(pa["hits"], ab)
            obp = rate(pa["reach_ob"], obp_d)
            slg = rate(pa["tb"], ab)
            ops = avg + slg if not (math.isnan(avg) or math.isnan(slg)) else float("nan")
            # True OPS uses OBP+SLG.
            ops = obp + slg if not (math.isnan(obp) or math.isnan(slg)) else float("nan")
            ebh = pa["doubles"] + pa["triples"] + pa["hr"]

            rows.append(
                {
                    "trackman_code": code,
                    "college_name": school["college_name"],
                    "full_college_name": school["full_college_name"],
                    "season": season,
                    "games_found": found,
                    "games_used": used,
                    "runs_scored": runs,
                    "runs_allowed": ra,
                    "runs_per_game": rpg,
                    "adj_runs_per_game": adj_rpg,
                    "parkfactor_100": pf_map.get(code, np.nan),
                    "runs_allowed_per_game": rag,
                    "pa": pa["pa"],
                    "ab": ab,
                    "hits": pa["hits"],
                    "avg": avg,
                    "obp": obp,
                    "slg": slg,
                    "ops": ops,
                    "doubles": pa["doubles"],
                    "triples": pa["triples"],
                    "hr": pa["hr"],
                    "hr_per_game": rate(pa["hr"], used),
                    "ebh": ebh,
                    "ebh_per_game": rate(ebh, used),
                    "sb": pit["sb"],
                    "sb_per_game": rate(pit["sb"], used),
                    "cs": pit["cs"],
                    "k": pa["k"],
                    "bb": pa["bb"],
                    "hitter_k_pct": rate(pa["k"], plate),
                    "hitter_bb_pct": rate(pa["bb"], plate),
                    "first_pitch_strike_pct": rate(
                        pit["first_pitch_strikes"], pit["first_pitches"]
                    ),
                    "zone_pct": rate(pit["in_zone"], pit["zone_pitches"]),
                    "chase_pct": rate(pit["chases"], pit["out_zone"]),
                    "in_zone_swing_pct": rate(pit["in_zone_swings"], pit["in_zone"]),
                    "opp_avg_runs_per_game": opp_rpg,
                    "opp_runs_allowed_per_game": opp_rag,
                }
            )
    out = pd.DataFrame(rows)
    return out.sort_values(["college_name", "season"]).reset_index(drop=True)


def pearson_spearman(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    if len(x) < 3:
        return float("nan"), float("nan")
    pearson = float(np.corrcoef(x, y)[0, 1])
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    spearman = float(np.corrcoef(rx, ry)[0, 1])
    return pearson, spearman


def correlation_table(stats: pd.DataFrame) -> pd.DataFrame:
    sample = stats.loc[stats["games_used"] >= MIN_GAMES_FOR_CORR].copy()
    y = sample["runs_per_game"].to_numpy(dtype=float)
    skip = {
        "trackman_code",
        "college_name",
        "full_college_name",
        "season",
        "games_found",
        "games_used",
        "runs_scored",
        "runs_per_game",
        "adj_runs_per_game",
        "pa",
        "ab",
        "hits",
        "doubles",
        "triples",
        "hr",
        "ebh",
        "sb",
        "cs",
        "k",
        "bb",
    }
    rows = []
    for col in sample.columns:
        if col in skip:
            continue
        x = pd.to_numeric(sample[col], errors="coerce").to_numpy(dtype=float)
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < MIN_GAMES_FOR_CORR:
            continue
        pearson, spearman = pearson_spearman(x[mask], y[mask])
        rows.append(
            {
                "metric": col,
                "pearson_r_vs_rpg": pearson,
                "spearman_r_vs_rpg": spearman,
                "n_team_seasons": int(mask.sum()),
                "abs_pearson_r": abs(pearson) if np.isfinite(pearson) else np.nan,
            }
        )
    corr = pd.DataFrame(rows).sort_values("abs_pearson_r", ascending=False)
    return corr.drop(columns=["abs_pearson_r"])


def top_percentile(stats: pd.DataFrame, column: str, q: float = 0.95) -> tuple[pd.DataFrame, float]:
    sample = stats.loc[
        (stats["games_used"] >= MIN_GAMES_FOR_CORR) & stats[column].notna()
    ].copy()
    cut = float(sample[column].quantile(q))
    top = sample.loc[sample[column] >= cut].sort_values(column, ascending=False)
    return top, cut


COMPARE_METRICS = [
    ("runs_per_game", "Runs / game"),
    ("adj_runs_per_game", "Adj runs / game"),
    ("parkfactor_100", "Park factor (100)"),
    ("ops", "OPS"),
    ("obp", "OBP"),
    ("slg", "SLG"),
    ("avg", "AVG"),
    ("ebh_per_game", "EBH / game"),
    ("hr_per_game", "HR / game"),
    ("hitter_bb_pct", "Hitter BB%"),
    ("hitter_k_pct", "Hitter K%"),
    ("chase_pct", "Chase%"),
    ("first_pitch_strike_pct", "1st-pitch strike seen"),
    ("sb_per_game", "SB / game"),
    ("runs_allowed_per_game", "RA / game"),
    ("opp_runs_allowed_per_game", "Opp RA / game"),
    ("opp_avg_runs_per_game", "Opp RPG"),
]


def vs_average_table(qualified: pd.DataFrame, top_rpg: pd.DataFrame, top_adj: pd.DataFrame) -> pd.DataFrame:
    avg = qualified.mean(numeric_only=True)
    rpg_m = top_rpg.mean(numeric_only=True)
    adj_m = top_adj.mean(numeric_only=True)
    rows = []
    for col, label in COMPARE_METRICS:
        if col not in avg:
            continue
        a, r, d = float(avg[col]), float(rpg_m[col]), float(adj_m[col])
        rows.append(
            {
                "metric": col,
                "label": label,
                "average": a,
                "top5_rpg": r,
                "top5_rpg_gap": r - a,
                "top5_rpg_pct_vs_avg": (r - a) / a * 100 if a else float("nan"),
                "top5_adj": d,
                "top5_adj_gap": d - a,
                "top5_adj_pct_vs_avg": (d - a) / a * 100 if a else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Team-season NCAA runs-per-game stats from TrackMan masters.")
    p.add_argument("--index", type=Path, default=INDEX_PATH)
    p.add_argument("--outdir", type=Path, default=ROOT)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    index = load_index(args.index)
    pf_map = load_park_factors(PARK_PATH)
    print(f"Index schools: {len(index)}")
    print(f"Park factors loaded: {len(pf_map)}")

    game_meta, game_runs, pitch_stats, pa_last, seasons = process_masters(MASTER_FILES)
    print(f"\nUnique games: {len(game_meta):,}")
    print(f"Seasons found: {sorted(seasons)}")
    print(f"Plate appearances stored: {len(pa_last):,}")

    games = build_team_games(game_meta, game_runs, pf_map)
    stats = aggregate_offense(index, games, pitch_stats, pa_last, seasons, pf_map)
    corr = correlation_table(stats)
    high = stats.loc[
        (stats["runs_per_game"] >= HIGH_RPG) & (stats["games_used"] >= MIN_GAMES_FOR_CORR)
    ].sort_values(["runs_per_game", "season"], ascending=[False, True])
    top_rpg, rpg_cut = top_percentile(stats, "runs_per_game", 0.95)
    top_adj, adj_cut = top_percentile(stats, "adj_runs_per_game", 0.95)
    qualified = stats.loc[
        (stats["games_used"] >= MIN_GAMES_FOR_CORR)
        & stats["runs_per_game"].notna()
        & stats["adj_runs_per_game"].notna()
    ]
    vs_avg = vs_average_table(qualified, top_rpg, top_adj)

    rate_cols = [
        "runs_per_game",
        "adj_runs_per_game",
        "parkfactor_100",
        "runs_allowed_per_game",
        "avg",
        "obp",
        "slg",
        "ops",
        "hr_per_game",
        "ebh_per_game",
        "sb_per_game",
        "hitter_k_pct",
        "hitter_bb_pct",
        "first_pitch_strike_pct",
        "zone_pct",
        "chase_pct",
        "in_zone_swing_pct",
        "opp_avg_runs_per_game",
        "opp_runs_allowed_per_game",
    ]
    present_rate = [c for c in rate_cols if c in stats.columns]
    stats[present_rate] = stats[present_rate].round(4)
    high[present_rate] = high[present_rate].round(4)
    top_rpg[present_rate] = top_rpg[present_rate].round(4)
    top_adj[present_rate] = top_adj[present_rate].round(4)
    vs_avg[
        [c for c in vs_avg.columns if c not in {"metric", "label"}]
    ] = vs_avg[[c for c in vs_avg.columns if c not in {"metric", "label"}]].round(4)
    corr[["pearson_r_vs_rpg", "spearman_r_vs_rpg"]] = corr[
        ["pearson_r_vs_rpg", "spearman_r_vs_rpg"]
    ].round(4)

    outdir = args.outdir
    stats_path = outdir / "team_season_offensive_stats.csv"
    corr_path = outdir / "rpg_metric_correlations.csv"
    high_path = outdir / "high_rpg_team_seasons.csv"
    top_rpg_path = outdir / "top5pct_rpg.csv"
    top_adj_path = outdir / "top5pct_adj_rpg.csv"
    vs_avg_path = outdir / "top5pct_vs_average.csv"
    stats.to_csv(stats_path, index=False)
    corr.to_csv(corr_path, index=False)
    high.to_csv(high_path, index=False)
    top_rpg.to_csv(top_rpg_path, index=False)
    top_adj.to_csv(top_adj_path, index=False)
    vs_avg.to_csv(vs_avg_path, index=False)

    print(f"\nWrote {stats_path} ({len(stats):,} rows)")
    print(f"Wrote {corr_path} ({len(corr):,} metrics)")
    print(
        f"Wrote {high_path} ({len(high):,} team-seasons at {HIGH_RPG}+ RPG "
        f"with {MIN_GAMES_FOR_CORR}+ games)"
    )
    print(f"Wrote {top_rpg_path} ({len(top_rpg):,} seasons, RPG >= {rpg_cut:.3f})")
    print(f"Wrote {top_adj_path} ({len(top_adj):,} seasons, adj RPG >= {adj_cut:.3f})")
    print(f"Wrote {vs_avg_path}")
    print("\nCorrelation with runs per game (min "
          f"{MIN_GAMES_FOR_CORR} games):")
    if not corr.empty:
        print(corr.head(20).to_string(index=False, float_format=lambda v: f"{v: .3f}"))
    print("\nSample (Oregon State / LSU):")
    sample = stats.loc[stats["college_name"].isin(["Oregon State", "LSU"])]
    cols = [
        "college_name",
        "season",
        "games_found",
        "games_used",
        "runs_per_game",
        "adj_runs_per_game",
        "parkfactor_100",
        "ops",
        "hitter_k_pct",
        "hitter_bb_pct",
        "opp_avg_runs_per_game",
        "opp_runs_allowed_per_game",
    ]
    print(sample[cols].to_string(index=False, float_format=lambda v: f"{v: .3f}"))
    print("\nTop 5% raw RPG / park-adjusted RPG:")
    cols_top = [
        "college_name",
        "season",
        "games_used",
        "runs_per_game",
        "adj_runs_per_game",
        "parkfactor_100",
        "ops",
    ]
    print(top_rpg[cols_top].head(10).to_string(index=False, float_format=lambda v: f"{v: .3f}"))
    print(top_adj[cols_top].head(10).to_string(index=False, float_format=lambda v: f"{v: .3f}"))


if __name__ == "__main__":
    main()
