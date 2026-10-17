#!/usr/bin/env python3
"""Analyze temporal arbitrage spreads logged by a Solana price scanner.

The scanner writes one row per venue quote per scan to ``spreads.csv``. A wide
spread seen in a single scan is usually noise, so this tool answers a narrower
question: which spreads *persist* across consecutive scans, once frozen venue
quotes have been removed.

Usage
-----
    python analyze_spreads.py spreads.csv [--threshold 30] [--min-scans 3]
        [--max-gap 300] [--stale-scans 5] [--out report.md] [--runs-csv runs.csv]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

BEST_VENUE = "Jupiter (best)"

# Spreads are ratios of floats: 100.3/100 - 1 lands just under 0.003, so a
# comparison against an exact boundary needs a tolerance smaller than any
# meaningful bps difference.
EPS = 1e-9

REQUIRED_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "slot",
    "pair",
    "amount",
    "venue",
    "out_amount",
    "price",
    "vs_best_bps",
    "impact_pct",
    "route",
    "cross_venue_spread_bps",
    "round_trip_bps",
)
OPTIONAL_COLUMNS: tuple[str, ...] = ("stale",)

NUMERIC_COLUMNS: tuple[str, ...] = (
    "slot",
    "amount",
    "out_amount",
    "price",
    "vs_best_bps",
    "impact_pct",
    "cross_venue_spread_bps",
    "round_trip_bps",
)

# A row is only usable when these survive coercion.
KEY_COLUMNS: tuple[str, ...] = ("timestamp", "pair", "venue", "out_amount", "price")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Params:
    """Analysis parameters, mirroring the CLI flags."""

    threshold: float = 30.0
    min_scans: int = 3
    max_gap: float = 300.0
    stale_scans: int = 5


@dataclass
class FrozenPeriod:
    """One contiguous stretch of identical quotes from a single venue."""

    pair: str
    venue: str
    start: datetime
    end: datetime
    duration_s: float
    out_amount: float
    distance_bps: float


@dataclass
class Run:
    """A persistent sequence of anomalous scans for one pair."""

    pair: str
    start: datetime
    end: datetime
    duration_s: float
    scans: int
    mean_spread: float
    max_spread: float
    dominant_outlier: str
    mean_round_trip: float | None


@dataclass
class LoadResult:
    """Loaded frame plus the accounting needed for the report header."""

    frame: pd.DataFrame
    skipped: int
    has_stale_column: bool


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #
def _count_data_lines(path: Path) -> int:
    """Count non-blank data lines, excluding the header row."""
    total = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index == 0:
                continue
            if line.strip():
                total += 1
    return total


def load_spreads(path: Path) -> LoadResult:
    """Read the CSV, coerce types and drop rows that cannot be used.

    Structural damage (a row with the wrong number of fields) and unparseable
    values are both counted, never raised, so one bad line cannot kill a run.
    """
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")

    raw = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        on_bad_lines="skip",
        encoding="utf-8-sig",
    )

    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(
            "missing required column(s): "
            + ", ".join(missing)
            + f" (found: {', '.join(raw.columns)})"
        )

    has_stale = "stale" in raw.columns
    structural_skips = max(_count_data_lines(path) - len(raw), 0)

    frame = raw.copy()
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"].str.strip(), errors="coerce", utc=True
    )
    for column in NUMERIC_COLUMNS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(
                frame[column].str.strip().replace("", None), errors="coerce"
            )

    frame["pair"] = frame["pair"].str.strip()
    frame["venue"] = frame["venue"].str.strip()

    if has_stale:
        stale_text = frame["stale"].str.strip().str.lower()
        frame["stale_flag"] = stale_text.map({"true": True, "false": False})
    else:
        frame["stale_flag"] = pd.NA

    usable = pd.Series(True, index=frame.index)
    for column in KEY_COLUMNS:
        usable &= frame[column].notna()
    usable &= frame["pair"].ne("")
    usable &= frame["venue"].ne("")

    value_skips = int((~usable).sum())
    frame = frame.loc[usable].reset_index(drop=True)
    frame = frame.sort_values(["pair", "timestamp", "venue"]).reset_index(drop=True)

    return LoadResult(
        frame=frame,
        skipped=structural_skips + value_skips,
        has_stale_column=has_stale,
    )


# --------------------------------------------------------------------------- #
# Frozen-quote detection
# --------------------------------------------------------------------------- #
def _best_route_changes(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach the best route's out_amount and whether it moved since last scan."""
    best = (
        frame.loc[frame["venue"] == BEST_VENUE, ["pair", "timestamp", "out_amount"]]
        .drop_duplicates(subset=["pair", "timestamp"])
        .sort_values(["pair", "timestamp"])
        .rename(columns={"out_amount": "best_out"})
    )
    best["best_changed"] = best.groupby("pair")["best_out"].diff().ne(0)
    first = best.groupby("pair").cumcount() == 0
    best.loc[first, "best_changed"] = False
    return best[["pair", "timestamp", "best_out", "best_changed"]]


