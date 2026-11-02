"""Orchestrator: bring servers up, measure server + browser latency across the
(app x repo x scenario x throttle x trial) matrix, write results/*.jsonl.

Crash safety
------------
* Every completed cell is written to results/trials.jsonl in ONE atomic
  append (all its rows + a trailing `scenario_done` marker, one write + fsync).
  A crash / power loss / kill therefore leaves the file with only *whole*
  cells -- never a half-measured one.
* The run always auto-resumes: cells whose `scenario_done` marker is already
  present are skipped. Just re-run the same command after any failure.
* Ctrl-C (SIGINT/SIGTERM) finishes the current cell, writes it, and exits
  cleanly. A second Ctrl-C forces an immediate stop (current cell discarded,
  nothing already on disk is lost).
* If a server dies mid-run, the failing cell is logged and the harness
  restarts the server before continuing.

Usage:
    python -m harness.run_bench                 # full run (auto-resumes)
    python -m harness.run_bench --smoke         # tiny validation run
    python -m harness.run_bench --fresh         # archive old results, start over
    python -m harness.run_bench --apps duks --repos pytorch --scenarios load
    python -m harness.run_bench --no-server     # skip the HTTP server phase
"""
from __future__ import annotations

import argparse
import random
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from .browser import (TraceRecorder, apply_throttle, instrument_for, open_treemap,
                      zoom_in, zoom_out, zoom_toggle, LAUNCH_ARGS)
from .config import REPO_ROOT, load_config
from .server_timing import run_server_phase
from .servers import build_servers
from .tree_paths import compute_paths
from .util import Stop as _Stop
from .util import append_jsonl, append_jsonl_many, read_jsonl, utcnow

RESULTS = REPO_ROOT / "results"
TRIALS_PATH = RESULTS / "trials.jsonl"
SERVER_PATH = RESULTS / "server.jsonl"
PATHS_PATH = RESULTS / "paths.json"
SHOTS = RESULTS / "shots"
TRACES = RESULTS / "traces"

RUN_ID = datetime.now().strftime("%Y%m%dT%H%M%S")


def _install_signal_handlers() -> None:
    def handler(signum, frame):
        if _Stop.requested:
            print("\n[stop] second signal -- forcing exit "
                  "(current cell discarded; results on disk are intact)", flush=True)
            raise KeyboardInterrupt
        _Stop.requested = True
        print("\n[stop] will finish the current cell and exit cleanly "
              "(Ctrl-C again to force). Re-run the same command to resume.",
              flush=True)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--smoke", action="store_true", help="tiny run to validate the harness")
    ap.add_argument("--apps", nargs="+", choices=["plotly", "duks"])
    ap.add_argument("--repos", nargs="+")
    ap.add_argument("--scenarios", nargs="+",
                    choices=["load", "zoom_in", "zoom_out", "zoom_toggle"])
    ap.add_argument("--trials", type=int)
    ap.add_argument("--throttle", nargs="+", help="throttle profile names to run")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="archive existing results/*.jsonl + shots, then start over")
    ap.add_argument("--resume", action="store_true",
                    help="(default behaviour; kept for clarity) skip finished cells")
    ap.add_argument("--keep-up", action="store_true", help="don't tear servers down")
    ap.add_argument("--headed", action="store_true")
    return ap.parse_args(argv)


def filter_cfg(cfg: dict, args) -> dict:
    if args.smoke:
        cfg["repos"] = [r for r in cfg["repos"] if r["name"] in ("socketio", "pytorch")]
        cfg["trials"] = 1
        cfg["warmup_trials"] = 0
        cfg["server_timing"]["warm_samples"] = 3
        cfg["server_timing"]["duks_cold_restarts"] = 1
        cfg["server_timing"]["plotly_samples"] = 3
    if args.apps:
        cfg["apps"] = {k: v for k, v in cfg["apps"].items() if k in args.apps}
    if args.repos:
        cfg["repos"] = [r for r in cfg["repos"] if r["name"] in args.repos]
    if args.scenarios:
        cfg["scenarios"] = args.scenarios
    if args.trials is not None:
        cfg["trials"] = args.trials
    if args.throttle:
        cfg["throttle_profiles"] = [p for p in cfg["throttle_profiles"]
                                    if p["name"] in args.throttle]
    if args.headed:
        cfg["headless"] = False
    return cfg


def archive_results() -> None:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    moved = []
    for p in (TRIALS_PATH, SERVER_PATH, PATHS_PATH):
        if p.exists():
            dest = p.with_name(f"{p.stem}.{stamp}{p.suffix}")
            p.rename(dest)
            moved.append(dest.name)
    if SHOTS.exists() and any(SHOTS.iterdir()):
        dest = SHOTS.with_name(f"shots.{stamp}")
        SHOTS.rename(dest)
        moved.append(dest.name)
    print(f"[fresh] archived: {', '.join(moved) or 'nothing to archive'}", flush=True)


