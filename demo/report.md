# Spread Persistence Report

- Input file: `demo/spreads_demo.csv`
- Time range: 2026-10-17 16:00:00 → 2026-10-17 22:39:02 UTC
- Scans analysed: 1200 · rows loaded: 8400 · rows skipped: 0
- Parameters: threshold 30 bps · min-scans 3 · max-gap 300s · stale-scans 5

## Per-pair summary

| Pair | Scans | Covered | ≥ threshold | Median | p95 | Max | Persistent runs | Longest run | Median round trip | Positive round trip | Frozen excluded |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BONK/SOL | 400 | 6h 39m | 0.00% | 2.10 | 5.25 | 8.05 | 0 | — | 0.78 | 60.00% | 0 |
| RAY/USDC | 400 | 6h 39m | 1.00% | 2.14 | 5.55 | 1637.74 | 1 | 4 scans / 3m 00s | 0.77 | 59.25% | 2 |
| SOL/USDC | 400 | 6h 39m | 2.00% | 2.02 | 5.42 | 75.17 | 1 | 8 scans / 7m 02s | 1.10 | 62.50% | 0 |

## Persistent runs

| Pair | Start | End | Duration | Scans | Mean spread | Max spread | Dominant outlier | Mean round trip |
|---|---|---|---|---|---|---|---|---|
| SOL/USDC | 2026-10-17 18:00:00 | 2026-10-17 18:07:02 | 7m 02s | 8 | 67.53 | 75.17 | Raydium CLMM | 1.45 |
| RAY/USDC | 2026-10-17 19:20:02 | 2026-10-17 19:23:02 | 3m 00s | 4 | 1633.52 | 1637.74 | Phoenix | 0.10 |

## Outlier venues

| Pair | Venue | Anomalous scans as outlier | Share |
|---|---|---|---|
| RAY/USDC | Phoenix | 4 | 100.00% |
| SOL/USDC | Raydium CLMM | 3 | 37.50% |
| SOL/USDC | Orca | 2 | 25.00% |
| SOL/USDC | Whirlpool | 2 | 25.00% |
| SOL/USDC | Meteora DLMM | 1 | 12.50% |

## Frozen quotes

| Pair | Venue | Start | End | Duration | Frozen out_amount | Distance from best |
|---|---|---|---|---|---|---|
| RAY/USDC | Phoenix | 2026-10-17 19:23:59 | 2026-10-17 19:25:02 | 1m 03s | 1.68 | 1629.20 bps |

Disagreements with the `stale` column: **4**

## Findings

- SOL/USDC: spread ≥ 30 bps in 2.00% of 400 scans (median 2.02 bps, max 75.17 bps).
- RAY/USDC: spread ≥ 30 bps in 1.00% of 400 scans (median 2.14 bps, max 1637.74 bps).
- The longest persistent run is SOL/USDC, 7m 02s across 8 scans, dominated by Raydium CLMM (mean spread 67.53 bps).
- 1 frozen quote period(s) were excluded; the worst was Phoenix on RAY/USDC, pinned at 1.68 for 1m 03s, a fake 1629.20 bps away from the best route.
