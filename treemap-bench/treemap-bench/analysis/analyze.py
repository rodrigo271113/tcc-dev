"""Turn results/*.jsonl into summary tables + figures.

    python -m analysis.analyze          # writes results/summary_*.csv and results/figs/*.png

Importable from the notebook too: `from analysis.analyze import load_trials, ...`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
FIGS = RESULTS / "figs"

# Primary latency column per step kind.
LATENCY_COL = {
    "load": "treemap_ready_ms",
    "zoom_in": "settle_ms",
    "zoom_out": "settle_ms",
    "toggle_in": "settle_ms",
    "toggle_out": "settle_ms",
}


def _read_jsonl(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return pd.DataFrame(rows)


def load_trials(path: str | Path = RESULTS / "trials.jsonl") -> pd.DataFrame:
    df = _read_jsonl(Path(path))
    if df.empty:
        return df
    df = df[~df["step_kind"].isin(["scenario_done", "cell_error", "prep_failed"])].copy()
    df = df[df.get("warmup", False) != True]  # noqa: E712
    # Drop loads that never put a treemap on screen. `render_failed` marks them
    # in runs made with the tile-count ready gate; for older runs, a load that
    # produced no first paint is the same failure (Plotly does this above ~146k
    # nodes: it builds an empty SVG and reports a fast, blank "load").
    if "render_failed" in df:
        df = df[df["render_failed"] != True]  # noqa: E712
    blank = (df["step_kind"] == "load") & df.get("fcp_ms", pd.Series(index=df.index)).isna()
    if "tiles" in df:
        blank |= (df["step_kind"] == "load") & (pd.to_numeric(df["tiles"], errors="coerce") == 0)
    dropped = df[blank][["app", "repo"]].value_counts()
    if len(dropped):
        print("[analyze] dropped non-rendering loads:")
        for (app, repo), n in dropped.items():
            print(f"  {app}/{repo}: {n}")
    # Interactions on a page that never rendered are meaningless too, so the
    # whole app+repo pair goes, not just its load rows.
    dead = set(map(tuple, df.loc[blank, ["app", "repo"]].drop_duplicates().values))
    if dead:
        df = df[~pd.Series(list(zip(df["app"], df["repo"])), index=df.index).isin(dead)]
    # unified latency
    df["latency_ms"] = np.where(
        df["step_kind"] == "load", df.get("treemap_ready_ms"), df.get("settle_ms")
    )
    df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce")
    return df


def load_server(path: str | Path = RESULTS / "server.jsonl") -> pd.DataFrame:
    df = _read_jsonl(Path(path))
    if df.empty:
        return df
    for c in ("ttfb_ms", "total_ms", "bytes"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _agg(s: pd.Series) -> pd.Series:
    s = s.dropna()
    if s.empty:
        return pd.Series({"n": 0, "median": np.nan, "p25": np.nan, "p75": np.nan,
                          "p95": np.nan, "iqr": np.nan})
    q = s.quantile([0.25, 0.5, 0.75, 0.95])
    return pd.Series({
        "n": len(s), "median": q[0.5], "p25": q[0.25], "p75": q[0.75],
        "p95": q[0.95], "iqr": q[0.75] - q[0.25],
    })


def _sort_if(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    have = [c for c in cols if c in df.columns]
    return df.sort_values(have) if have and len(df) else df


def summary_by_scenario(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    keys = ["throttle", "scenario", "step_kind", "repo", "repo_nodes", "app"]
    out = (df.groupby(keys, dropna=False)["latency_ms"]
             .apply(_agg).unstack().reset_index())
    return _sort_if(out, ["throttle", "scenario", "repo_nodes", "app"])


def load_detail(df: pd.DataFrame) -> pd.DataFrame:
    """Initial-load breakdown (server ttfb, fcp, lcp, ready, transfer bytes)."""
    d = df[df["step_kind"] == "load"].copy()
    cols = ["throttle", "app", "repo", "repo_nodes", "ttfb_ms", "fcp_ms", "lcp_ms",
            "treemap_ready_ms", "tbt_ms", "longtask_max_ms", "js_heap_bytes",
            "total_transfer_bytes", "doc_decoded_bytes", "request_count",
            "settle_timed_out", "load_error"]
    d = d[[c for c in cols if c in d.columns]]
    if d.empty:
        return d
    g = d.groupby(["throttle", "app", "repo", "repo_nodes"], dropna=False).median(numeric_only=True)
    return _sort_if(g.reset_index(), ["throttle", "repo_nodes", "app"])


def mannwhitney(df: pd.DataFrame) -> pd.DataFrame:
    """plotly vs duks per (throttle, scenario, step_kind, repo)."""
    from scipy.stats import mannwhitneyu
    rows = []
    keys = ["throttle", "scenario", "step_kind", "repo", "repo_nodes"]
    for key, g in df.groupby(keys, dropna=False):
        a = g[g.app == "plotly"]["latency_ms"].dropna()
        b = g[g.app == "duks"]["latency_ms"].dropna()
        if len(a) < 3 or len(b) < 3:
            continue
        u, p = mannwhitneyu(a, b, alternative="two-sided")
        # Cliff's delta
        delta = 2 * u / (len(a) * len(b)) - 1
        rows.append({**dict(zip(keys, key)),
                     "plotly_median": a.median(), "duks_median": b.median(),
                     "ratio_plotly_over_duks": a.median() / b.median() if b.median() else np.nan,
                     "U": u, "p": p, "cliffs_delta": delta})
    return _sort_if(pd.DataFrame(rows), ["throttle", "scenario", "repo_nodes"])


def scaling_fit(df: pd.DataFrame) -> pd.DataFrame:
    """log-log slope of median latency vs node count, per app x scenario x step."""
    rows = []
    for (thr, app, scen, sk), g in df.groupby(
            ["throttle", "app", "scenario", "step_kind"], dropna=False):
        m = g.groupby("repo_nodes")["latency_ms"].median().dropna()
        m = m[m > 0]
        if len(m) < 3:
            continue
        slope, intercept = np.polyfit(np.log10(m.index.values), np.log10(m.values), 1)
        rows.append({"throttle": thr, "app": app, "scenario": scen, "step_kind": sk,
                     "loglog_slope": slope, "n_points": len(m)})
    return _sort_if(pd.DataFrame(rows), ["throttle", "scenario", "app"])


# ---- figures --------------------------------------------------------------

def _style():
    import matplotlib.pyplot as plt
    plt.rcParams.update({"figure.dpi": 120, "font.size": 10,
                         "axes.grid": True, "grid.alpha": 0.3})


COLORS = {"plotly": "#4C78A8", "duks": "#F58518"}


def fig_latency_vs_size(df: pd.DataFrame, step_kind: str, throttle: str = "native"):
    import matplotlib.pyplot as plt
    _style()
    d = df[(df.step_kind == step_kind) & (df.throttle == throttle)]
    if d.empty:
        return None
    fig, ax = plt.subplots(figsize=(6, 4))
    for app, g in d.groupby("app"):
        m = g.groupby(["repo_nodes"])["latency_ms"].agg(["median",
             lambda x: x.quantile(.25), lambda x: x.quantile(.75)])
        m.columns = ["median", "p25", "p75"]
        m = m.sort_index()
        ax.plot(m.index, m["median"], "o-", label=app, color=COLORS.get(app))
        ax.fill_between(m.index, m["p25"], m["p75"], alpha=0.15, color=COLORS.get(app))
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("nodes in repository"); ax.set_ylabel(f"{step_kind} latency (ms)")
    ax.set_title(f"{step_kind} latency vs repo size  ({throttle})")
    ax.legend()
    fig.tight_layout()
    return fig


def fig_box_by_scenario(df: pd.DataFrame, repo: str, throttle: str = "native"):
    import matplotlib.pyplot as plt
    _style()
    d = df[(df.repo == repo) & (df.throttle == throttle)]
    if d.empty:
        return None
    kinds = ["load", "zoom_in", "zoom_out", "toggle_in", "toggle_out"]
    kinds = [k for k in kinds if k in d.step_kind.unique()]
    fig, axes = plt.subplots(1, len(kinds), figsize=(3 * len(kinds), 4), sharey=False)
    if len(kinds) == 1:
        axes = [axes]
    for ax, k in zip(axes, kinds):
        sub = d[d.step_kind == k]
        data = [sub[sub.app == a]["latency_ms"].dropna() for a in ("plotly", "duks")]
        ax.boxplot(data, tick_labels=["plotly", "duks"], showfliers=False)
        ax.set_title(k); ax.set_ylabel("ms")
    fig.suptitle(f"{repo}  ({throttle})")
    fig.tight_layout()
    return fig


def fig_transfer_vs_size(df: pd.DataFrame):
    import matplotlib.pyplot as plt
    _style()
    d = df[df.step_kind == "load"]
    if d.empty or "total_transfer_bytes" not in d.columns:
        return None
    fig, ax = plt.subplots(figsize=(6, 4))
    for app, g in d.groupby("app"):
        m = g.groupby("repo_nodes")["total_transfer_bytes"].median().sort_index()
        ax.plot(m.index, m.values / 1e6, "o-", label=app, color=COLORS.get(app))
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("nodes in repository"); ax.set_ylabel("page transfer (MB)")
    ax.set_title("Bytes over the wire vs repo size")
    ax.legend(); fig.tight_layout()
    return fig


def fig_server_latency(sv: pd.DataFrame):
    import matplotlib.pyplot as plt
    _style()
    if sv.empty:
        return None
    fig, ax = plt.subplots(figsize=(6, 4))
    for (app, mode), g in sv.groupby(["app", "mode"]):
        m = g.groupby("repo_nodes")["total_ms"].median().sort_index()
        ax.plot(m.index, m.values, "o-", label=f"{app}/{mode}")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("nodes"); ax.set_ylabel("server response time (ms)")
    ax.set_title("Server-side latency vs repo size")
    ax.legend(); fig.tight_layout()
    return fig


def main():
    FIGS.mkdir(parents=True, exist_ok=True)
    df = load_trials()
    sv = load_server()
    if df.empty:
        print("no trials yet")
        return

    summary_by_scenario(df).to_csv(RESULTS / "summary_by_scenario.csv", index=False)
    load_detail(df).to_csv(RESULTS / "summary_load_detail.csv", index=False)
    scaling_fit(df).to_csv(RESULTS / "summary_scaling.csv", index=False)
    try:
        mannwhitney(df).to_csv(RESULTS / "summary_stats.csv", index=False)
    except Exception as e:
        print(f"stats skipped: {e!r}")

    saved = []
    for sk in ("load", "zoom_in", "zoom_out", "toggle_in"):
        f = fig_latency_vs_size(df, sk)
        if f:
            p = FIGS / f"latency_vs_size_{sk}.png"
            f.savefig(p); saved.append(p)
    f = fig_transfer_vs_size(df)
    if f:
        p = FIGS / "transfer_vs_size.png"; f.savefig(p); saved.append(p)
    for repo in df.repo.unique():
        f = fig_box_by_scenario(df, repo)
        if f:
            p = FIGS / f"box_{repo}.png"; f.savefig(p); saved.append(p)
    f = fig_server_latency(sv)
    if f:
        p = FIGS / "server_latency.png"; f.savefig(p); saved.append(p)

    print("wrote:")
    for p in [RESULTS / "summary_by_scenario.csv", RESULTS / "summary_load_detail.csv",
              RESULTS / "summary_scaling.csv", RESULTS / "summary_stats.csv"]:
        print(f"  {p}")
    for p in saved:
        print(f"  {p}")


if __name__ == "__main__":
    main()
