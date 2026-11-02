"""Browser-side measurement: initial load + zoom in/out latency, via Playwright."""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

INSTRUMENT_JS = (Path(__file__).parent / "instrument.js").read_text()

LAUNCH_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-features=CalculateNativeWinOcclusion",
    "--force-color-profile=srgb",
    "--hide-scrollbars",
]


class TraceRecorder:
    """Chrome DevTools performance trace for one interaction window (optional)."""

    def __init__(self, cdp):
        self.cdp = cdp
        self._chunks: list = []

    def start(self):
        self._chunks = []
        self.cdp.on("Tracing.dataCollected", lambda p: self._chunks.extend(p.get("value", [])))
        self.cdp.send("Tracing.start", {
            "traceConfig": {
                "recordMode": "recordAsMuchAsPossible",
                "includedCategories": [
                    "devtools.timeline", "blink.user_timing", "v8.execute",
                    "disabled-by-default-devtools.timeline",
                    "disabled-by-default-devtools.timeline.frame",
                ],
            },
            "transferMode": "ReportEvents",
        })

    def stop(self, out_path: Path):
        done = {"v": False}
        self.cdp.on("Tracing.tracingComplete", lambda p: done.__setitem__("v", True))
        self.cdp.send("Tracing.end")
        deadline = time.time() + 20
        while not done["v"] and time.time() < deadline:
            time.sleep(0.05)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"traceEvents": self._chunks}))


# ---------------------------------------------------------------------------

def instrument_for(repo_duks_id: str | None, maxdepth: int | None) -> str:
    js = INSTRUMENT_JS
    js = js.replace("__BENCH_REPO__", repo_duks_id or "")
    js = js.replace("__BENCH_MAXDEPTH__", str(maxdepth) if maxdepth else "")
    return js


def apply_throttle(cdp, profile: dict) -> None:
    rate = profile.get("cpu", 1) or 1
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})
    net = profile.get("net")
    cdp.send("Network.enable")
    if net:
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False,
            "downloadThroughput": net["download"] / 8,
            "uploadThroughput": net["upload"] / 8,
            "latency": net["latency"],
        })
    else:
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "downloadThroughput": -1,
            "uploadThroughput": -1, "latency": 0,
        })


# ---------------------------------------------------------------------------

def open_treemap(page: Page, app_name: str, app_cfg: dict, url: str, cfg: dict) -> dict:
    """Navigate and wait until the treemap has stopped churning. Returns metrics."""
    sel = app_cfg["container_selector"]
    quiet = cfg["settle_quiet_ms_load"]
    timeout = cfg["load_timeout_ms"]

    t_goto = time.perf_counter()
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    page.evaluate("(s) => window.__bench.watchContainer(s)", sel)
    if app_name == "plotly":
        page.evaluate("() => window.__bench.plotlyHook()")

    # The ready gate gets its own (shorter) budget: it decides whether the app
    # drew a treemap at all, and an app that cannot draw this repo should be
    # recorded as such quickly rather than after the full navigation timeout.
    ready_timeout = cfg.get("ready_timeout_ms") or timeout
    err = None
    render_failed = False
    try:
        page.wait_for_function(app_cfg["ready_js"], timeout=ready_timeout)
    except Exception as e:
        err = repr(e)
        render_failed = True

    if render_failed:
        settle = {"settle_ms": None, "wall_ms": (time.perf_counter() - t_goto) * 1000,
                  "timed_out": True}
    else:
        try:
            if app_name == "plotly":
                page.evaluate("() => window.__bench.plotlyHook()")
            settle = page.evaluate(
                "([q,t]) => window.__bench.waitSettle(q,t)", [quiet, timeout]
            )
        except Exception as e:
            err = repr(e)
            settle = {"settle_ms": None, "wall_ms": (time.perf_counter() - t_goto) * 1000,
                      "timed_out": True}

    snap = page.evaluate("() => window.__bench.snapshot()")
    # Counted after settle: how much the app actually put on screen. Zero tiles
    # with no error is the blank-page failure the gate above exists to catch.
    tiles = page.evaluate("(s) => document.querySelectorAll(s).length",
                          app_cfg.get("tile_selector", app_cfg["container_selector"]))
    dom_nodes = page.evaluate("() => document.getElementsByTagName('*').length")
    row = {
        "nav_wall_ms": (time.perf_counter() - t_goto) * 1000,
        "treemap_ready_ms": settle.get("settle_ms"),
        "settle_timed_out": settle.get("timed_out"),
        "load_error": err,
        "render_failed": render_failed,
        "tiles": tiles,
        "dom_nodes": dom_nodes,
        **{k: settle.get(k) for k in (
            "longtask_count", "longtask_total_ms", "longtask_max_ms", "tbt_ms",
            "frames", "jank_frames", "long_frames", "min_fps", "median_frame_ms",
        )},
        **{k: snap.get(k) for k in (
            "fcp_ms", "lcp_ms", "cls", "ttfb_ms", "dcl_ms", "load_ms",
            "doc_transfer_bytes", "doc_decoded_bytes", "total_transfer_bytes",
            "total_decoded_bytes", "request_count", "js_heap_bytes",
        )},
    }
    return row


