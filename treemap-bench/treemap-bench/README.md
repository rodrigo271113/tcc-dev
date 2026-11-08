# treemap-bench

Automated performance comparison of two "Pony Factor" treemap implementations:

| id | implementation | repo | stack |
|----|----------------|------|-------|
| `plotly` | `rep_pack_march6/server` | Flask + pandas, template inlines the data | **Plotly** `treemap` (`Plotly.newPlot`, `maxdepth: -1`) |
| `duks` | `DUKS/server` | Flask + polars, cached JSON API | **D3** custom `d3.treemap`, `MAX_DEPTH = 3`, progressive render |

Both read the **same per-project CSVs**, so the comparison is apples-to-apples on input.

## What it measures

**Scalability** — every metric is collected across a geometric ladder of repo
sizes (`socket.io` ~1k nodes → `Firefox` ~420k) and plotted vs node count;
`analysis` also fits a log-log scaling slope.

**Initial load latency** (`load` scenario) — decomposed:
- `ttfb_ms`, `total_transfer_bytes`, `request_count` — network / payload
- `fcp_ms`, `lcp_ms` — paint milestones (PerformanceObserver)
- **`treemap_ready_ms`** — navigation start → the treemap stops mutating (primary)
- `tbt_ms`, `longtask_max_ms` — main-thread blocking during load
- `js_heap_bytes` — heap after settle

**Zoom in / out latency on tile select**:
- `zoom_in` — drill into the largest directory `drill_depth` levels (real mouse clicks)
- `zoom_out` — climb back to the root (breadcrumb / Plotly pathbar)
- `zoom_toggle` — zoom into one top-level dir and back, ×`toggle_reps` (steady state)
- per interaction: `settle_ms` (click → quiet), `tbt_ms`, `min_fps`, `jank_frames`, `event_max_dur_ms` (INP-like)

**Server-side latency** (separate HTTP phase, no browser) — isolates payload
generation: Plotly's `pandas.apply` (no cache) vs DUKS' polars + LRU cache,
measured cold (after container restart) and warm.

The same canonical drill path (largest child directory, N levels) is used for
both apps — node identifiers are identical across the two trees.

## Requirements

- Docker (daemon running) — the harness runs `docker compose up` for both servers
- Python 3.11+ 
- Internet on first run (Chromium download; the pages pull `plotly.js` / `d3.js` from CDN)
- The `rep_pack_march6` and `DUKS` checkouts as siblings of this folder
  (override in `config.yaml → paths`)
- DUKS treemap CSVs present in `DUKS/data/knowledge-map/` and
  `rep_pack_march6/data/` (they already are in this workspace)

## Run

```bash
./setup.sh
source .venv/bin/activate

python -m harness.run_bench --smoke      # ~3 min sanity check (2 repos, 1 trial)
python -m harness.run_bench              # full matrix (hours — see note)
python -m analysis.analyze               # summary CSVs + figures in results/
jupyter notebook analysis/analyze.ipynb  # interactive
```

Useful flags: `--apps duks`, `--repos pytorch node`, `--scenarios load zoom_in`,
`--trials 10`, `--headed`, `--no-server`, `--keep-up`, `--fresh`.

### Crash safety / resuming

The run is built to be killed and restarted without losing data:

- Each **cell** (one scenario × one trial) is written to `results/trials.jsonl`
  in a **single atomic append** — all its rows plus a trailing `scenario_done`
  marker, one `write()` + `fsync()`. A crash, power loss, or `kill -9` leaves
  the file containing only *whole* cells, never a half-measured one.
- The run **auto-resumes**: on every start, cells that already have a
  `scenario_done` marker are skipped. Just re-run the exact same command.
- **Ctrl-C** (SIGINT/SIGTERM) finishes the current cell, commits it, and exits
  cleanly. A **second Ctrl-C** forces an immediate stop — the in-flight cell is
  discarded, everything already on disk is intact.
- If a **server dies** mid-run, the failing cell is logged as `cell_error` and
  the harness restarts that server before continuing.
- The **server phase** and the **path pre-compute** resume the same way.
- `--fresh` archives the existing `results/*.jsonl` + `shots/` to timestamped
  names and starts clean.

Safe pattern for a long run: start it; if anything fails or you Ctrl-C, run the
identical command again.

### Runtime

Full config = 2 apps × 10 repos × 4 scenarios × (15+3) trials ≈ 8–9 h through
`chromium`, ~12–14 h including `firefox` on Plotly (which mostly times out — a
result in itself). Large repos on the Plotly side take tens of seconds each (it
inlines every node into the HTML and runs `DataFrame.apply` row-wise with no
cache). Start narrow (`--repos socketio numpy threejs icu pytorch --trials 10`),
then widen — the resume behaviour above makes widening free.

