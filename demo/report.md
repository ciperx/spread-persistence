# Spread Persistence Report

- Input file: `demo/spreads_demo.csv`
- Time range: 2026-10-17 16:00:00 → 2026-10-17 22:39:02 UTC
- Scans analysed: 1200 · rows loaded: 8400 · rows skipped: 0
- Parameters: threshold 30 bps · min-scans 3 · max-gap 300s · stale-scans 5

## Per-pair summary

| Pair | Scans | Covered | ≥ threshold | Median | p95 | Max | Persistent runs | Longest run | Median round trip | Positive round trip | Frozen excluded |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BONK/SOL | 400 | 6h 39m | 0.00% | 2.10 | 5.25 | 8.05 | 0 | — | 0.78 | 60.00% | 0 |
| RAY/USDC | 400 | 6h 39m | 0.00% | 2.11 | 5.41 | 8.14 | 0 | — | 0.77 | 59.25% | 6 |
| SOL/USDC | 400 | 6h 39m | 2.00% | 2.02 | 5.42 | 75.17 | 1 | 8 scans / 7m 02s | 1.10 | 62.50% | 0 |

## Persistent runs

| Pair | Start | End | Duration | Scans | Mean spread | Max spread | Dominant outlier | Mean round trip |
|---|---|---|---|---|---|---|---|---|
| SOL/USDC | 18:00:00 | 18:07:02 | 7m 02s | 8 | 67.53 | 75.17 | Raydium CLMM | 1.45 |

## Outlier venues

| Pair | Venue | Anomalous scans as outlier | Share |
|---|---|---|---|
| SOL/USDC | Raydium CLMM | 3 | 37.50% |
| SOL/USDC | Orca | 2 | 25.00% |
| SOL/USDC | Whirlpool | 2 | 25.00% |
| SOL/USDC | Meteora DLMM | 1 | 12.50% |

## Frozen quotes

| Pair | Venue | Start | End | Duration | Frozen out_amount | Distance from best |
|---|---|---|---|---|---|---|
| RAY/USDC | Phoenix | 19:20:02 | 19:25:02 | 5m 00s | 1.68 | 1632.08 bps |

Disagreements with the `stale` column: **0**

## Findings

- SOL/USDC: spread ≥ 30 bps in 2.00% of 400 scans (median 2.02 bps, max 75.17 bps).
- The longest persistent run is SOL/USDC, 7m 02s across 8 scans, dominated by Raydium CLMM (mean spread 67.53 bps).
- 1 frozen quote period(s) were excluded; the worst was Phoenix on RAY/USDC, pinned at 1.68 for 5m 00s, a fake 1632.08 bps away from the best route.
- Round trip was positive in 60.58% of scans on average, which is the share of the time a round trip would have cleared the spread.