def done_cells(path: Path) -> set:
    cells = set()
    for row in read_jsonl(path):
        if row.get("step_kind") == "scenario_done":
            cells.add((row["app"], row["repo"], row["throttle"],
                       row["scenario"], row["trial"]))
    return cells


def _recover_servers(servers: dict) -> None:
    """A cell just failed -- if a server is unreachable, restart it."""
    for name, s in servers.items():
        try:
            s.wait_healthy(timeout=8)
        except Exception:
            print(f"[recover] {name} looks down; restarting", flush=True)
            try:
                s.restart(timeout=180)
            except Exception as e:
                print(f"[recover] {name} restart failed: {e!r}", flush=True)


# ---------------------------------------------------------------------------

def run_browser_phase(cfg: dict, servers: dict, paths: dict, args) -> bool:
    """Returns True if it ran to completion, False if interrupted."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    done = done_cells(TRIALS_PATH)
    if done:
        print(f"[resume] {len(done)} cells already recorded -- skipping them", flush=True)

    tasks = []
    for prof in cfg["throttle_profiles"]:
        for trial in range(cfg["trials"] + cfg["warmup_trials"]):
            is_warmup = trial < cfg["warmup_trials"]
            app_order = list(cfg["apps"])
            if cfg.get("interleave") and trial % 2:
                app_order = app_order[::-1]
            for app_name in app_order:
                for repo in cfg["repos"]:
                    for scen in cfg["scenarios"]:
                        tasks.append((prof, trial, is_warmup, app_name, repo, scen))

    total = len(tasks)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg["headless"], args=LAUNCH_ARGS)
        chrome_version = browser.version
        print(f"[browser] chromium {chrome_version}, headless={cfg['headless']}, "
              f"{total} cells", flush=True)

        for idx, (prof, trial, is_warmup, app_name, repo, scen) in enumerate(tasks, 1):
            if _Stop.requested:
                print(f"[stop] exiting before cell {idx}/{total}", flush=True)
                browser.close()
                return False
            tnum = trial - cfg["warmup_trials"]
            cell = (app_name, repo["name"], prof["name"], scen, tnum)
            if not is_warmup and cell in done:
                continue
            try:
                _run_cell(browser, cfg, servers, paths, prof, trial, is_warmup,
                          app_name, repo, scen, chrome_version, f"{idx}/{total}")
            except KeyboardInterrupt:
                print("[stop] interrupted mid-cell -- that cell was NOT written; "
                      "re-run to resume", flush=True)
                browser.close()
                return False
            except Exception:
                print(f"[cell FAIL] {cell}\n{traceback.format_exc()}", flush=True)
                append_jsonl(TRIALS_PATH, {
                    "ts": utcnow(), "run_id": RUN_ID, "app": app_name,
                    "repo": repo["name"], "repo_nodes": repo["nodes"],
                    "throttle": prof["name"], "scenario": scen, "trial": tnum,
                    "warmup": is_warmup, "step_kind": "cell_error",
                    "error": traceback.format_exc()[-2000:],
                })
                _recover_servers(servers)
        browser.close()
    return True


# (app, repo) -> how many times the ready gate found no treemap on screen.
# Once an app has failed to draw a repo a couple of times, the remaining cells
# for that pair are recorded as failures without being run: the result is
# already established and re-measuring it costs a minute per cell.
_GATE_FAILURES: dict = {}


def _run_cell(browser, cfg, servers, paths, prof, trial, is_warmup,
              app_name, repo, scen, chrome_version, progress):
    """Run one matrix cell. All result rows are buffered and written in a single
    atomic append at the end, so an abort leaves nothing partial on disk."""
    app_cfg = cfg["apps"][app_name]
    server = servers[app_name]
    tnum = trial - cfg["warmup_trials"]
    steps = paths.get(repo["name"], [])
    md = cfg.get("depth_normalize") if app_name == "plotly" else None

    base_row = {
        "ts": utcnow(), "run_id": RUN_ID, "app": app_name, "repo": repo["name"],
        "repo_nodes": repo["nodes"], "throttle": prof["name"],
        "scenario": scen, "trial": tnum, "warmup": is_warmup,
        "chrome": chrome_version, "depth_normalize": md,
    }
    tag = f"{app_name}/{repo['name']}/{prof['name']}/{scen}/t{tnum}"
    print(f"[cell {progress}] {tag}{' (warmup)' if is_warmup else ''}", flush=True)

    rows: list[dict] = []
    fail_key = (app_name, repo["name"])
    limit = cfg.get("abandon_after_failures")
    if limit and _GATE_FAILURES.get(fail_key, 0) >= limit:
        print(f"  skipped: {app_name} does not render {repo['name']}", flush=True)
        if not is_warmup:
            append_jsonl_many(TRIALS_PATH, [
                {**base_row, "step_kind": "load" if scen == "load" else "prep_failed",
                 "render_failed": True, "tiles": 0, "skipped": True},
                {**base_row, "step_kind": "scenario_done"},
            ])
        return

    context = browser.new_context(
        viewport=cfg["viewport"], device_scale_factor=1, service_workers="block",
    )
    try:
        context.add_init_script(
            instrument_for(repo["duks"] if app_name == "duks" else None, md))
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        apply_throttle(cdp, prof)

        tracer = None
        if cfg.get("save_trace") and not is_warmup:
            tracer = TraceRecorder(cdp)

        url_tmpl = repo.get("url_override", {}).get(app_name) or app_cfg["url_template"]
        url = url_tmpl.format(base=server.base_url, repo=repo.get(app_name, ""))

        if tracer:
            if scen != "load":
                open_treemap(page, app_name, app_cfg, url, cfg)
            tracer.start()

        if scen == "load":
            m = open_treemap(page, app_name, app_cfg, url, cfg)
            if m.get("render_failed"):
                _GATE_FAILURES[fail_key] = _GATE_FAILURES.get(fail_key, 0) + 1
            rows.append({**base_row, "step_kind": "load", **m})
            if cfg.get("save_screenshots") and not is_warmup:
                _shot(page, f"{tag.replace('/', '_')}.png")
        else:
            load_m = open_treemap(page, app_name, app_cfg, url, cfg)
            if load_m.get("render_failed"):
                _GATE_FAILURES[fail_key] = _GATE_FAILURES.get(fail_key, 0) + 1
            if load_m.get("load_error") or not steps:
                rows.append({**base_row, "step_kind": "prep_failed",
                             "load_error": load_m.get("load_error"),
                             "render_failed": load_m.get("render_failed"),
                             "tiles": load_m.get("tiles"),
                             "have_steps": bool(steps)})
            else:
                if scen == "zoom_in":
                    gen = zoom_in(page, app_name, steps, cfg)
                elif scen == "zoom_out":
                    for s in steps:
                        _quiet_click(page, app_name, s["path"], cfg)
                    gen = zoom_out(page, app_name, steps, cfg)
                elif scen == "zoom_toggle":
                    gen = zoom_toggle(page, app_name, steps[0], cfg["toggle_reps"], cfg)
                else:
                    gen = iter(())
                for i, m in enumerate(gen):
                    rows.append({**base_row, "step_index": i, **m})

        if tracer:
            TRACES.mkdir(parents=True, exist_ok=True)
            tracer.stop(TRACES / f"{tag.replace('/', '_')}.json")
    finally:
        try:
            context.close()
        except Exception:
            pass

    # Atomic commit of the whole cell (skipped for warmup, which is unmeasured).
    if not is_warmup:
        append_jsonl_many(TRIALS_PATH, rows + [{**base_row, "step_kind": "scenario_done"}])


def _quiet_click(page, app_name, path, cfg):
    """Perform a drill-in click without measuring, then wait for it to settle."""
    from .browser import _click_target, _plotly_click_or_level
    page.evaluate("() => window.__bench.beginInteraction()")
    if app_name == "duks":
        _click_target(page, "duksTarget", path)
    else:
        _plotly_click_or_level(page, path)
    page.evaluate("([q,t]) => window.__bench.waitSettle(q,t)",
                  [cfg["settle_quiet_ms_interaction"], cfg["interaction_timeout_ms"]])


def _shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(SHOTS / name))
    except Exception as e:
        print(f"[shot] {name} failed: {e!r}", flush=True)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    cfg = filter_cfg(load_config(args.config), args)
    RESULTS.mkdir(parents=True, exist_ok=True)
    _install_signal_handlers()

    if args.fresh:
        archive_results()

    servers = build_servers(cfg)
    for s in servers.values():
        s.up(build=True)

    completed = True
    try:
        need_paths = any(sc != "load" for sc in cfg["scenarios"])
        paths = {}
        if need_paths and not _Stop.requested:
            paths = compute_paths(cfg, servers["duks"], cfg["repos"], str(PATHS_PATH))

        if not args.no_server and not _Stop.requested:
            run_server_phase(cfg, servers, str(SERVER_PATH), cfg["repos"])

        if not args.no_browser and not _Stop.requested:
            completed = run_browser_phase(cfg, servers, paths, args)
    except KeyboardInterrupt:
        completed = False
        print("[stop] interrupted", flush=True)
    finally:
        if cfg.get("teardown") and not args.keep_up:
            for s in servers.values():
                s.down()

    print(f"\n{'Done.' if completed else 'Stopped (partial).'} Results in {RESULTS}/")
    print("  trials.jsonl  - browser measurements (whole cells only)")
    print("  server.jsonl  - server-side HTTP latency")
    if not completed:
        print("\nRe-run the same command to resume where it stopped.")
    else:
        print("Analyze with:  python -m analysis.analyze")


if __name__ == "__main__":
    main()
