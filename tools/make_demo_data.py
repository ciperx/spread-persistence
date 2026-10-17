#!/usr/bin/env python3
"""Generate a synthetic scanner log for demos and benchmarks.

The output mimics what the Solana price scanner writes: one row per venue per
scan, with a best route plus six venue quotes. Two behaviours are injected on
purpose so the report has something to find:

* SOL/USDC carries a genuinely persistent 45 bps spread for eight scans.
* Phoenix freezes on RAY/USDC for six scans while the market keeps moving.

Usage
-----
    python tools/make_demo_data.py --scans 400 --out demo/spreads_demo.csv
    python tools/make_demo_data.py --scans 71400 --out /tmp/big.csv   # ~500k rows
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAIRS = ("SOL/USDC", "BONK/SOL", "RAY/USDC")
VENUES = ("Raydium", "Raydium CLMM", "Whirlpool", "Meteora DLMM", "Phoenix", "Orca")
BASE = datetime(2026, 10, 17, 16, 0, 0, tzinfo=timezone.utc)

HEADER = [
    "timestamp", "slot", "pair", "amount", "venue", "out_amount", "price",
    "vs_best_bps", "impact_pct", "route", "cross_venue_spread_bps",
    "round_trip_bps", "stale",
]


def build(scans: int, seed: int = 7) -> list[list[str]]:
    rng = random.Random(seed)
    rows: list[list[str]] = []

    anchors = {"SOL/USDC": 103.4, "BONK/SOL": 2.86e-8, "RAY/USDC": 1.95}
    # Phoenix freezes on the value it had when the window opened; it must stay
    # byte-identical afterwards or it is a moving quote, not a stale one.
    frozen_price: float | None = None
    amounts = {"SOL/USDC": "1", "BONK/SOL": "10000000", "RAY/USDC": "100"}

    for index in range(scans):
        stamp = BASE + timedelta(seconds=index * 60 + rng.randint(-2, 2))
        slot = 458096091 + index * 37
        iso = stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        for pair in PAIRS:
            # Random walk with mean reversion keeps prices plausible.
            drift = rng.gauss(0, 0.0006)
            anchors[pair] *= 1 + drift
            mid = anchors[pair]

            persistent = pair == "SOL/USDC" and 120 <= index < 128
            frozen = pair == "RAY/USDC" and 200 <= index < 206
            if pair == "RAY/USDC" and index == 200:
                frozen_price = round(anchors["RAY/USDC"] * 0.86, 10)
            width = 0.0045 if persistent else abs(rng.gauss(0.00015, 0.00012))

            quotes = {}
            for venue in VENUES:
                if frozen and venue == "Phoenix":
                    quotes[venue] = frozen_price
                    continue
                offset = rng.uniform(-width, width)
                quotes[venue] = round(mid * (1 + offset), 12)

            best_venue = max(quotes, key=quotes.get)
            best_price = quotes[best_venue]
            round_trip = round(rng.gauss(1.2, 3.4), 2)
            spread = (max(quotes.values()) / min(quotes.values()) - 1) * 10_000

            rows.append(
                [
                    iso, str(slot), pair, amounts[pair], "Jupiter (best)",
                    repr(best_price), repr(best_price), "", "0", best_venue,
                    f"{spread:.2f}", f"{round_trip:.2f}", "",
                ]
            )

            for venue, price in quotes.items():
                vs_best = (price / best_price - 1) * 10_000
                # the scanner only flags a quote stale once it has seen enough
                # repeats: the 5th and 6th identical quotes of the stretch
                stale = "true" if (frozen and venue == "Phoenix" and index >= 204) else "false"
                rows.append(
                    [
                        iso, str(slot), pair, amounts[pair], venue,
                        repr(price), repr(price), f"{vs_best:.2f}",
                        f"{abs(rng.gauss(0.2, 0.15)):.4f}", venue,
                        f"{spread:.2f}", f"{round_trip:.2f}", stale,
                    ]
                )

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scans", type=int, default=400)
    parser.add_argument("--out", type=Path, default=Path("demo/spreads_demo.csv"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rows = build(args.scans, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)

    print(f"wrote {len(rows):,} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
