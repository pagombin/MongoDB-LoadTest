/* Time-series charts on uPlot with one shared time axis, crosshair, and zoom.
   Classic script so the offline report can inline it. Defines window.BallastCharts. */
(function () {
  "use strict";
  var F = window.BallastFormat;
  var LANE_ROWS = ["Markers", "Topology", "Errors", "Gaps, pool"];
  var Y_AXIS = 72;
  var LANE_ROW_H = 20;
  var syncCount = 0;

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  }

  function theme() {
    return {
      ink: css("--ink"), muted: css("--muted"), rule: css("--rule"), surface: css("--surface"), accent: css("--accent"),
      fail: css("--fail"), warn: css("--warn"), ok: css("--ok"), shade: css("--shade"), window: css("--window"),
      c1: css("--c1"), c2: css("--c2"), c3: css("--c3"), c4: css("--c4"), c5: css("--c5"), c6: css("--c6"),
      font: "12px " + css("--font-sans"), mono: "11px " + css("--font-mono")
    };
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function tzDate() {
    return F.isUtc() ? function (ts) { return uPlot.tzDate(new Date(ts * 1000), "Etc/UTC"); } : function (ts) { return new Date(ts * 1000); };
  }

  function axisX(t) {
    return {
      stroke: t.muted, grid: { stroke: t.rule, width: 1 }, ticks: { stroke: t.rule, width: 1, size: 4 }, font: t.font, size: 30,
      values: function (u, vals) { return vals.map(function (v) { return F.clock(v * 1000).replace("Z", ""); }); }
    };
  }

  function axisY(t, label, fmt, size) {
    return {
      stroke: t.muted, grid: { stroke: t.rule, width: 1 }, ticks: { show: false }, font: t.font, size: size || Y_AXIS - 16,
      label: label, labelFont: t.font, labelSize: 16, labelGap: 2,
      values: fmt ? function (u, vals) { return vals.map(fmt); } : undefined
    };
  }

  function msFmt(v) {
    if (v === null || v === undefined) return "";
    if (v >= 1000) return F.num(v / 1000, 1) + " s";
    if (v >= 1) return F.num(v, v >= 100 ? 0 : 1) + " ms";
    return F.num(v * 1000, 0) + " µs";
  }

  function compactFmt(v) {
    if (v === null || v === undefined) return "";
    var a = Math.abs(v);
    if (a >= 1e6) return F.num(v / 1e6, 1) + "M";
    if (a >= 1e3) return F.num(v / 1e3, a >= 1e4 ? 0 : 1) + "k";
    return F.num(v, 0);
  }

  function toMs(us) { return us ? us / 1000 : null; }

  /* ---- plugins ---- */

  function shadingPlugin(ctx, showLabels) {
    return {
      hooks: {
        drawClear: [function (u) {
          var c = u.ctx;
          var t = ctx.theme;
          var top = u.bbox.top, h = u.bbox.height;
          c.save();
          (ctx.phaseSpans || []).forEach(function (span) {
            var x0 = u.valToPos(span.start, "x", true), x1 = u.valToPos(span.end, "x", true);
            if (x1 < u.bbox.left || x0 > u.bbox.left + u.bbox.width) return;
            c.fillStyle = t.shade;
            c.fillRect(x0, top, Math.max(1, x1 - x0), h);
            if (showLabels) {
              c.fillStyle = t.muted;
              c.font = t.font;
              c.textAlign = "left";
              c.textBaseline = "alphabetic";
              c.fillText(span.label, Math.max(x0, u.bbox.left) + 4 * devicePixelRatio, top + h - 6 * devicePixelRatio);
            }
          });
          (ctx.windows || []).forEach(function (w) {
            var x0 = u.valToPos(w.start_ms / 1000, "x", true), x1 = u.valToPos(w.end_ms / 1000, "x", true);
            c.fillStyle = t.window;
            c.fillRect(x0, top, Math.max(2, x1 - x0), h);
          });
          c.restore();
        }],
        draw: [function (u) {
          var c = u.ctx;
          var t = ctx.theme;
          c.save();
          c.setLineDash([4 * devicePixelRatio, 3 * devicePixelRatio]);
          c.lineWidth = devicePixelRatio;
          (ctx.markers || []).forEach(function (m) {
            var x = Math.round(u.valToPos(m.t_ms / 1000, "x", true)) + 0.5;
            if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) return;
            c.strokeStyle = t.accent;
            c.beginPath();
            c.moveTo(x, u.bbox.top);
            c.lineTo(x, u.bbox.top + u.bbox.height);
            c.stroke();
          });
          c.restore();
        }]
      }
    };
  }

  function readoutPlugin(rows) {
    var box;
    return {
      hooks: {
        init: [function (u) {
          box = el("div", "chart-readout");
          box.style.display = "none";
          u.over.appendChild(box);
          u.over.addEventListener("mouseleave", function () { box.style.display = "none"; });
        }],
        setCursor: [function (u) {
          var idx = u.cursor.idx;
          if (idx === null || idx === undefined || u.cursor.left < 0 || !u.cursor.event) { box.style.display = "none"; return; }
          var ts = u.data[0][idx];
          box.textContent = "";
          box.appendChild(el("div", "rt", F.clock(ts * 1000)));
          rows.forEach(function (r) {
            var v = u.data[r.i][idx];
            var line = el("div", "rr");
            line.appendChild(el("i", "", r.label));
            line.appendChild(el("span", "", v === null || v === undefined ? "–" : r.fmt(v)));
            box.appendChild(line);
          });
          box.style.display = "block";
          var left = u.cursor.left + 14;
          if (left + box.offsetWidth > u.over.clientWidth) left = u.cursor.left - box.offsetWidth - 14;
          box.style.left = left + "px";
          box.style.top = Math.max(0, u.cursor.top - 20) + "px";
        }]
      }
    };
  }

  function lanePlugin(ctx) {
    return {
      hooks: {
        draw: [function (u) {
          var c = u.ctx, t = ctx.theme, dpr = devicePixelRatio;
          var rowH = u.bbox.height / LANE_ROWS.length;
          var left = u.bbox.left, right = u.bbox.left + u.bbox.width, top = u.bbox.top;
          c.save();
          c.font = t.font;
          c.textBaseline = "middle";
          for (var r = 0; r < LANE_ROWS.length; r++) {
            var y = top + r * rowH;
            c.fillStyle = t.muted;
            c.textAlign = "right";
            c.fillText(LANE_ROWS[r], left - 8 * dpr, y + rowH / 2);
            c.strokeStyle = t.rule;
            c.lineWidth = dpr;
            c.beginPath();
            c.moveTo(left, y + rowH + 0.5);
            c.lineTo(right, y + rowH + 0.5);
            c.stroke();
          }
          c.textAlign = "left";
          function xOf(ms) { return u.valToPos(ms / 1000, "x", true); }
          function inView(x) { return x >= left - 1 && x <= right + 1; }
          (ctx.markers || []).forEach(function (m) {
            var x = xOf(m.t_ms);
            if (!inView(x)) return;
            var y = top + rowH / 2;
            c.fillStyle = t.accent;
            c.beginPath();
            c.moveTo(x - 5 * dpr, y - 6 * dpr);
            c.lineTo(x + 5 * dpr, y - 6 * dpr);
            c.lineTo(x, y + 4 * dpr);
            c.closePath();
            c.fill();
            c.fillStyle = t.ink;
            c.fillText(m.label, x + 7 * dpr, y);
          });
          (ctx.events || []).forEach(function (e) {
            var row = e.kind === "pool_cleared" ? 3 : (e.kind === "error_sample" ? 2 : 1);
            if (["phase", "load_done", "paused", "resumed", "level_changed", "stop_requested", "sampling_unavailable", "heartbeat_recovered", "topology_changed"].indexOf(e.kind) >= 0) return;
            var x = xOf(e.t_ms);
            if (!inView(x)) return;
            var y = top + row * rowH + rowH / 2;
            var tone = F.eventTone(e.kind);
            c.fillStyle = tone === "fail" ? t.fail : tone === "warn" ? t.warn : t.muted;
            c.beginPath();
            var s = (row === 2 ? 3 : 5) * dpr;
            c.moveTo(x, y - s); c.lineTo(x + s, y); c.lineTo(x, y + s); c.lineTo(x - s, y); c.closePath();
            if (e.kind === "pool_cleared") { c.strokeStyle = c.fillStyle; c.lineWidth = 1.5 * dpr; c.stroke(); } else { c.fill(); }
          });
          var pts = ctx.points || [];
          var maxErr = 0;
          pts.forEach(function (p) { if (p.err > maxErr) maxErr = p.err; });
          if (maxErr > 0) {
            var step = (ctx.step || 1);
            pts.forEach(function (p) {
              if (!p.err) return;
              var x0 = u.valToPos(p.t, "x", true), x1 = u.valToPos(p.t + step, "x", true);
              if (x1 < left || x0 > right) return;
              var hh = Math.max(2 * dpr, (rowH - 4 * dpr) * Math.sqrt(p.err / maxErr));
              c.fillStyle = t.fail;
              c.fillRect(x0, top + 3 * rowH - 2 * dpr - hh, Math.max(dpr, x1 - x0 - dpr), hh);
            });
          }
          (ctx.gaps || []).forEach(function (g) {
            var x0 = xOf(g.start_ms), x1 = xOf(g.end_ms);
            if (x1 < left || x0 > right) return;
            var y = top + 3 * rowH + (g.kind === "write" ? 6 : 13) * dpr;
            c.strokeStyle = g.kind === "write" ? t.c3 : t.c1;
            c.lineWidth = 3 * dpr;
            c.beginPath();
            c.moveTo(Math.max(x0, left), y);
            c.lineTo(Math.min(Math.max(x1, x0 + 2 * dpr), right), y);
            c.stroke();
          });
          c.restore();
        }]
      }
    };
  }

  /* ---- data ---- */

  function phaseSpans(points, step) {
    var spans = [];
    var cur = null;
    (points || []).forEach(function (p) {
      var shaded = p.phase === "warmup" || p.phase === "load" || p.phase === "paused";
      if (shaded) {
        var label = p.phase === "warmup" ? "warm-up" : p.phase === "load" ? "data load" : "paused";
        if (cur && cur.label === label && p.t <= cur.end + step) { cur.end = p.t + step; return; }
        cur = { label: label, start: p.t, end: p.t + step };
        spans.push(cur);
      } else {
        cur = null;
      }
    });
    return spans;
  }

  function buildData(points) {
    var x = [], ok = [], err = [], target = [], p50 = [], p99 = [], max = [], st99 = [];
    points.forEach(function (p) {
      x.push(p.t);
      ok.push(p.ok);
      err.push(p.err || 0);
      target.push(p.target === null || p.target === undefined ? null : p.target);
      p50.push(toMs(p.p50));
      p99.push(toMs(p.p99));
      max.push(toMs(p.max));
      st99.push(toMs(p.st99));
    });
    return { tput: [x, ok, err, target], lat: [x, p50, p99, max, st99], lane: [x, x.map(function () { return null; })] };
  }

  /* ---- run charts ---- */

  function runCharts(container, opts) {
    var t = theme();
    var key = "ballast-" + (++syncCount);
    var sync = uPlot.sync(key);
    var ctx = { theme: t, markers: opts.markers || [], events: opts.events || [], gaps: opts.gaps || [], windows: opts.windows || [], points: opts.points || [], step: opts.step || 1, phaseSpans: [] };
    ctx.phaseSpans = phaseSpans(ctx.points, ctx.step);
    var data = buildData(ctx.points);
    var charts = [];
    var zoomed = null;
    var applying = false;
    var follow = opts.live ? true : false;
    var windowSeconds = opts.windowSeconds || 0;
    container.textContent = "";

    function block(title, legend) {
      var b = el("div", "chart-block");
      var head = el("div", "chart-title");
      head.appendChild(el("strong", "", title));
      var lg = el("div", "legend");
      legend.forEach(function (item) {
        var k = el("span", "key");
        var sw = el("span", "swatch " + (item.style || ""));
        sw.style.color = item.color;
        k.appendChild(sw);
        k.appendChild(document.createTextNode(item.label));
        lg.appendChild(k);
      });
      head.appendChild(lg);
      b.appendChild(head);
      var host = el("div", "chart-host");
      b.appendChild(host);
      container.appendChild(b);
      return host;
    }

    function width() { return Math.max(320, container.clientWidth - 16); }

    function common(height, plugins) {
      return {
        width: width(), height: height, tzDate: tzDate(), legend: { show: false }, plugins: plugins,
        cursor: { sync: { key: key, setSeries: false }, drag: { x: true, y: false, setScale: true }, points: { size: 6 }, focus: { prox: 16 } },
        scales: { x: { time: true } },
        hooks: {
          setScale: [function (u, scaleKey) {
            if (scaleKey !== "x" || applying) return;
            var min = u.scales.x.min, max = u.scales.x.max;
            applying = true;
            charts.forEach(function (other) { if (other !== u) other.setScale("x", { min: min, max: max }); });
            applying = false;
            if (u.cursor.drag && u.select && u.select.width > 0) {
              zoomed = { min: min, max: max };
              follow = false;
              if (opts.onZoom) opts.onZoom(zoomed);
            }
          }]
        }
      };
    }

    var tputHost = block("Throughput", [
      { label: "successful ops/s", color: t.c1 },
      { label: "failed ops/s", color: t.c3, style: "bar" },
      { label: "target", color: t.c6, style: "dashed" }
    ]);
    var tput = new uPlot(Object.assign(common(opts.height || 190, [shadingPlugin(ctx, true), readoutPlugin([
      { i: 1, label: "ok/s", fmt: function (v) { return F.num(v, 0); } },
      { i: 2, label: "failed/s", fmt: function (v) { return F.num(v, 0); } },
      { i: 3, label: "target", fmt: function (v) { return F.num(v, 0); } }
    ])]), {
      series: [
        {},
        { label: "ok/s", stroke: t.c1, width: 1.5, points: { show: false } },
        { label: "failed/s", stroke: t.c3, fill: t.c3, width: 0, paths: uPlot.paths.bars({ size: [1, 6], align: 1 }), points: { show: false } },
        { label: "target", stroke: t.c6, width: 1, dash: [5, 4], points: { show: false } }
      ],
      axes: [axisX(t), axisY(t, "ops/s", compactFmt)],
      scales: { x: { time: true }, y: { range: function (u, min, max) { return [0, Math.max(10, max * 1.08)]; } } }
    }), data.tput, tputHost);
    sync.sub(tput);
    charts.push(tput);

    var latHost = block("Response time (log scale)", [
      { label: "p50", color: t.c1 }, { label: "p99", color: t.c2 }, { label: "max", color: t.c3, style: "dashed" }, { label: "service time p99", color: t.c4, style: "dotted" }
    ]);
    var latChart = new uPlot(Object.assign(common(opts.height || 190, [shadingPlugin(ctx, false), readoutPlugin([
      { i: 1, label: "p50", fmt: msFmt }, { i: 2, label: "p99", fmt: msFmt }, { i: 3, label: "max", fmt: msFmt }, { i: 4, label: "service p99", fmt: msFmt }
    ])]), {
      series: [
        {},
        { label: "p50", stroke: t.c1, width: 1.5, points: { show: false } },
        { label: "p99", stroke: t.c2, width: 1.5, points: { show: false } },
        { label: "max", stroke: t.c3, width: 1, dash: [5, 4], points: { show: false } },
        { label: "service p99", stroke: t.c4, width: 1.5, dash: [2, 3], points: { show: false } }
      ],
      axes: [axisX(t), axisY(t, "ms", msFmt)],
      scales: { x: { time: true }, y: { distr: 3, log: 10 } }
    }), data.lat, latHost);
    sync.sub(latChart);
    charts.push(latChart);

    var laneHost = block("Timeline", [
      { label: "marker", color: t.accent, style: "bar" }, { label: "topology event", color: t.warn, style: "bar" },
      { label: "errors", color: t.fail, style: "bar" }, { label: "write gap", color: t.c3 }, { label: "read gap", color: t.c1 }
    ]);
    var lane = new uPlot(Object.assign(common(LANE_ROWS.length * LANE_ROW_H + 44, [lanePlugin(ctx)]), {
      series: [{}, { label: "", stroke: "transparent", points: { show: false } }],
      axes: [axisX(t), { show: true, size: Y_AXIS, stroke: "transparent", grid: { show: false }, ticks: { show: false }, values: function () { return []; } }],
      scales: { x: { time: true }, y: { range: [0, 1] } }
    }), data.lane, laneHost);
    sync.sub(lane);
    charts.push(lane);

    function visibleRange() {
      var xs = data.tput[0];
      if (!xs.length) return null;
      var max = xs[xs.length - 1] + ctx.step;
      var min = xs[0];
      if (windowSeconds > 0) min = Math.max(min, max - windowSeconds);
      return { min: min, max: max };
    }

    function applyRange() {
      var r = zoomed && !follow ? zoomed : visibleRange();
      if (!r) return;
      applying = true;
      charts.forEach(function (c) { c.setScale("x", r); });
      applying = false;
    }

    function setData(points, extras) {
      ctx.points = points;
      if (extras) {
        if (extras.markers) ctx.markers = extras.markers;
        if (extras.events) ctx.events = extras.events;
        if (extras.gaps) ctx.gaps = extras.gaps;
        if (extras.windows) ctx.windows = extras.windows;
        if (extras.step) ctx.step = extras.step;
      }
      ctx.phaseSpans = phaseSpans(points, ctx.step);
      data = buildData(points);
      tput.setData(data.tput, false);
      latChart.setData(data.lat, false);
      lane.setData(data.lane, false);
      applyRange();
      charts.forEach(function (c) { c.redraw(false, true); });
    }

    var ro = new ResizeObserver(function () {
      var w = width();
      charts.forEach(function (c) { c.setSize({ width: w, height: c.height }); });
    });
    ro.observe(container);
    container.addEventListener("dblclick", function () { zoomed = null; follow = !!opts.live; applyRange(); if (opts.onZoom) opts.onZoom(null); });
    applyRange();

    return {
      setData: setData,
      setWindow: function (seconds) { windowSeconds = seconds; zoomed = null; follow = !!opts.live; applyRange(); },
      resetZoom: function () { zoomed = null; follow = !!opts.live; applyRange(); },
      range: function () { var s = tput.scales.x; return { min: s.min, max: s.max }; },
      destroy: function () { ro.disconnect(); charts.forEach(function (c) { sync.unsub(c); c.destroy(); }); container.textContent = ""; },
      charts: charts
    };
  }

  /* ---- compare ---- */

  var DASHES = [[], [6, 4], [2, 3], [8, 3, 2, 3], [1, 2]];

  function compareChart(container, runs, metric) {
    var t = theme();
    var colors = [t.c1, t.c2, t.c3, t.c4, t.c5];
    container.textContent = "";
    var xsSet = {};
    runs.forEach(function (r) { r.points.forEach(function (p) { xsSet[p.t - r.offset_s] = true; }); });
    var xs = Object.keys(xsSet).map(Number).sort(function (a, b) { return a - b; });
    var index = {};
    xs.forEach(function (x, i) { index[x] = i; });
    var data = [xs];
    runs.forEach(function (r) {
      var ys = xs.map(function () { return null; });
      r.points.forEach(function (p) {
        var v = metric === "throughput" ? p.ok : toMs(p.p99);
        ys[index[p.t - r.offset_s]] = v;
      });
      data.push(ys);
    });
    var series = [{}].concat(runs.map(function (r, i) {
      return { label: r.name, stroke: colors[i % colors.length], width: 1.5, dash: DASHES[i % DASHES.length], points: { show: false }, spanGaps: false };
    }));
    var fmt = metric === "throughput" ? compactFmt : msFmt;
    var u = new uPlot({
      width: Math.max(320, container.clientWidth - 16), height: 240, legend: { show: false },
      cursor: { drag: { x: true, setScale: true }, points: { size: 6 } },
      plugins: [readoutPluginRel(runs.map(function (r, i) { return { i: i + 1, label: r.name, fmt: fmt }; }))],
      scales: { x: { time: false }, y: metric === "throughput" ? { range: function (uu, min, max) { return [0, Math.max(10, max * 1.08)]; } } : { distr: 3, log: 10 } },
      series: series,
      axes: [
        Object.assign(axisX(t), { values: function (uu, vals) { return vals.map(function (v) { return (v < 0 ? "−" : "+") + F.duration(Math.abs(v) * 1000); }); } }),
        axisY(t, metric === "throughput" ? "ops/s" : "p99 ms", fmt)
      ]
    }, data, container);
    var ro = new ResizeObserver(function () { u.setSize({ width: Math.max(320, container.clientWidth - 16), height: 240 }); });
    ro.observe(container);
    container.addEventListener("dblclick", function () { u.setScale("x", { min: xs[0], max: xs[xs.length - 1] }); });
    return { destroy: function () { ro.disconnect(); u.destroy(); }, colors: colors, dashes: DASHES };
  }

  function readoutPluginRel(rows) {
    var p = readoutPlugin(rows);
    p.hooks.setCursor = [function (u) {
      var box = u.over.querySelector(".chart-readout");
      var idx = u.cursor.idx;
      if (idx === null || idx === undefined || !u.cursor.event) { box.style.display = "none"; return; }
      var x = u.data[0][idx];
      box.textContent = "";
      box.appendChild(el("div", "rt", (x < 0 ? "−" : "+") + F.duration(Math.abs(x) * 1000) + " from alignment"));
      rows.forEach(function (r) {
        var v = u.data[r.i][idx];
        var line = el("div", "rr");
        line.appendChild(el("i", "", r.label));
        line.appendChild(el("span", "", v === null || v === undefined ? "–" : r.fmt(v)));
        box.appendChild(line);
      });
      box.style.display = "block";
      var left = u.cursor.left + 14;
      if (left + box.offsetWidth > u.over.clientWidth) left = u.cursor.left - box.offsetWidth - 14;
      box.style.left = left + "px";
      box.style.top = Math.max(0, u.cursor.top - 20) + "px";
    }];
    return p;
  }

  function pointsTable(points) {
    var wrap = el("div", "chart-table");
    var table = el("table", "data");
    var thead = el("thead");
    var hr = el("tr");
    ["Time", "Phase", "ok/s", "failed/s", "p50", "p99", "max", "service p99"].forEach(function (h, i) {
      var th = el("th", i > 1 ? "num" : "", h);
      th.scope = "col";
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tb = el("tbody");
    points.slice(-1200).forEach(function (p) {
      var tr = el("tr");
      [F.clock(p.t * 1000), p.phase, F.num(p.ok, 0), F.num(p.err, 0), F.lat(p.p50), F.lat(p.p99), F.lat(p.max), F.lat(p.st99)].forEach(function (v, i) {
        tr.appendChild(el("td", i > 1 ? "num" : "", v));
      });
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    wrap.appendChild(table);
    return wrap;
  }

  window.BallastCharts = { runCharts: runCharts, compareChart: compareChart, pointsTable: pointsTable, theme: theme, msFmt: msFmt };
})();