def _click_target(page: Page, locator_js: str, arg=None) -> dict:
    res = page.evaluate(f"(a) => window.__bench.{locator_js}(a)", arg) if arg is not None \
        else page.evaluate(f"() => window.__bench.{locator_js}()")
    if not res or not res.get("ok"):
        return {"ok": False, "reason": (res or {}).get("reason", "no result")}
    page.mouse.move(res["cx"], res["cy"])
    page.mouse.click(res["cx"], res["cy"])
    return {"ok": True, "cx": res["cx"], "cy": res["cy"]}


def _measure_interaction(page: Page, do_click, cfg: dict) -> dict:
    quiet = cfg["settle_quiet_ms_interaction"]
    timeout = cfg["interaction_timeout_ms"]
    page.evaluate("() => window.__bench.beginInteraction()")
    click = do_click()
    if not click.get("ok"):
        page.evaluate("() => { window.__bench._frameOn = false; }")
        return {"click_ok": False, "click_reason": click.get("reason"),
                "click_via": click.get("via"), "settle_ms": None, "timed_out": True}
    settle = page.evaluate("([q,t]) => window.__bench.waitSettle(q,t)", [quiet, timeout])
    settle["click_ok"] = True
    settle["click_via"] = click.get("via")
    return settle


def zoom_in(page: Page, app_name: str, steps: list[dict], cfg: dict):
    """Drill into each step; yields one metrics row per level."""
    for s in steps:
        if app_name == "duks":
            do = lambda p=s["path"]: _click_target(page, "duksTarget", p)
        else:
            do = lambda p=s["path"]: _plotly_click_or_level(page, p)
        m = _measure_interaction(page, do, cfg)
        m.update(step_kind="zoom_in", target_path=s["path"], target_depth=s["depth"])
        yield m


def zoom_out(page: Page, app_name: str, steps: list[dict], cfg: dict):
    """From the deepest level, climb back to the root; one row per level."""
    ancestors = [s["path"] for s in steps][:-1][::-1] + [""]  # parents then root
    for i, parent in enumerate(ancestors):
        if app_name == "duks":
            do = lambda: _click_target(page, "duksBreadcrumb")
        else:
            do = lambda p=parent: _plotly_click_or_level(page, p)
        m = _measure_interaction(page, do, cfg)
        m.update(step_kind="zoom_out", target_path=parent,
                 target_depth=len(ancestors) - 1 - i)
        yield m


def zoom_toggle(page: Page, app_name: str, first_step: dict, reps: int, cfg: dict):
    """Repeatedly zoom into one top-level directory and back to root."""
    for rep in range(reps):
        if app_name == "duks":
            din = lambda p=first_step["path"]: _click_target(page, "duksTarget", p)
            dout = lambda: _click_target(page, "duksBreadcrumb")
        else:
            din = lambda p=first_step["path"]: _plotly_click_or_level(page, p)
            dout = lambda: _plotly_click_or_level(page, "")
        mi = _measure_interaction(page, din, cfg)
        mi.update(step_kind="toggle_in", target_path=first_step["path"],
                  target_depth=1, rep=rep)
        yield mi
        mo = _measure_interaction(page, dout, cfg)
        mo.update(step_kind="toggle_out", target_path="", target_depth=0, rep=rep)
        yield mo


def _plotly_click_or_level(page: Page, path: str) -> dict:
    """Prefer a real click on the tile/pathbar; fall back to the Plotly API."""
    res = page.evaluate("(p) => window.__bench.plotlyTarget(p)", path)
    if res and res.get("ok"):
        page.mouse.move(res["cx"], res["cy"])
        page.mouse.click(res["cx"], res["cy"])
        return {"ok": True, "via": "click"}
    try:
        page.evaluate("(p) => window.__bench.plotlyLevel(p)", path)
        return {"ok": True, "via": "api"}
    except Exception as e:
        return {"ok": False, "reason": f"plotly level failed: {e!r}"}
