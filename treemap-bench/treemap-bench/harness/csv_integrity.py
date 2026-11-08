"""Find and repair orphan parent references in a Pony Factor CSV.

Both implementations rebuild the directory hierarchy from the `identifier`
column alone: a node's parent is its identifier minus the trailing
`/node_name`. Nothing in the format guarantees that the parent row exists, and
in two of the sampled repositories it does not -- a directory is referenced as
a parent by its children but has no row of its own.

The two implementations disagree on what that means:

  * DUKS attaches an orphan to the root and renders the rest of the tree.
  * Plotly's treemap trace fails to build the hierarchy, reports it as a
    `console.warn`, and draws nothing at all. The page stays blank with no
    error visible to the user.

So a single missing row decides whether a 146k-node repository renders. That
is a finding in its own right, and `--check` is the way to reproduce it. But it
also means Plotly cannot be *measured* on those repositories, which is a
different question from whether it can render them. `--repair` synthesizes the
missing directory rows so the scaling comparison can reach the top of the
ladder; runs that use a repaired file must say so, since the rows are
fabricated.

Usage:
    python -m harness.csv_integrity --check <csv> [<csv> ...]
    python -m harness.csv_integrity --repair <csv> --out <csv>
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# The per-project exports use an interpunct; the token dumps use a comma. The
# server sniffs by trying one and falling back, so we do the same.
SEPARATORS = ("\xb7", ",")


def sniff_separator(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace") as fh:
        header = fh.readline()
    for sep in SEPARATORS:
        if header.count(sep) >= 3:
            return sep
    raise ValueError(f"{path}: could not determine separator from header")


def parent_of(identifier: str, node_name: str) -> str:
    """The server's rule: strip the trailing '/node_name' from the identifier."""
    if not node_name:
        return ""
    return identifier[: -(len(node_name) + 1)]


def load(path: Path) -> tuple[list[str], str, list[list[str]]]:
    sep = sniff_separator(path)
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.reader(fh, delimiter=sep))
    if not rows:
        raise ValueError(f"{path}: empty file")
    return rows[0], sep, [r for r in rows[1:] if len(r) >= 4]


def find_orphans(header: list[str], rows: list[list[str]]) -> dict[str, list[str]]:
    """Map each missing parent identifier to the children that reference it."""
    present = {r[0] for r in rows}
    orphans: dict[str, list[str]] = {}
    for row in rows:
        parent = parent_of(row[0], row[3])
        if parent and parent not in present:
            orphans.setdefault(parent, []).append(row[0])
    return orphans


def _numeric(value: str, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def synthesize(header: list[str], rows: list[list[str]]) -> list[list[str]]:
    """Build directory rows for every missing parent, ancestors included.

    A synthesized row takes its size from the sum of its descendants and its
    Pony Factor from the lowest among its children, which is the conservative
    (highest risk) reading. Both values are fabricated; the caller is expected
    to disclose that.
    """
    made: list[list[str]] = []
    while True:
        orphans = find_orphans(header, rows + made)
        if not orphans:
            return made
        by_id = {r[0]: r for r in rows + made}
        for parent, children in orphans.items():
            kids = [by_id[c] for c in children]
            size = sum(_numeric(k[2]) for k in kids)
            pf = min((_numeric(k[1]) for k in kids), default=0.0)
            name = parent.rsplit("/", 1)[-1]
            row = [parent, _fmt(pf), _fmt(size), name]
            # Pad to the file's own width; the extra per-project columns are
            # unused by both visualizations, except is_file, which must be
            # False so the node reads as a directory.
            while len(row) < len(header):
                row.append("False" if header[len(row)] == "is_file" else "0")
            made.append(row)


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def write(path: Path, header: list[str], sep: str, rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=sep, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv", nargs="+", type=Path)
    ap.add_argument("--check", action="store_true", help="report orphans, change nothing")
    ap.add_argument("--repair", action="store_true", help="write a repaired copy")
    ap.add_argument("--out", type=Path, help="output path for --repair (one input only)")
    args = ap.parse_args(argv)

    if args.repair and len(args.csv) != 1:
        ap.error("--repair takes exactly one input")
    if not (args.check or args.repair):
        args.check = True

    failures = 0
    for path in args.csv:
        header, sep, rows = load(path)
        orphans = find_orphans(header, rows)
        affected = sum(len(v) for v in orphans.values())
        print(f"{path.name:24s} nodes={len(rows):>8,}  orphan parents={len(orphans):>4,}"
              f"  rows affected={affected:>5,}")
        for parent in list(orphans)[:5]:
            print(f"    missing: {parent}")
        if len(orphans) > 5:
            print(f"    ... and {len(orphans) - 5} more")
        if orphans:
            failures += 1

        if args.repair:
            made = synthesize(header, rows)
            out = args.out or path.with_suffix(".repaired.csv")
            write(out, header, sep, rows + made)
            print(f"    synthesized {len(made)} directory row(s) -> {out}")
            for row in made:
                print(f"      + {row[0]}  (pf={row[1]}, size={row[2]})")

    return 1 if (args.check and not args.repair and failures) else 0


if __name__ == "__main__":
    sys.exit(main())