def detect_frozen(frame: pd.DataFrame, stale_scans: int) -> tuple[pd.DataFrame, list[FrozenPeriod]]:
    """Flag frozen venue quotes and summarise each frozen period.

    A quote is a *repeat* when its ``out_amount`` equals that venue's previous
    quote for the pair **and** the best route moved in between. Repeats are
    counted per (pair, venue); a changing quote resets the counter, and a flat
    market leaves it untouched. Once ``stale_scans - 1`` repeats have
    accumulated the whole identical stretch is marked frozen, because those
    earlier copies are equally fake.
    """
    frame = frame.copy()
    best = _best_route_changes(frame)
    frame = frame.merge(best, on=["pair", "timestamp"], how="left")
    frame["best_changed"] = frame["best_changed"].fillna(False).astype(bool)
    frame["frozen"] = False

    for (pair, venue), group in frame.groupby(["pair", "venue"], sort=False):
        if venue == BEST_VENUE:
            continue
        positions = group.index.to_numpy()
        outs = group["out_amount"].to_numpy()
        moved = group["best_changed"].to_numpy()

        repeats = 0
        streak: list[int] = []
        previous: float | None = None

        for offset in range(len(positions)):
            value = outs[offset]
            if previous is not None and value == previous:
                if moved[offset]:
                    repeats += 1
                streak.append(int(positions[offset]))
            else:
                repeats = 0
                streak = [int(positions[offset])]

            if repeats >= stale_scans - 1:
                frame.loc[streak, "frozen"] = True
            previous = value

    periods = _frozen_periods(frame)
    return frame, periods


def _frozen_periods(frame: pd.DataFrame) -> list[FrozenPeriod]:
    """Collapse consecutive frozen rows of a (pair, venue) into periods."""
    frozen = frame.loc[frame["frozen"]].copy()
    if frozen.empty:
        return []

    frozen = frozen.sort_values(["pair", "venue", "timestamp"])
    frozen["distance"] = (frozen["best_out"] / frozen["out_amount"] - 1.0) * 10_000.0
    gaps = frozen.groupby(["pair", "venue"], sort=False)["timestamp"].diff().dt.total_seconds()
    starts_block = gaps.isna() | (gaps > 600)
    frozen["block"] = starts_block.groupby(
        [frozen["pair"], frozen["venue"]], sort=False
    ).cumsum()

    grouped = frozen.groupby(["pair", "venue", "block"], sort=False).agg(
        start=("timestamp", "min"),
        end=("timestamp", "max"),
        out_amount=("out_amount", "last"),
        distance=("distance", "mean"),
    )

    periods: list[FrozenPeriod] = []
    for (pair, venue, _block), row in grouped.iterrows():
        start_ts = row["start"].to_pydatetime()
        end_ts = row["end"].to_pydatetime()
        periods.append(
            FrozenPeriod(
                pair=pair,
                venue=venue,
                start=start_ts,
                end=end_ts,
                duration_s=(end_ts - start_ts).total_seconds(),
                out_amount=float(row["out_amount"]),
                distance_bps=float(row["distance"]),
            )
        )
    return periods


