// Injected at document_start into every benchmarked page (both apps).
// Provides window.__bench: performance collection, a generic "settle" detector,
// and per-app helpers to locate a tile for a real mouse click.
//
// Placeholders replaced by the harness before injection:
//   __BENCH_REPO__      DUKS repo id (redirects the first /tree fetch), or ""
//   __BENCH_MAXDEPTH__  integer to force Plotly maxdepth, or ""
(function () {
  if (window.__bench) return;

  var B = {
    marks: {},
    longtasks: [],     // {start, dur}
    events: [],        // {name, start, dur}
    fcp: null,
    lcp: null,
    cls: 0,
    _lastMutation: 0,
    _lastLongtask: 0,
    _lastAfterplot: 0,
    _mutObs: null,
    _interactStart: null,
    _ltBase: 0,
    _evBase: 0,
    _frameOn: false,
    _frameTimes: [],
  };
  window.__bench = B;

  var now = function () { return performance.now(); };

  // ---- DUKS: redirect the first knowledge-map fetch to the target repo -----
  var REPO = "__BENCH_REPO__";
  if (REPO && REPO.indexOf("__BENCH_") !== 0) {
    var _fetch = window.fetch;
    var redirected = false;
    window.fetch = function (input, init) {
      try {
        var u = (typeof input === "string") ? input : (input && input.url);
        // Force the FIRST /tree request to the benchmark's target repo,
        // replacing any repo= the page put there (DUKS defaults to an
        // explicit ?repo=tokenFilesFull, so "no repo param" is not the signal).
        if (!redirected && u && u.indexOf("/api/knowledge-map/tree") !== -1) {
          redirected = true;
          u = u.split("?")[0] + "?repo=" + encodeURIComponent(REPO);
          return _fetch.call(this, u, init);
        }
      } catch (e) {}
      return _fetch.apply(this, arguments);
    };
  }

  // ---- Plotly: optionally force maxdepth for a depth-normalized comparison --
  var MAXDEPTH = "__BENCH_MAXDEPTH__";
  if (/^\d+$/.test(MAXDEPTH)) {
    var target = parseInt(MAXDEPTH, 10);
    var patch = function (data) {
      try {
        if (Array.isArray(data)) {
          for (var i = 0; i < data.length; i++) {
            var t = data[i];
            if (t && (t.type === "treemap" || t.type === "icicle" || t.type === "sunburst")) {
              t.maxdepth = target;
            }
          }
        }
      } catch (e) {}
      return data;
    };
    var install = function () {
      if (!window.Plotly || window.Plotly.__benchPatched) return !!window.Plotly;
      ["newPlot", "react"].forEach(function (fn) {
        if (typeof window.Plotly[fn] === "function") {
          var orig = window.Plotly[fn];
          window.Plotly[fn] = function (gd, data) {
            var rest = Array.prototype.slice.call(arguments, 2);
            return orig.apply(this, [gd, patch(data)].concat(rest));
          };
        }
      });
      window.Plotly.__benchPatched = true;
      return true;
    };
    if (!install()) {
      var iv = setInterval(function () { if (install()) clearInterval(iv); }, 5);
      setTimeout(function () { clearInterval(iv); }, 20000);
    }
  }

  // ---- performance observers ---------------------------------------------
  function obs(type, cb, extra) {
    try {
      var o = new PerformanceObserver(function (list) { cb(list.getEntries()); });
      o.observe(Object.assign({ type: type, buffered: true }, extra || {}));
    } catch (e) {}
  }
  obs("paint", function (es) {
    for (var i = 0; i < es.length; i++)
      if (es[i].name === "first-contentful-paint") B.fcp = es[i].startTime;
  });
  obs("largest-contentful-paint", function (es) {
    if (es.length) B.lcp = es[es.length - 1].startTime;
  });
  obs("longtask", function (es) {
    for (var i = 0; i < es.length; i++) {
      B.longtasks.push({ start: es[i].startTime, dur: es[i].duration });
      B._lastLongtask = now();
    }
  });
  obs("event", function (es) {
    for (var i = 0; i < es.length; i++)
      B.events.push({ name: es[i].name, start: es[i].startTime, dur: es[i].duration });
  }, { durationThreshold: 16 });
  obs("layout-shift", function (es) {
    for (var i = 0; i < es.length; i++) if (!es[i].hadRecentInput) B.cls += es[i].value;
  });

  // ---- mutation watching -------------------------------------------------
  B.watchContainer = function (sel) {
    var el = document.querySelector(sel);
    if (!el) return false;
    if (B._mutObs) B._mutObs.disconnect();
    B._lastMutation = now();
    B._mutObs = new MutationObserver(function () { B._lastMutation = now(); });
    B._mutObs.observe(el, { childList: true, subtree: true, attributes: true });
    return true;
  };

  // ---- Plotly afterplot hook (precise settle signal) --------------------
  B.plotlyHook = function () {
    try {
      var gd = document.querySelector("#plotlyCanvas");
      if (gd && gd.on && !gd.__benchHooked) {
        gd.on("plotly_afterplot", function () {
          B._lastAfterplot = now();
          B._lastMutation = now();
        });
        gd.__benchHooked = true;
        return true;
      }
    } catch (e) {}
    return false;
  };

  // ---- frame sampler ---------------------------------------------------
  function frameTick(t) {
    if (!B._frameOn) return;
    B._frameTimes.push(t);
    requestAnimationFrame(frameTick);
  }

  B.mark = function (n) { B.marks[n] = now(); return B.marks[n]; };

  B.beginInteraction = function () {
    B._interactStart = now();
    B._ltBase = B.longtasks.length;
    B._evBase = B.events.length;
    B._lastMutation = now();
    B._lastLongtask = 0;
    B._lastAfterplot = 0;
    B._frameOn = true;
    B._frameTimes = [];
    requestAnimationFrame(frameTick);
    return B._interactStart;
  };

  // Resolve once the container has had no mutations / long tasks / afterplots
  // for `quietMs`, or after `timeoutMs`. Returns a metrics summary.
  B.waitSettle = function (quietMs, timeoutMs) {
    quietMs = quietMs || 300;
    timeoutMs = timeoutMs || 60000;
    var t0 = now();
    var ref = (B._interactStart != null) ? B._interactStart : 0;
    return new Promise(function (resolve) {
      function lastActivity() {
        return Math.max(B._lastMutation, B._lastLongtask, B._lastAfterplot);
      }
      function check() {
        var t = now();
        if (t - lastActivity() >= quietMs) return resolve(finish(false));
        if (t - t0 >= timeoutMs) return resolve(finish(true));
        requestAnimationFrame(check);
      }
      function finish(timedOut) {
        B._frameOn = false;
        var settleAt = lastActivity();
        var lt = B.longtasks.slice(B._ltBase);
        var ev = B.events.slice(B._evBase);
        var d = [];
        for (var i = 1; i < B._frameTimes.length; i++)
          d.push(B._frameTimes[i] - B._frameTimes[i - 1]);
        var maxd = d.length ? Math.max.apply(null, d) : null;
        var sorted = d.slice().sort(function (a, b) { return a - b; });
        return {
          settle_ms: settleAt - ref,
          wall_ms: now() - ref,
          timed_out: timedOut,
          longtask_count: lt.length,
          longtask_total_ms: lt.reduce(function (s, e) { return s + e.dur; }, 0),
          longtask_max_ms: lt.reduce(function (m, e) { return Math.max(m, e.dur); }, 0),
          tbt_ms: lt.reduce(function (s, e) { return s + Math.max(0, e.dur - 50); }, 0),
          event_max_dur_ms: ev.reduce(function (m, e) { return Math.max(m, e.dur); }, 0),
          frames: d.length,
          jank_frames: d.filter(function (x) { return x > 25; }).length,
          long_frames: d.filter(function (x) { return x > 50; }).length,
          min_fps: maxd ? 1000 / maxd : null,
          median_frame_ms: sorted.length ? sorted[sorted.length >> 1] : null,
        };
      }
      check();
    });
  };

  B.snapshot = function () {
    var nav = performance.getEntriesByType("navigation")[0] || {};
    var res = performance.getEntriesByType("resource");
    var transfer = nav.transferSize || 0, decoded = nav.decodedBodySize || 0, reqs = 1;
    for (var i = 0; i < res.length; i++) {
      transfer += res[i].transferSize || 0;
      decoded += res[i].decodedBodySize || 0;
      reqs++;
    }
    return {
      fcp_ms: B.fcp,
      lcp_ms: B.lcp,
      cls: B.cls,
      ttfb_ms: nav.responseStart || null,
      response_end_ms: nav.responseEnd || null,
      dcl_ms: nav.domContentLoadedEventEnd || null,
      load_ms: nav.loadEventEnd || null,
      doc_transfer_bytes: nav.transferSize || null,
      doc_decoded_bytes: nav.decodedBodySize || null,
      total_transfer_bytes: transfer,
      total_decoded_bytes: decoded,
      request_count: reqs,
      js_heap_bytes: (performance.memory ? performance.memory.usedJSHeapSize : null),
      longtask_count: B.longtasks.length,
      longtask_total_ms: B.longtasks.reduce(function (s, e) { return s + e.dur; }, 0),
      tbt_ms: B.longtasks.reduce(function (s, e) { return s + Math.max(0, e.dur - 50); }, 0),
      marks: B.marks,
    };
  };

  // ---- tile locators: return viewport coords for a real mouse click -------
  B.duksTarget = function (path) {
    if (!window.d3) return { ok: false, reason: "no d3" };
    var gs = document.querySelectorAll("#chart g.node");
    for (var i = 0; i < gs.length; i++) {
      var d = window.d3.select(gs[i]).datum();
      if (d && d.data && d.data.path === path) {
        var r = gs[i].getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return { ok: false, reason: "too small" };
        // Click in the header band so we hit the directory, not a child tile.
        return { ok: true, cx: r.left + r.width / 2, cy: r.top + Math.min(10, r.height / 2) };
      }
    }
    return { ok: false, reason: "not found" };
  };

  B.duksBreadcrumb = function () {
    var el = document.querySelector("#breadcrumb");
    if (!el) return { ok: false };
    var r = el.getBoundingClientRect();
    return { ok: true, cx: r.left + 40, cy: r.top + r.height / 2 };
  };

  B.plotlyTarget = function (path) {
    var gd = document.querySelector("#plotlyCanvas");
    if (!gd) return { ok: false, reason: "no gd" };
    var slices = gd.querySelectorAll("g.slice, .pathbar .slice, g.pathbar-slice");
    for (var i = 0; i < slices.length; i++) {
      var d = slices[i].__data__;
      var id = d && d.data ? (d.data.id != null ? d.data.id : d.data.label) : undefined;
      if (id === path) {
        var node = slices[i].querySelector("path") || slices[i];
        var r = node.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return { ok: false, reason: "too small" };
        return { ok: true, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
      }
    }
    return { ok: false, reason: "not found" };
  };

  // Deterministic fallback / cross-check: drive Plotly zoom via the API.
  B.plotlyLevel = function (path) {
    var gd = document.querySelector("#plotlyCanvas");
    if (!gd || !window.Plotly) return Promise.reject("no plotly");
    return window.Plotly.restyle(gd, { level: [path] });
  };
})();
