"""Compute a canonical drill-down path for each repo, shared by both apps.

Both implementations key nodes by the same identifier string (the DUKS tree's
`path`, the Plotly trace's `ids` entry), so one path list drives identical
navigation in both. The rule: from the root, repeatedly descend into the
largest child directory, `drill_depth` levels deep.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .servers import Server
from .util import Stop


def _subtree_value(node: dict) -> float:
    kids = node.get("children")
    if not kids:
        return float(node.get("value") or 0)
    return sum(_subtree_value(k) for k in kids)


def _canonical(tree: dict, depth: int) -> list[dict]:
    out = []
    node = tree
    for _ in range(depth):
        dirs = [c for c in node.get("children", []) if c.get("children")]
        if not dirs:
            break
        dirs.sort(key=_subtree_value, reverse=True)
        node = dirs[0]
        out.append({
            "path": node["path"],
            "name": node.get("name", ""),
            "depth": len(out) + 1,
            "value": _subtree_value(node),
            "n_children": len(node.get("children", [])),
        })
    return out


def compute_paths(cfg: dict, duks: Server, repos: list[dict], out_file: str) -> dict:
    """Returns {repo_name: [step, ...]} and caches it to out_file."""
    cache = Path(out_file)
    cache.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(cache.read_text()) if cache.exists() else {}
    result = dict(existing)

    tmpl = cfg["apps"]["duks"]["server_url_template"]
    with httpx.Client(timeout=cfg["server_timing"]["timeout_s"]) as client:
        for r in repos:
            if Stop.requested:
                break
            if r["name"] in result and result[r["name"]]:
                continue
            url = tmpl.format(base=duks.base_url, repo=r["duks"])
            print(f"[paths] fetching tree for {r['name']}", flush=True)
            tree = client.get(url).json()
            if "error" in tree:
                raise RuntimeError(f"tree fetch for {r['name']}: {tree['error']}")
            steps = _canonical(tree, cfg["drill_depth"])
            result[r["name"]] = steps
            for s in steps:
                print(f"  d{s['depth']}: {s['path']}  ({s['n_children']} children)", flush=True)
            cache.write_text(json.dumps(result, indent=2))   # persist per repo

    return result