# --------------------------------------------------------------------------- #
# Per-scan aggregation
# --------------------------------------------------------------------------- #
def collapse_scans(frame: pd.DataFrame) -> pd.DataFrame:
    """Reduce venue rows to one row per (pair, timestamp) with a recomputed spread."""
    live = frame.loc[(frame["venue"] != BEST_VENUE) & (~frame["frozen"])]

    if live.empty:
        return _empty_scans()

    grouped = live.groupby(["pair", "timestamp"]).agg(
        max_price=("price", "max"),
        min_price=("price", "min"),
        venues=("price", "size"),
    )
    grouped["spread_bps"] = (
        (grouped["max_price"] / grouped["min_price"] - 1.0) * 10_000.0
    ).round(6)
    grouped.loc[grouped["venues"] < 2, "spread_bps"] = float("nan")

    round_trip = (
        frame.dropna(subset=["round_trip_bps"])[
            ["pair", "timestamp", "round_trip_bps"]
        ]
        .drop_duplicates(subset=["pair", "timestamp"], keep="first")
    )

    candidates = live.loc[live["vs_best_bps"].notna()]
    if candidates.empty:
        outlier = pd.DataFrame(
            {
                "pair": pd.Series(dtype="object"),
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                "outlier_venue": pd.Series(dtype="object"),
            }
        )
    else:
        idx = candidates.groupby(["pair", "timestamp"])["vs_best_bps"].idxmin()
        outlier = (
            candidates.loc[idx, ["pair", "timestamp", "venue"]]
            .rename(columns={"venue": "outlier_venue"})
            .reset_index(drop=True)
        )

    scans = grouped.reset_index().merge(round_trip, on=["pair", "timestamp"], how="left")
    scans = scans.merge(outlier, on=["pair", "timestamp"], how="left")
    return scans.sort_values(["pair", "timestamp"]).reset_index(drop=True)


def _empty_scans() -> pd.DataFrame:
    """A scans frame with the right dtypes, for input where nothing survived."""
    return pd.DataFrame(
        {
            "pair": pd.Series(dtype="object"),
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "max_price": pd.Series(dtype="float64"),
            "min_price": pd.Series(dtype="float64"),
            "venues": pd.Series(dtype="int64"),
            "spread_bps": pd.Series(dtype="float64"),
            "round_trip_bps": pd.Series(dtype="float64"),
            "outlier_venue": pd.Series(dtype="object"),
        }
    )


def find_runs(scans: pd.DataFrame, params: Params) -> list[Run]:
    """Group consecutive anomalous scans into runs, keeping the persistent ones."""
    if scans.empty:
        return []

    ordered = scans.sort_values(["pair", "timestamp"]).copy()
    ordered["anomalous"] = ordered["spread_bps"].notna() & (
        ordered["spread_bps"] >= params.threshold - EPS
    )

    gap = ordered.groupby("pair", sort=False)["timestamp"].diff().dt.total_seconds()
    previous_anomalous = ordered.groupby("pair", sort=False)["anomalous"].shift(fill_value=False)
    starts_run = ordered["anomalous"] & (~previous_anomalous | (gap > params.max_gap))
    ordered["run_id"] = starts_run.groupby(ordered["pair"], sort=False).cumsum()

    hot = ordered.loc[ordered["anomalous"]]
    if hot.empty:
        return []

    grouped = hot.groupby(["pair", "run_id"], sort=False)
    runs: list[Run] = []
    for (pair, _run_id), group in grouped:
        if len(group) < params.min_scans:
            continue
        start = group["timestamp"].min().to_pydatetime()
        end = group["timestamp"].max().to_pydatetime()
        outliers = group["outlier_venue"].dropna()
        runs.append(
            Run(
                pair=pair,
                start=start,
                end=end,
                duration_s=(end - start).total_seconds(),
                scans=len(group),
                mean_spread=float(group["spread_bps"].mean()),
                max_spread=float(group["spread_bps"].max()),
                dominant_outlier=(
                    str(outliers.mode().iloc[0]) if not outliers.empty else "unknown"
                ),
                mean_round_trip=(
                    float(group["round_trip_bps"].mean())
                    if group["round_trip_bps"].notna().any()
                    else None
                ),
            )
        )

    return sorted(runs, key=lambda r: r.duration_s, reverse=True)


