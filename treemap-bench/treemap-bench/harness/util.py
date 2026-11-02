"""Small shared helpers."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()


class Stop:
    """Cooperative-shutdown flag, set by run_bench's signal handler and checked
    in the long phases so Ctrl-C lands between units of work, not mid-write."""
    requested = False


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: str | Path, row: dict) -> None:
    append_jsonl_many(path, [row])


def append_jsonl_many(path: str | Path, rows: list[dict]) -> None:
    """Append rows in ONE write() + fsync, so a crash leaves either all of them
    or none — never a half-written batch. Used to make a benchmark cell atomic:
    the reader treats a cell as done only when its trailing marker row is present."""
    if not rows:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(json.dumps(r, default=str) + "\n" for r in rows)
    with _lock:
        with path.open("a") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())


def read_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
