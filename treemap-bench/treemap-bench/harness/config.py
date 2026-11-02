"""Config loading + path resolution."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike = "config.yaml") -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    cfg = yaml.safe_load(p.read_text())

    # Resolve the two implementation checkouts to absolute paths.
    paths = cfg.setdefault("paths", {})
    plotly_repo = _abs(paths.get("plotly_repo", "../rep_pack_march6"))
    duks_repo = _abs(paths.get("duks_repo", "../DUKS"))
    paths["plotly_repo"] = str(plotly_repo)
    paths["duks_repo"] = str(duks_repo)

    subst = {"duks_repo": str(duks_repo), "plotly_repo": str(plotly_repo)}
    for app in cfg["apps"].values():
        app["compose_cwd"] = _fmt(app.get("compose_cwd", "."), subst)
        if not os.path.isabs(app["compose_cwd"]):
            app["compose_cwd"] = str((REPO_ROOT / app["compose_cwd"]).resolve())

    cfg["_repo_root"] = str(REPO_ROOT)
    return cfg


def _abs(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def _fmt(s: str, subst: dict) -> str:
    for k, v in subst.items():
        s = s.replace("{" + k + "}", v)
    return s