# --------------------------------------------------------------------------- #
# Statistics and report
# --------------------------------------------------------------------------- #
def pair_statistics(
    scans: pd.DataFrame, runs: Sequence[Run], frozen_counts: dict[str, int], params: Params
) -> list[dict[str, object]]:
    """Per-pair summary rows for the report table."""
    rows: list[dict[str, object]] = []
    for pair, group in scans.groupby("pair", sort=False):
        group = group.sort_values("timestamp")
        spreads = group["spread_bps"].dropna()
        round_trips = group["round_trip_bps"].dropna()
        pair_runs = [r for r in runs if r.pair == pair]
        longest = max(pair_runs, key=lambda r: r.duration_s) if pair_runs else None
        covered = (
            (group["timestamp"].max() - group["timestamp"].min()).total_seconds()
            if len(group) > 1
            else 0.0
        )
        rows.append(
            {
                "pair": pair,
                "scans": len(group),
                "covered_s": covered,
                "pct_at_threshold": (
                    100.0 * float((spreads >= params.threshold - EPS).sum()) / len(spreads)
                    if len(spreads)
                    else 0.0
                ),
                "median": float(spreads.median()) if len(spreads) else float("nan"),
                "p95": float(spreads.quantile(0.95)) if len(spreads) else float("nan"),
                "max": float(spreads.max()) if len(spreads) else float("nan"),
                "persistent_runs": len(pair_runs),
                "longest_scans": longest.scans if longest else 0,
                "longest_s": longest.duration_s if longest else 0.0,
                "median_round_trip": (
                    float(round_trips.median()) if len(round_trips) else float("nan")
                ),
                "pct_positive_round_trip": (
                    100.0 * float((round_trips > 0).sum()) / len(round_trips)
                    if len(round_trips)
                    else 0.0
                ),
                "frozen_excluded": frozen_counts.get(pair, 0),
            }
        )
    return rows


