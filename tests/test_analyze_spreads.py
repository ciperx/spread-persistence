"""Tests for analyze_spreads.py.

Every fixture is generated in-test, so the suite has no external data files.
Venue prices drift between scans unless a test is explicitly about frozen
quotes, because a genuinely moving market must never be flagged as stale.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import analyze_spreads as mod
from analyze_spreads import Params

HEADER_13 = (
    "timestamp,slot,pair,amount,venue,out_amount,price,vs_best_bps,"
    "impact_pct,route,cross_venue_spread_bps,round_trip_bps,stale"
)
HEADER_12 = (
    "timestamp,slot,pair,amount,venue,out_amount,price,vs_best_bps,"
    "impact_pct,route,cross_venue_spread_bps,round_trip_bps"
)

BASE = datetime(2026, 10, 17, 16, 0, 0, tzinfo=timezone.utc)


def ts(seconds: int) -> str:
    """ISO-8601 UTC stamp ``seconds`` after the fixture epoch."""
    return (BASE + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def scan(
    seconds: int,
    pair: str,
    best: float,
    venues: dict[str, float],
    *,
    round_trip: str = "1.50",
    stale: str = "",
    route_comma: bool = False,
) -> list[str]:
    """Build the rows of a single scan: the best route plus one row per venue."""
    rows = [
        f"{ts(seconds)},1,{pair},1,Jupiter (best),{best},{best},,0,BisonFi,0,{round_trip},"
    ]
    for venue, price in venues.items():
        route = f'"{venue}, routed"' if route_comma else venue
        rows.append(
            f"{ts(seconds)},1,{pair},1,{venue},{price},{price},-10,0.1,{route},0,{round_trip},{stale}"
        )
    return rows


def write(tmp_path: Path, rows: list[str], header: str = HEADER_13) -> Path:
    path = tmp_path / "spreads.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def moving_venues(index: int, bps: float) -> dict[str, float]:
    """Two venues ``bps`` apart, both drifting so neither looks frozen."""
    low = 100.0 + index * 0.01
    return {"Raydium": round(low, 10), "Whirlpool": round(low * (1 + bps / 10_000), 10)}


def analyse(path: Path, **kwargs) -> tuple[pd.DataFrame, list, list, mod.LoadResult, list]:
    """Run the pipeline and hand back every intermediate stage."""
    params = Params(**kwargs)
    load = mod.load_spreads(path)
    frame, periods = mod.detect_frozen(load.frame, params.stale_scans)
    scans = mod.collapse_scans(frame)
    runs = mod.find_runs(scans, params)
    stats = mod.pair_statistics(
        scans,
        runs,
        frame.loc[frame["frozen"]].groupby("pair").size().to_dict(),
        params,
    )
    return scans, runs, periods, load, stats


# --------------------------------------------------------------------------- #
# Runs: threshold, min-scans, max-gap
# --------------------------------------------------------------------------- #
def test_run_of_exactly_min_scans_is_persistent(tmp_path: Path) -> None:
    rows: list[str] = []
    for i in range(3):
        rows += scan(i * 60, "SOL/USDC", 100.0 + i * 0.1, moving_venues(i, 40))
    _, runs, _, _, _ = analyse(write(tmp_path, rows), min_scans=3)

    assert len(runs) == 1
    assert runs[0].scans == 3
    assert runs[0].duration_s == 120


def test_run_of_one_scan_short_is_not_persistent(tmp_path: Path) -> None:
    rows: list[str] = []
    for i in range(2):
        rows += scan(i * 60, "SOL/USDC", 100.0 + i * 0.1, moving_venues(i, 40))
    _, runs, _, _, _ = analyse(write(tmp_path, rows), min_scans=3)

    assert runs == []


def test_gap_larger_than_max_gap_breaks_the_run(tmp_path: Path) -> None:
    """Two scans, a restart gap, then two more: never three in a row."""
    times = [0, 60, 580, 640]
    rows: list[str] = []
    for i, second in enumerate(times):
        rows += scan(second, "SOL/USDC", 100.0 + i * 0.1, moving_venues(i, 40))

    _, runs, _, _, _ = analyse(write(tmp_path, rows), min_scans=3, max_gap=300)
    assert runs == []

    _, relaxed, _, _, _ = analyse(write(tmp_path, rows), min_scans=3, max_gap=600)
    assert len(relaxed) == 1
    assert relaxed[0].scans == 4


def test_one_scan_below_threshold_breaks_the_run(tmp_path: Path) -> None:
    rows: list[str] = []
    for i, bps in enumerate([40, 40, 10, 40, 40]):
        rows += scan(i * 60, "SOL/USDC", 100.0 + i * 0.1, moving_venues(i, bps))

    _, runs, _, _, _ = analyse(write(tmp_path, rows), min_scans=3)
    assert runs == []


def test_threshold_boundary_counts(tmp_path: Path) -> None:
    """A spread of exactly 30 bps must count as anomalous."""
    rows: list[str] = []
    for i in range(3):
        rows += scan(i * 60, "SOL/USDC", 100.0 + i * 0.1, moving_venues(i, 30))

    scans, runs, _, _, _ = analyse(write(tmp_path, rows), threshold=30, min_scans=3)
    assert scans["spread_bps"].dropna().iloc[0] == pytest.approx(30.0, abs=1e-6)
    assert len(runs) == 1


def test_runs_are_computed_independently_per_pair(tmp_path: Path) -> None:
    rows: list[str] = []
    for i in range(3):
        rows += scan(i * 60, "SOL/USDC", 100.0 + i * 0.1, moving_venues(i, 45))
    for i in range(2):
        rows += scan(i * 60, "JUP/USDC", 1.0 + i * 0.01, moving_venues(i, 45))

    _, runs, _, _, stats = analyse(write(tmp_path, rows), min_scans=3)

    assert [r.pair for r in runs] == ["SOL/USDC"]
    assert {row["pair"] for row in stats} == {"SOL/USDC", "JUP/USDC"}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_malformed_rows_are_skipped_and_counted(tmp_path: Path) -> None:
    rows = scan(0, "SOL/USDC", 100.0, moving_venues(0, 40))
    rows.append("not-a-timestamp,1,SOL/USDC,1,Raydium,100,100,-10,0.1,Raydium,0,1.5,")
    rows.append("2026-10-17T16:01:00.000Z,1,SOL/USDC,1,Raydium,oops,oops,-10,0.1,Raydium,0,1.5,")

    load = mod.load_spreads(write(tmp_path, rows))
    assert load.skipped == 2
    assert len(load.frame) == len(rows) - 2


def test_route_with_a_comma_survives(tmp_path: Path) -> None:
    rows = scan(0, "SOL/USDC", 100.0, moving_venues(0, 40), route_comma=True)
    load = mod.load_spreads(write(tmp_path, rows))

    assert len(load.frame) == 3
    assert "routed" in load.frame["route"].iloc[1]


def test_empty_round_trip_is_tolerated(tmp_path: Path) -> None:
    rows = scan(0, "SOL/USDC", 100.0, moving_venues(0, 40), round_trip="")
    scans, _, _, _, stats = analyse(write(tmp_path, rows))

    assert scans["round_trip_bps"].isna().all()
    assert stats[0]["median_round_trip"] != stats[0]["median_round_trip"]  # NaN


def test_missing_column_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / "broken.csv"
    path.write_text("timestamp,pair,venue\n2026-10-17T16:00:00Z,SOL/USDC,Raydium\n")

    with pytest.raises(ValueError, match="missing required column"):
        mod.load_spreads(path)


def test_scan_with_a_single_venue_has_no_spread(tmp_path: Path) -> None:
    rows: list[str] = []
    for i in range(3):
        rows += scan(i * 60, "SOL/USDC", 100.0 + i * 0.1, {"Raydium": 100.0 + i * 0.01})

    scans, runs, _, _, _ = analyse(write(tmp_path, rows), min_scans=3)
    assert scans["spread_bps"].isna().all()
    assert runs == []


def test_everything_frozen_still_reports(tmp_path: Path) -> None:
    """A file where no live quote survives must render, not crash."""
    rows: list[str] = []
    for i in range(5):
        rows += scan(i * 105, "SOL/USDC", 103.0 + 0.05 * i, {"Raydium": 103.0, "Phoenix": 89.0})

    out = tmp_path / "report.md"
    assert mod.main([str(write(tmp_path, rows)), "--out", str(out)]) == 0
    assert "Frozen quotes" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Frozen quotes
# --------------------------------------------------------------------------- #
def frozen_rows(scans: int, *, move_best: bool = True, stale: str = "") -> list[str]:
    """``scans`` scans where Phoenix is pinned at 89.0 while the market moves."""
    rows: list[str] = []
    for i in range(scans):
        best = 103.0 + (0.05 * i if move_best else 0.0)
        rows += scan(
            i * 105,
            "SOL/USDC",
            best,
            {"Raydium": 103.0 + 0.01 * i, "Whirlpool": 103.05 + 0.01 * i, "Phoenix": 89.0},
            stale=stale,
        )
    return rows


def test_five_identical_quotes_while_market_moves_is_frozen(tmp_path: Path) -> None:
    frame, periods = mod.detect_frozen(
        mod.load_spreads(write(tmp_path, frozen_rows(5))).frame, stale_scans=5
    )
    phoenix = frame.loc[frame["venue"] == "Phoenix"]

    assert phoenix["frozen"].all()
    assert len(periods) == 1
    assert periods[0].venue == "Phoenix"
    assert periods[0].start == BASE
    assert periods[0].end == BASE + timedelta(seconds=4 * 105)


def test_four_identical_quotes_are_not_frozen(tmp_path: Path) -> None:
    frame, periods = mod.detect_frozen(
        mod.load_spreads(write(tmp_path, frozen_rows(4))).frame, stale_scans=5
    )
    assert not frame.loc[frame["venue"] == "Phoenix", "frozen"].any()
    assert periods == []


def test_identical_quotes_in_a_flat_market_are_not_frozen(tmp_path: Path) -> None:
    frame, periods = mod.detect_frozen(
        mod.load_spreads(write(tmp_path, frozen_rows(8, move_best=False))).frame,
        stale_scans=5,
    )
    assert not frame["frozen"].any()
    assert periods == []


def test_frozen_venue_fake_spread_disappears(tmp_path: Path) -> None:
    """Phoenix's 1,500 bps fake spread must vanish once the quote is excluded."""
    path = write(tmp_path, frozen_rows(6))
    scans, runs, periods, _, _ = analyse(path, threshold=30, min_scans=1)

    assert scans["spread_bps"].max() == pytest.approx(4.85, abs=0.01)
    assert runs == []
    assert any(p.venue == "Phoenix" for p in periods)