## Output

```
results/
  trials.jsonl   one row per measured load / interaction (+ scenario_done markers)
  server.jsonl   server-side HTTP samples
  paths.json     canonical drill path per repo
  shots/         load screenshots (QA: confirms the treemap actually rendered)
  traces/        chrome perf traces (only if save_trace: true)
  summary_*.csv  written by analysis/analyze.py
  figs/*.png     latency-vs-size, per-repo boxplots, transfer size, server latency
```

## Input integrity (orphan parent rows)

Both implementations rebuild the directory hierarchy from the `identifier`
column alone -- a node's parent is its identifier minus the trailing
`/node_name`. Nothing in the format guarantees the parent row exists, and in
two of the sampled repositories it does not: a directory is referenced as a
parent by its children but has no row of its own.

| CSV | nodes | orphan parents | rows affected |
|---|---|---|---|
| `socket.io` ... `hhvm` | 964 ... 83,071 | 0 | 0 |
| `Chromium` | 146,201 | 1 | 2 |
| `Firefox` | 419,572 | 2 | 2 |

The two implementations disagree about what that means. DUKS attaches an
orphan to the root and renders the rest of the tree. Plotly's treemap trace
fails to build the hierarchy, reports it as a `console.warn`, and draws
nothing -- the page stays blank, with no error visible to the user and no
JavaScript exception. Two malformed rows out of 146,201 decide whether a
repository renders at all.

Check the ladder before a run:

```bash
python -m harness.csv_integrity --check ../rep_pack_march6/data/*.csv
```

Exit status is 1 when any orphan is found, so it can gate a run.

That failure is a result worth reporting, but it also means Plotly cannot be
*measured* on those two repositories, which is a separate question from
whether it can render them. To reach the top of the ladder, synthesize the
missing directory rows:

```bash
python -m harness.csv_integrity --repair ../rep_pack_march6/data/Chromium.csv \
    --out ../rep_pack_march6/data/ChromiumRepaired.csv
python -m harness.csv_integrity --repair ../rep_pack_march6/data/Firefox.csv \
    --out ../rep_pack_march6/data/FirefoxRepaired.csv
```

A synthesized row takes its size from the sum of its children and its Pony
Factor from the lowest among them. **Both values are fabricated**, and every
synthesized row is printed, so runs that use a repaired file must disclose it.
In practice the affected nodes are empty directories of zero-length files, so
the effect on the rendered picture is nil -- but it is still fabricated input.
The repaired CSVs are large (24 MB and 81 MB) and are not committed; the
script regenerates them.

With the repaired input, Plotly renders both: 146,202 tiles for Chromium and
419,574 for Firefox. There is no node-count ceiling -- see the measurements in
`## What it measures`.

## Method notes / knobs

- **Settle detection** is one shared definition for both apps: after a marked
  interaction, resolve once there have been no DOM mutations, long tasks, or
  (Plotly) `plotly_afterplot` events for `settle_quiet_ms_*`. Fairest
  cross-framework "done" signal; `lcp_ms` is the robust cross-check for load.
- **DUKS repo parity**: DUKS' page has no `?repo=` param, so the harness injects
  a `fetch` shim that points the first `/api/knowledge-map/tree` call at the
  target repo — a genuine single cold load, comparable to Plotly's `/any/<repo>`.
  No files in either repo are modified.
- **Depth normalization**: set `depth_normalize: 3` to force Plotly's `maxdepth`
  to 3 (matching DUKS' `MAX_DEPTH`) for a controlled client comparison. Default
  is as-shipped (`-1` vs `3`).
- **Dev servers, uncompressed**: both run their Flask dev server without gzip, so
  `total_transfer_bytes` compares raw payloads. Production (nginx gzip for DUKS)
  would narrow the transfer gap — measure separately by pointing `base_url` at a
  prod compose if needed.
- **Throttling**: add profiles under `throttle_profiles` (CPU rate via CDP
  `Emulation.setCPUThrottlingRate`, network via `Network.emulateNetworkConditions`)
  to emulate low-end devices / slow links.
- Report **median + IQR + p95**, not mean — the distributions are right-skewed.
  `--headed` (or `headless: false`) is more representative for the final run.
- Failures (crash / OOM / >timeout) are recorded as rows (`load_error`,
  `settle_timed_out`, `cell_error`), not dropped — "does not scale past X" is a result.
