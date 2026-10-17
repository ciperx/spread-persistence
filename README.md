# spread-persistence

Find the arbitrage spreads that **last** — and throw out the ones that only exist
because a venue stopped updating its quote.

A Solana price scanner logs every venue quote it sees on every scan into
`spreads.csv`. A single scan showing a 40 bps gap means nothing: one stale
pool can invent a 1,500 bps "opportunity" out of thin air. This tool removes
frozen quotes, recomputes the spread from live venues only, and reports which
spreads stayed open long enough to matter.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# demo data ships with the repo, so this works immediately
python analyze_spreads.py demo/spreads_demo.csv --out demo/report.md
```

Output: a Markdown report on stdout (or `--out`), plus an optional CSV of the
persistent runs. [`demo/report.md`](demo/report.md) is the report for
[`demo/spreads_demo.csv`](demo/spreads_demo.csv).

```
# Spread Persistence Report
- Input file: `demo/spreads_demo.csv`
- Time range: 2026-10-17 16:00:00 → 2026-10-17 22:39:02 UTC
- Scans analysed: 1200 · rows loaded: 8400 · rows skipped: 0
- Parameters: threshold 30 bps · min-scans 3 · max-gap 300s · stale-scans 5
```

## Usage

```
python analyze_spreads.py CSV [options]

  --threshold   30     spread in bps that counts as anomalous
  --min-scans   3      consecutive anomalous scans needed for a persistent run
  --max-gap     300    seconds between scans that breaks a run
  --stale-scans 5      identical quotes that mark a venue frozen
  --out         PATH   write the Markdown report here instead of stdout
  --runs-csv    PATH   also export the persistent runs table as CSV
```

Example with a tighter definition of "persistent":

```bash
python analyze_spreads.py spreads.csv \
  --threshold 25 --min-scans 5 --max-gap 120 \
  --out report.md --runs-csv runs.csv
```

## Input format

One row per venue quote per scan. Both the 12-column format and the 13-column
format with a trailing `stale` column are accepted — the flag is **never**
trusted, it is only compared against what the detector works out on its own.

| column | meaning |
|---|---|
| `timestamp` | ISO-8601 UTC scan time, e.g. `2026-10-17T16:03:44.476Z` |
| `slot` | Solana slot of the scan |
| `pair` | trading pair, e.g. `SOL/USDC` |
| `amount` | input amount used for the quote |
| `venue` | pool / router name; `Jupiter (best)` is the reference route |
| `out_amount` | output amount for `amount` |
| `price` | `out_amount / amount` |
| `vs_best_bps` | distance from the best route in bps (blank on the best row) |
| `impact_pct` | price impact |
| `route` | route label — **may contain commas**, so it is quoted |
| `cross_venue_spread_bps` | the scanner's own spread estimate |
| `round_trip_bps` | round-trip result for the scan |
| `stale` | optional flag written by the scanner |

Damaged input never aborts a run: rows with the wrong field count, an
unparseable timestamp, or a non-numeric `out_amount`/`price` are dropped and
counted in the header as `rows skipped`.

## How it works

### 1. Frozen quotes

A venue quote is a **repeat** when its `out_amount` equals that venue's previous
quote for the same pair **and** the `Jupiter (best)` amount moved in between —
a real quote cannot stay bit-identical while the market moves around it.

* a changed quote resets that venue's counter to zero;
* a flat market (nothing moved, including the best route) leaves the counter
  alone, so a quiet venue is never punished for being quiet;
* the quote that reaches `--stale-scans − 1` repeats (4 by default, i.e. the 5th
  identical scan) is frozen, and every repeat after it stays frozen — a quote is
  judged on the evidence available at its own timestamp, so earlier copies of
  the same number are **not** reclassified retroactively. The first four scans
  of a stale stretch therefore still show the spread: five identical quotes are
  needed before it can be called frozen.

### 2. One row per scan

All venue rows of a `(pair, timestamp)` collapse into one scan. **Every scan in
the input keeps its row**, including scans whose venues are all frozen: dropping
them would understate the scan counts and let an anomalous run bridge across the
hole. The spread is recomputed from non-frozen single venues only:

```
spread_bps = (max_price / min_price - 1) * 10_000
```

With fewer than two live venues the spread is *undefined*, not zero — an
undefined spread is never anomalous, because "we could not measure it" is not
the same as "there was no spread".

The **outlier venue** for a scan is the live single venue (never the best
route) with the lowest `vs_best_bps`; that is the venue doing the dragging.

### 3. Persistent runs

Per pair, in time order: a scan is anomalous when `spread_bps >= threshold`.
A run is a maximal stretch of consecutive anomalous scans with no gap larger
than `--max-gap`; it is **persistent** when it holds at least `--min-scans`
scans. Anything shorter is noise and never reaches the report's headline table.

### 4. Report

Six sections: per-pair summary, persistent runs, outlier venues, frozen quotes,
the `stale`-flag disagreement count, and two to four findings computed from the
data — no hard-coded prose.

## Tests

```bash
pip install pytest
python -m pytest tests -q      # 22 passed
```

The suite builds every fixture in-test and covers the cases that decide whether
the numbers can be trusted:

* a run of exactly `min_scans` counts; one scan short does not;
* a gap over `max_gap` splits a run, and so does a single scan below threshold;
* a spread of exactly 30 bps counts as anomalous (float-tolerant comparison);
* pairs are analysed independently in one file;
* malformed rows are skipped and counted, not fatal;
* routes containing commas survive parsing;
* empty `round_trip_bps` is tolerated;
* the 5th identical quote is frozen and the first four are not, identical quotes
  in a flat market are never frozen, and a scan whose venues are all frozen
  keeps its row and splits the run;
* a frozen venue's fake 1,500 bps spread disappears from the scans after the
  freeze point;
* the 12-column format loads without a `stale` column;
* a `stale` flag that disagrees with the detector is counted, not trusted.

## Performance

`pandas`-based and vectorised apart from one pass per `(pair, venue)` group for
the repeat counter. A 499,800-row file (23,800 scans × 3 pairs × 7 rows) runs
end to end in **~7 s** on 2 vCPU, well inside the 10 s budget for 500k rows.

## Demo data

`demo/spreads_demo.csv` is generated by `tools/make_demo_data.py` and contains
two injected behaviours so the report has something real to say:

* `SOL/USDC` holds a genuine ~45–75 bps spread for eight consecutive scans;
* `Phoenix` freezes on `RAY/USDC` for six scans at a quote 1,632 bps away from
  the best route — the fake spread that the frozen filter is there to kill.

```bash
python tools/make_demo_data.py --scans 23800 --out /tmp/big.csv   # ~500k rows
```

## Limitations

* Frozen detection assumes the scanner quotes a fixed amount per pair; a
  changing `amount` would make `out_amount` incomparable.
* The best route is the reference clock. If `Jupiter (best)` itself freezes,
  nothing is flagged — by design, since there is then no evidence of movement.
* A venue that is genuinely offline looks identical to a frozen one; both are
  excluded, which is the right call for spread measurement.
