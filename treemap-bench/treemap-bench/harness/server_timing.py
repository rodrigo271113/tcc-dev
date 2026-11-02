"""Server-side latency, measured with a plain HTTP client (no browser).

Separates the cost of *producing* the treemap payload (pandas.apply for Plotly,
polars + LRU cache for DUKS) from anything the browser does. Plotly has no
cache, so every hit is effectively cold; DUKS is measured cold (after a
container restart) and warm (repeat hits).
"""
from __future__ import annotations

import random
import time
from collections import Counter
from pathlib import Path

import httpx

from .servers import Server
from .util import Stop, append_jsonl, read_jsonl, utcnow


def _time_get(client: httpx.Client, url: str, timeout: float) -> dict:
    t0 = time.perf_counter()
    first = None
    nbytes = 0
    status = None
    try:
        with client.stream("GET", url, timeout=timeout) as r:
            status = r.status_code
            for chunk in r.iter_bytes():
                if first is None:
                    first = time.perf_counter()
                nbytes += len(chunk)
        end = time.perf_counter()
        return {
            "status": status,
            "ttfb_ms": (first - t0) * 1000 if first else None,
            "total_ms": (end - t0) * 1000,
            "bytes": nbytes,
            "error": None,
        }
    except Exception as e:
        return {
            "status": status,
            "ttfb_ms": None,
            "total_ms": (time.perf_counter() - t0) * 1000,
            "bytes": nbytes,
            "error": repr(e),
        }


def _existing_counts(out_path: str) -> Counter:
    """(app, repo, mode, round) -> how many samples already on disk."""
    c = Counter()
    for row in read_jsonl(Path(out_path)):
        c[(row.get("app"), row.get("repo"), row.get("mode"), row.get("round", 0))] += 1
    return c


def run_server_phase(cfg: dict, servers: dict[str, Server], out_path: str,
                     repos: list[dict]) -> None:
    st = cfg["server_timing"]
    if not st.get("enabled", True):
        return
    timeout = float(st["timeout_s"])
    have = _existing_counts(out_path)          # resume: skip work already recorded
    if sum(have.values()):
        print(f"[server][resume] {sum(have.values())} samples already recorded", flush=True)

    def record(row):
        append_jsonl(out_path, {"ts": utcnow(), **row})

    with httpx.Client(follow_redirects=True) as client:
        for app_name, server in servers.items():
            if Stop.requested:
                break
            app_cfg = cfg["apps"][app_name]
            tmpl = app_cfg["server_url_template"]

            def url_for(r):
                t = r.get("server_override", {}).get(app_name) or tmpl
                return t.format(base=server.base_url, repo=r.get(app_name, ""))

            if app_cfg.get("has_server_cache"):
                # ---- DUKS: cold (one hit after a restart) + warm --------
                for rnd in range(int(st["duks_cold_restarts"])):
                    if Stop.requested:
                        break
                    pending = [r for r in repos
                               if not have[(app_name, r["name"], "cold", rnd)]]
                    if not pending:
                        continue
                    print(f"[server] {app_name} cold round {rnd + 1} "
                          f"({len(pending)} repos)", flush=True)
                    server.restart()
                    random.shuffle(pending)
                    for r in pending:
                        if Stop.requested:
                            break
                        res = _time_get(client, url_for(r), timeout)
                        record({"app": app_name, "repo": r["name"],
                                "repo_nodes": r["nodes"], "mode": "cold",
                                "round": rnd, "sample": 0, **res})
                warm_n = int(st["warm_samples"])
                for r in repos:
                    if Stop.requested:
                        break
                    done = have[(app_name, r["name"], "warm", 0)]
                    if done >= warm_n:
                        continue
                    _time_get(client, url_for(r), timeout)          # prime
                    for s in range(done, warm_n):
                        res = _time_get(client, url_for(r), timeout)
                        record({"app": app_name, "repo": r["name"],
                                "repo_nodes": r["nodes"], "mode": "warm",
                                "round": 0, "sample": s, **res})
            else:
                # ---- Plotly: no cache, every hit recomputes -------------
                n = int(st.get("plotly_samples", 15))
                for r in repos:
                    if Stop.requested:
                        break
                    done = have[(app_name, r["name"], "nocache", 0)]
                    if done >= n:
                        continue
                    for s in range(done, n):
                        res = _time_get(client, url_for(r), timeout)
                        record({"app": app_name, "repo": r["name"],
                                "repo_nodes": r["nodes"], "mode": "nocache",
                                "round": 0, "sample": s, **res})
                        if res["error"]:
                            print(f"[server] {app_name}/{r['name']} error: "
                                  f"{res['error']}", flush=True)
                            break
    print(f"[server] {'stopped' if Stop.requested else 'wrote'} {out_path}", flush=True)