def test_twelve_column_file_without_stale_loads(tmp_path: Path) -> None:
    rows = [r.rsplit(",", 1)[0] for r in frozen_rows(5)]
    load = mod.load_spreads(write(tmp_path, rows, header=HEADER_12))

    assert load.has_stale_column is False
    assert len(load.frame) == 20


def test_stale_column_disagreements_are_counted(tmp_path: Path) -> None:
    """A wrong ``stale`` flag must be reported, never raised."""
    rows = frozen_rows(5)
    # Claim the very first Phoenix row is stale, though it cannot be frozen yet.
    rows[3] = rows[3][:-5] + ",true"

    load = mod.load_spreads(write(tmp_path, rows))
    frame, _ = mod.detect_frozen(load.frame, stale_scans=5)
    declared = frame["stale_flag"].fillna(False).astype(bool)
    assert int((declared != frame["frozen"]).sum()) >= 1


# --------------------------------------------------------------------------- #
# CLI and report
# --------------------------------------------------------------------------- #
def test_cli_defaults_match_the_spec() -> None:
    args = mod.parse_args(["spreads.csv"])
    assert (args.threshold, args.min_scans, args.max_gap, args.stale_scans) == (30, 3, 300, 5)
    assert args.out is None and args.runs_csv is None


def test_report_has_every_section(tmp_path: Path) -> None:
    path = write(tmp_path, frozen_rows(6))
    out = tmp_path / "report.md"
    runs_csv = tmp_path / "runs.csv"

    assert mod.main([str(path), "--out", str(out), "--runs-csv", str(runs_csv)]) == 0
    report = out.read_text(encoding="utf-8")

    for heading in (
        "# Spread Persistence Report",
        "## Per-pair summary",
        "## Persistent runs",
        "## Outlier venues",
        "## Frozen quotes",
        "## Findings",
    ):
        assert heading in report
    assert runs_csv.exists()


def test_duration_formatting() -> None:
    assert mod.format_duration(65) == "1m 05s"
    assert mod.format_duration(3900) == "1h 05m"
    assert mod.format_duration(45) == "45s"
