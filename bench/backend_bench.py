#!/usr/bin/env python3
"""Compare the CSV -> JSON tree build across git refs.

Every backend optimisation in this project claims two things at once: it is
faster, AND it does not change the tree. A speedup that quietly corrupts the
hierarchy is worthless here -- an earlier version of this codebase shipped
exactly that bug (phantom duplicate directories from naive path splitting), so
the identity check below is not ceremony, it is the point.

Usage (from the repo root, with the project's python -- pyenv 3.10.4):

    python bench/backend_bench.py main perf-lookup-vectorize
    python bench/backend_bench.py --runs 5 main HEAD
    python bench/backend_bench.py --dump /tmp/tree.json perf-lookup-vectorize

Per ref it reports:
  cold     first /api/tree after a fresh module load (build + serialise)
  warm     second request -- i.e. the in-memory cache path, if that ref has one
  payload  response size and md5

Then it cross-checks every ref pair: identical md5 means the optimisation is
byte-for-byte behaviour preserving. It also asserts the structural invariants
documented in CLAUDE.md (internal nodes carry no `value`; leaves carry no
`children`), because those are what the layout maths depends on.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time


def repo_root():
    return subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()


def app_from_ref(ref, root, workdir):
    """Materialise `src/app.py` at `ref` into an isolated dir that can import it.

    app.py resolves its CSV as `<dir of app.py>/../data`, so the temp layout
    mirrors the repo (src/ next to data/) and data/ is symlinked to the real,
    gitignored dataset rather than copied.
    """
    src = os.path.join(workdir, ref.replace("/", "_"), "src")
    os.makedirs(src, exist_ok=True)
    code = subprocess.check_output(["git", "show", f"{ref}:src/app.py"], cwd=root)
    path = os.path.join(src, "app.py")
    with open(path, "wb") as fh:
        fh.write(code)
    link = os.path.join(os.path.dirname(src), "data")
    if not os.path.exists(link):
        os.symlink(os.path.join(root, "data"), link)
    return path


def load_module(path):
    """Import app.py fresh, so module-level caches start empty (a true cold run)."""
    spec = importlib.util.spec_from_file_location("appmod_bench", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_invariants(tree):
    """Assert the tree shape the frontend layout depends on (see CLAUDE.md)."""
    nodes = internal = leaves = bad_value = bad_children = 0
    stack = [tree]
    while stack:
        n = stack.pop()
        nodes += 1
        kids = n.get("children")
        if kids:
            internal += 1
            if "value" in n:
                bad_value += 1          # would double-count against d3's .sum()
            stack.extend(kids)
        else:
            leaves += 1
            if kids == []:
                bad_children += 1       # empty array should have been pruned
    return {
        "nodes": nodes, "internal": internal, "leaves": leaves,
        "internal_with_value": bad_value, "empty_children": bad_children,
    }


def measure(ref, path, runs):
    cold = []
    warm = None
    payload = digest = tree = None
    for i in range(runs):
        mod = load_module(path)         # fresh module == cold cache every run
        client = mod.app.test_client()
        t0 = time.perf_counter()
        resp = client.get("/api/tree")
        cold.append(time.perf_counter() - t0)
        if resp.status_code != 200:
            sys.exit(f"{ref}: /api/tree returned {resp.status_code}")
        if i == 0:
            body = resp.get_data()
            payload, digest = len(body), hashlib.md5(body).hexdigest()
            tree = json.loads(body)
            t0 = time.perf_counter()    # second hit on the same module = warm
            client.get("/api/tree")
            warm = time.perf_counter() - t0
    return {
        "ref": ref, "cold": cold, "warm": warm,
        "payload": payload, "md5": digest, "inv": check_invariants(tree),
        "tree": tree,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("refs", nargs="*", default=["main", "HEAD"],
                    help="git refs to compare (default: main HEAD)")
    ap.add_argument("--runs", type=int, default=3, help="cold runs per ref")
    ap.add_argument("--dump", metavar="PATH",
                    help="write the last ref's tree JSON here (feeds frontend_lookup_bench.mjs)")
    args = ap.parse_args()

    root = repo_root()
    csv = os.path.join(root, "data", "tokenFilesFull.csv")
    if not os.path.exists(csv):
        sys.exit(f"missing dataset: {csv}\n(data/ is gitignored -- see CLAUDE.md)")

    print(f"dataset: {csv} ({os.path.getsize(csv)/1048576:.1f} MB)")
    print(f"runs per ref: {args.runs}\n")

    results = []
    with tempfile.TemporaryDirectory() as workdir:
        for ref in args.refs:
            results.append(measure(ref, app_from_ref(ref, root, workdir), args.runs))

    print(f"{'ref':<28} {'cold (mean)':>12} {'min':>8} {'max':>8} {'warm':>10} {'payload':>10}")
    for r in results:
        print(f"{r['ref']:<28} {statistics.mean(r['cold']):>11.3f}s "
              f"{min(r['cold']):>7.3f}s {max(r['cold']):>7.3f}s "
              f"{r['warm']*1000:>9.2f}ms {r['payload']/1048576:>9.2f}MB")

    print("\ninvariants (internal_with_value and empty_children must be 0):")
    for r in results:
        i = r["inv"]
        print(f"  {r['ref']:<26} nodes={i['nodes']:<7} internal={i['internal']:<6} "
              f"leaves={i['leaves']:<7} internal_with_value={i['internal_with_value']} "
              f"empty_children={i['empty_children']}")

    if len(results) > 1:
        print("\noutput identity:")
        base = results[0]
        for r in results[1:]:
            same = r["md5"] == base["md5"]
            print(f"  {base['ref']} vs {r['ref']}: "
                  f"{'IDENTICAL' if same else 'DIFFERENT'}  ({base['md5']} / {r['md5']})")
            if not same:
                a, b = json.dumps(base["tree"], sort_keys=True), json.dumps(r["tree"], sort_keys=True)
                print(f"    normalised (sorted keys) equal: {a == b}")

        speedups = [statistics.mean(base["cold"]) / statistics.mean(r["cold"]) for r in results[1:]]
        print("\nspeedup vs first ref:")
        for r, s in zip(results[1:], speedups):
            print(f"  {r['ref']}: {s:.1f}x cold")

    if args.dump:
        with open(args.dump, "w") as fh:
            json.dump(results[-1]["tree"], fh)
        print(f"\nwrote {args.dump} ({os.path.getsize(args.dump)/1048576:.2f} MB)")


if __name__ == "__main__":
    main()