def format_duration(seconds: float) -> str:
    """Render a duration as ``2h 05m`` / ``7m 30s`` / ``45s``."""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _fmt(value: float | None, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/a"
    return f"{value:.2f}{suffix}"


def build_report(
    *,
    source: Path,
    load: LoadResult,
    scans: pd.DataFrame,
    runs: Sequence[Run],
    periods: Sequence[FrozenPeriod],
    stats: Sequence[dict[str, object]],
    params: Params,
    stale_disagreements: int,
) -> str:
    """Render the Markdown report."""
    frame = load.frame
    if frame.empty:
        start = end = None
    else:
        start = frame["timestamp"].min().to_pydatetime()
        end = frame["timestamp"].max().to_pydatetime()
    lines: list[str] = []

    lines.append("# Spread Persistence Report")
    lines.append("")
    lines.append(f"- Input file: `{source}`")
    if start is None:
        lines.append("- Time range: n/a (no usable rows)")
    else:
        lines.append(
            f"- Time range: {start.strftime('%Y-%m-%d %H:%M:%S')} → "
            f"{end.strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
    lines.append(f"- Scans analysed: {len(scans)} · rows loaded: {len(frame)} · rows skipped: {load.skipped}")
    lines.append(
        "- Parameters: "
        f"threshold {params.threshold:g} bps · min-scans {params.min_scans} · "
        f"max-gap {params.max_gap:g}s · stale-scans {params.stale_scans}"
    )
    lines.append("")

    lines.append("## Per-pair summary")
    lines.append("")
    lines.append(
        "| Pair | Scans | Covered | ≥ threshold | Median | p95 | Max | Persistent runs | Longest run | Median round trip | Positive round trip | Frozen excluded |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in stats:
        lines.append(
            "| {pair} | {scans} | {covered} | {pct:.1f}% | {med} | {p95} | {mx} | {runs} | {longest} | {rt} | {pos:.1f}% | {frozen} |".format(
                pair=row["pair"],
                scans=row["scans"],
                covered=format_duration(float(row["covered_s"])),
                pct=float(row["pct_at_threshold"]),
                med=_fmt(row["median"]),
                p95=_fmt(row["p95"]),
                mx=_fmt(row["max"]),
                runs=row["persistent_runs"],
                longest=(
                    f"{row['longest_scans']} scans / {format_duration(float(row['longest_s']))}"
                    if row["longest_scans"]
                    else "—"
                ),
                rt=_fmt(row["median_round_trip"]),
                pos=float(row["pct_positive_round_trip"]),
                frozen=row["frozen_excluded"],
            )
        )
    lines.append("")

    lines.append("## Persistent runs")
    lines.append("")
    if runs:
        lines.append("| Pair | Start | End | Duration | Scans | Mean spread | Max spread | Dominant outlier | Mean round trip |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for run in runs:
            lines.append(
                f"| {run.pair} | {run.start.strftime('%H:%M:%S')} | {run.end.strftime('%H:%M:%S')} | "
                f"{format_duration(run.duration_s)} | {run.scans} | {run.mean_spread:.2f} | "
                f"{run.max_spread:.2f} | {run.dominant_outlier} | {_fmt(run.mean_round_trip)} |"
            )
    else:
        lines.append("_No persistent runs at the requested threshold._")
    lines.append("")

    lines.append("## Outlier venues")
    lines.append("")
    anomalous = scans.loc[
        scans["spread_bps"].notna() & (scans["spread_bps"] >= params.threshold - EPS)
    ]
    if not anomalous.empty:
        counts = (
            anomalous.dropna(subset=["outlier_venue"])
            .groupby(["pair", "outlier_venue"]).size()
            .reset_index(name="count")
        )
        totals = counts.groupby("pair")["count"].transform("sum")
        counts["share"] = 100.0 * counts["count"] / totals
        counts = counts.sort_values(["pair", "count"], ascending=[True, False])
        lines.append("| Pair | Venue | Anomalous scans as outlier | Share |")
        lines.append("|---|---|---|---|")
        for _, row in counts.iterrows():
            lines.append(f"| {row['pair']} | {row['outlier_venue']} | {int(row['count'])} | {row['share']:.1f}% |")
    else:
        lines.append("_No anomalous scans, so no outlier venues._")
    lines.append("")

    lines.append("## Frozen quotes")
    lines.append("")
    if periods:
        lines.append("| Pair | Venue | Start | End | Duration | Frozen out_amount | Distance from best |")
        lines.append("|---|---|---|---|---|---|---|")
        for period in periods:
            lines.append(
                f"| {period.pair} | {period.venue} | {period.start.strftime('%H:%M:%S')} | "
                f"{period.end.strftime('%H:%M:%S')} | {format_duration(period.duration_s)} | "
                f"{period.out_amount:.6f} | {period.distance_bps:.2f} bps |"
            )
    else:
        lines.append("_No frozen quotes detected._")
    lines.append("")
    lines.append(f"Disagreements with the `stale` column: **{stale_disagreements}**")
    lines.append("")

    lines.append("## Findings")
    lines.append("")
    for finding in derive_findings(stats, runs, periods, params):
        lines.append(f"- {finding}")
    lines.append("")

    return "\n".join(lines)


def derive_findings(
    stats: Sequence[dict[str, object]],
    runs: Sequence[Run],
    periods: Sequence[FrozenPeriod],
    params: Params,
) -> list[str]:
    """Write two to four observations that are actually computed from the data."""
    findings: list[str] = []

    hot = [row for row in stats if float(row["pct_at_threshold"]) > 0]
    for row in sorted(hot, key=lambda r: float(r["pct_at_threshold"]), reverse=True)[:2]:
        findings.append(
            f"{row['pair']}: spread ≥ {params.threshold:g} bps in "
            f"{float(row['pct_at_threshold']):.0f}% of {row['scans']} scans "
            f"(median {_fmt(row['median'])} bps, max {_fmt(row['max'])} bps)."
        )

    if runs:
        longest = runs[0]
        findings.append(
            f"The longest persistent run is {longest.pair}, {format_duration(longest.duration_s)} "
            f"across {longest.scans} scans, dominated by {longest.dominant_outlier} "
            f"(mean spread {longest.mean_spread:.2f} bps)."
        )
    else:
        findings.append(
            "No spread stayed above the threshold for the required number of consecutive scans."
        )

    if periods:
        worst = max(periods, key=lambda p: p.distance_bps)
        findings.append(
            f"{len(periods)} frozen quote period(s) were excluded; the worst was {worst.venue} on "
            f"{worst.pair}, pinned at {worst.out_amount:.6f} for {format_duration(worst.duration_s)}, "
            f"a fake {worst.distance_bps:.0f} bps away from the best route."
        )
    else:
        findings.append("No frozen venue quotes were present, so no spreads needed correcting.")

    positive = [float(row["pct_positive_round_trip"]) for row in stats]
    if positive:
        findings.append(
            f"Round trip was positive in {sum(positive) / len(positive):.0f}% of scans on average, "
            "which is the share of the time a round trip would have cleared the spread."
        )

    return findings[:4]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect persistent arbitrage spreads and frozen venue quotes."
    )
    parser.add_argument("csv", type=Path, help="path to spreads.csv")
    parser.add_argument("--threshold", type=float, default=30.0, help="spread in bps that counts as anomalous (default: 30)")
    parser.add_argument("--min-scans", type=int, default=3, help="consecutive anomalous scans needed for a run (default: 3)")
    parser.add_argument("--max-gap", type=float, default=300.0, help="seconds between scans that breaks a run (default: 300)")
    parser.add_argument("--stale-scans", type=int, default=5, help="identical quotes that mark a venue frozen (default: 5)")
    parser.add_argument("--out", type=Path, default=None, help="write the Markdown report here (default: stdout)")
    parser.add_argument("--runs-csv", type=Path, default=None, help="also export the persistent runs table as CSV")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    params = Params(
        threshold=args.threshold,
        min_scans=args.min_scans,
        max_gap=args.max_gap,
        stale_scans=args.stale_scans,
    )

    try:
        load = load_spreads(args.csv)
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    frame, periods = detect_frozen(load.frame, params.stale_scans)
    scans = collapse_scans(frame)
    runs = find_runs(scans, params)

    if load.has_stale_column and "stale_flag" in frame.columns:
        declared = frame["stale_flag"].fillna(False).astype(bool)
        disagreements = int((declared != frame["frozen"]).sum())
    else:
        disagreements = 0

    frozen_counts = (
        frame.loc[frame["frozen"]].groupby("pair").size().to_dict() if frame["frozen"].any() else {}
    )
    stats = pair_statistics(scans, runs, frozen_counts, params)

    report = build_report(
        source=args.csv,
        load=load,
        scans=scans,
        runs=runs,
        periods=periods,
        stats=stats,
        params=params,
        stale_disagreements=disagreements,
    )

    if args.out:
        args.out.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)

    if args.runs_csv:
        pd.DataFrame(
            [
                {
                    "pair": run.pair,
                    "start": run.start.isoformat(),
                    "end": run.end.isoformat(),
                    "duration_s": round(run.duration_s, 2),
                    "scans": run.scans,
                    "mean_spread_bps": round(run.mean_spread, 2),
                    "max_spread_bps": round(run.max_spread, 2),
                    "dominant_outlier": run.dominant_outlier,
                    "mean_round_trip_bps": (
                        round(run.mean_round_trip, 2) if run.mean_round_trip is not None else None
                    ),
                }
                for run in runs
            ]
        ).to_csv(args.runs_csv, index=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
