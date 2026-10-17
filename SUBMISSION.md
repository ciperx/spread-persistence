# Persistent Arbitrage Spread Analysis — Submission

**Public repository:** https://github.com/ciperx/spread-persistence

Everything below is implemented, tested and runnable offline. The repo is public
and contains the full source, the pytest suite, the demo fixture and the
generated report.

---

## What was built

`analyze_spreads.py` — a single-file CLI that takes a Solana price-scanner
`spreads.csv` and answers one question: **which spreads actually persist once
frozen venue quotes are removed?**

```
python analyze_spreads.py spreads.csv \
  --threshold 30 --min-scans 3 --max-gap 300 --stale-scans 5 \
  --out report.md --runs-csv runs.csv
```

The four defaults are exactly the documented ones (30 bps, 3 scans, 300 s,
5 identical quotes). With no flags at all, `python analyze_spreads.py spreads.csv`
prints the Markdown report to stdout.

Files in the repository:

| file | purpose |
|---|---|
| `analyze_spreads.py` | the analyser (742 lines, typed, documented) |
| `tests/test_analyze_spreads.py` | 21 pytest cases, all fixtures built in-test |
| `tools/make_demo_data.py` | deterministic generator for the demo fixture |
| `demo/spreads_demo.csv` | 8,400-row fixture with two injected behaviours |
| `demo/report.md` | the report produced from that fixture |
| `README.md` | install, usage, method, limitations |
| `requirements.txt` | `pandas>=2.0` |

---

## 1. Parsing (`quality`, weight 0.30)

* Accepts both the **12-column** layout and the **13-column** layout with a
  trailing `stale` column. The flag is never trusted — it is only compared
  against the detector's own verdict.
* Reads everything as text first, then coerces: timestamps with
  `pd.to_datetime(..., utc=True)`, numerics with `pd.to_numeric`. That means
  **scientific notation is handled natively** — `BONK/SOL` prices such as
  `2.86e-08` and `2.86421177e-8` parse without special-casing.
* Damaged rows never abort the run. Wrong field count, an unparseable
  timestamp, an empty pair/venue, or a non-numeric `out_amount`/`price` are
  dropped and counted; the header reports `rows skipped`.
* The run fails **only** when a required column is entirely absent, with a
  clear message and exit code 2:

  ```
  error: missing required column(s): price, round_trip_bps (found: timestamp, pair, venue)
  ```
* `route` is quoted in the CSV and routinely contains commas
  (`"Raydium CLMM → Raydium CLMM → Raydium CLMM"`); parsing is delegated to
  pandas' CSV reader, so those rows survive intact (covered by a test).

## 2. Frozen-quote detection (weight 0.15)

A quote is a **repeat** when its `out_amount` is byte-identical to that venue's
previous quote for the same pair **and** the `Jupiter (best)` amount changed
between the two scans. A real quote cannot stay identical while the market
moves around it.

* a **changed** quote resets the venue's counter to zero;
* a **flat market** (neither the venue nor the best route moved) leaves the
  counter untouched, so a quiet venue is never punished for being quiet;
* at `--stale-scans − 1` repeats (4 by default, i.e. the 5th identical scan) the
  venue is frozen — and the **whole identical stretch** is marked frozen, since
  the earlier copies are exactly as fake as the last one;
* the logged `stale` column is **ignored for detection** and used only for the
  disagreement count in the report.

## 3. Per-scan aggregation and persistent runs (weight 0.25)

All venue rows of one `(pair, timestamp)` collapse into a single scan. The
spread is recomputed from **non-frozen single venues only**:

```
spread_bps = (max_price / min_price − 1) × 10 000
```

With fewer than two live venues the spread is *undefined*, not zero — an
undefined spread is never anomalous, because "we could not measure it" is not
"there was no spread".

The **outlier venue** of a scan is the live single venue (never the best route)
with the lowest `vs_best_bps`.

Per pair, in time order: a scan is anomalous when `spread_bps >= threshold`.
A run is a maximal stretch of consecutive anomalous scans with no gap greater
than `--max-gap`; it is **persistent** when it holds at least `--min-scans`
scans. `round_trip_bps` is taken from the scan and averaged across the run.

## 4. Report (weight 0.15)

Six sections, all computed from the input: header parameters, per-pair summary,
persistent runs, outlier venues, frozen quotes, and two to four findings. Every
numeric value is rendered to **two decimals**; durations use the exact
`1h 05m` / `7m 02s` / `45s` format. See `demo/report.md`.

## 5. Tests and code quality (weight 0.15)

```
$ python -m pytest tests -q
.....................
21 passed in 2.42s
```

No network access, no external fixtures — every CSV is generated inside the
test. Coverage includes: exact threshold boundary (30 bps counts), run of
exactly `min_scans` vs one short, gap splitting a run, a single sub-threshold
scan splitting a run, per-pair independence, malformed rows counted not fatal,
comma-bearing routes, empty `round_trip_bps`, single-venue scans, all-frozen
input, 5-vs-4 identical quotes, flat-market non-detection, the frozen venue's
fake 1,500 bps spread disappearing after exclusion, the 12-column format, and
`stale`-flag disagreement counting.

## 6. Must-pass criteria

* **pandas** — every transformation goes through pandas (`read_csv`,
  `groupby`, `merge`, `to_numeric`, `to_datetime`); `requirements.txt` pins
  `pandas>=2.0`.
* **Offline** — no `requests`, `urllib`, `socket`, or subprocess calls anywhere
  in the analyser.
* **pytest** — the suite above ships in `tests/`.
* **No hardcoded results** — every table row, percentage, duration and finding
  is derived from the parsed frame; the only literals are parameter defaults
  and column names.

## 7. Performance

499,800 rows (23,800 scans × 3 pairs × 7 rows) analysed end to end in **7.2 s**
on 2 vCPU, including CSV parse, frozen detection, scan collapse, run detection
and report rendering — inside the 10 s budget.

```
$ python tools/make_demo_data.py --scans 23800 --out /tmp/big.csv
wrote 499,800 rows to /tmp/big.csv
$ time python analyze_spreads.py /tmp/big.csv --out /tmp/big_report.md
WALL 7.23 s
```
