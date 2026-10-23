/* Run report renderer used by the app's report page and the offline HTML export.
   Classic script. Defines window.BallastReport. */
(function () {
  "use strict";
  var F = window.BallastFormat;

  function h(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? "" : v);
      });
    }
    (Array.isArray(children) ? children : children === undefined ? [] : [children]).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      n.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return n;
  }

  function panel(title, meta, body, extraClass) {
    return h("section", { class: "panel " + (extraClass || "") }, [
      h("header", null, [h("h2", { text: title }), meta ? h("span", { class: "meta" }, meta) : null]),
      body
    ]);
  }

  function figure(label, value, unit, sub, cls) {
    return h("div", { class: "figure " + (cls || "") }, [
      h("div", { class: "fig-label", text: label }),
      h("div", { class: "fig-value" }, [value, unit ? h("span", { class: "unit", text: unit }) : null]),
      sub ? h("div", { class: "fig-sub", text: sub }) : null
    ]);
  }

  function latFigure(label, us, sub, cls) {
    var p = F.latParts(us);
    return figure(label, p[0], p[1], sub, cls);
  }

  function notice(tone, title, detail, fix) {
    return h("div", { class: "notice " + tone, role: tone === "fail" ? "alert" : null }, [
      h("div", null, [h("div", { class: "notice-title", text: title }), detail ? h("div", { class: "small", text: detail }) : null, fix ? h("div", { class: "small fix", text: fix }) : null])
    ]);
  }

  function table(headers, rows, opts) {
    var thead = h("thead", null, h("tr", null, headers.map(function (hd) {
      return h("th", { class: hd.num ? "num" : "", scope: "col", text: hd.label });
    })));
    var tbody = h("tbody", null, rows);
    return h("div", { class: "scroll-x" }, h("table", { class: "data" + (opts && opts.cls ? " " + opts.cls : "") }, [thead, tbody]));
  }

  function td(text, num, cls) {
    return h("td", { class: (num ? "num " : "") + (cls || ""), text: text });
  }

  function stats(s) {
    return s || {};
  }

  function gapText(g) {
    return g ? F.lat(g.ms * 1000) : "none";
  }

  function impactRow(imp, settings, expanded, toggle) {
    var before = stats(imp.before), during = stats(imp.during), after = stats(imp.after);
    var primaryCount = (imp.primary_changes || []).filter(function (p) { return p.kind !== "primary_lost"; }).length;
    var recovery = imp.recovered ? F.seconds(imp.recovery_seconds) : "not within run";
    var btn = h("button", { class: "btn quiet small", "aria-expanded": expanded ? "true" : "false", onclick: toggle, text: expanded ? "Hide" : "Details" });
    var row = h("tr", null, [
      h("td", { class: "wrap" }, [h("strong", { text: imp.label || "Window" }), h("div", { class: "xs muted", text: imp.source === "markers" ? "marker pair" : imp.source === "detected" ? (imp.overlaps_marker_window ? "detected, in marker window" : "detected") : "selected range" })]),
      td(F.clock(imp.start_ms, true) + " – " + F.clock(imp.end_ms, true), false),
      td(imp.first_failure_ms ? F.clock(imp.first_failure_ms, true) : "none", true),
      td(imp.last_failure_ms ? F.clock(imp.last_failure_ms, true) : "none", true),
      td(F.int(imp.failed_ops), true, imp.failed_ops ? "hot" : ""),
      td(gapText(imp.longest_write_gap), true),
      td(gapText(imp.longest_read_gap), true),
      td(String(primaryCount), true),
      td(F.lat(before.p99_us) + " / " + F.lat(during.p99_us) + " / " + F.lat(after.p99_us), true),
      td(recovery, true, imp.recovered ? "" : "hot"),
      h("td", null, btn)
    ]);
    return row;
  }

  function impactDetail(imp, settings) {
    var before = stats(imp.before), during = stats(imp.during), after = stats(imp.after);
    function cell(v, hot) { return h("td", { class: hot ? "hot" : "", text: v }); }
    var latTable = h("table", { class: "lat-compare" }, [
      h("thead", null, h("tr", null, [h("th", { text: "" }), h("th", { text: "Before" }), h("th", { text: "During" }), h("th", { text: "After" })])),
      h("tbody", null, [
        h("tr", null, [h("td", { text: "p50" }), cell(F.lat(before.p50_us)), cell(F.lat(during.p50_us), during.p50_us > (before.p50_us || 0) * 2), cell(F.lat(after.p50_us))]),
        h("tr", null, [h("td", { text: "p99" }), cell(F.lat(before.p99_us)), cell(F.lat(during.p99_us), during.p99_us > (before.p99_us || 0) * 2), cell(F.lat(after.p99_us))]),
        h("tr", null, [h("td", { text: "max" }), cell(F.lat(before.max_us)), cell(F.lat(during.max_us), during.max_us > (before.max_us || 0) * 2), cell(F.lat(after.max_us))]),
        h("tr", null, [h("td", { text: "ops/s" }), cell(F.num(before.throughput, 0)), cell(F.num(during.throughput, 0), during.throughput < (before.throughput || 0) * 0.8), cell(F.num(after.throughput, 0))]),
        h("tr", null, [h("td", { text: "seconds" }), cell(F.int(before.seconds)), cell(F.int(during.seconds)), cell(F.int(after.seconds))])
      ])
    ]);
    var classes = Object.keys(imp.failed_by_class || {});
    var failList = classes.length ? h("ul", { class: "bar-list" }, classes.map(function (c) {
      return h("li", null, [h("span", { class: "bar-name", text: c }), h("span", { class: "bar-n", text: F.int(imp.failed_by_class[c]) })]);
    })) : h("p", { class: "small muted", text: "No failed operations." });
    var prim = (imp.primary_changes || []).length ? h("ul", { class: "event-list" }, imp.primary_changes.map(function (p) {
      return h("li", null, [h("time", { text: F.clock(p.t_ms, true) }), h("span", { text: F.eventText({ kind: p.kind, data: p }) })]);
    })) : h("p", { class: "small muted", text: "No primary changes observed." });
    var gaps = h("table", { class: "kv" }, h("tbody", null, [
      h("tr", null, [h("th", { text: "Longest write gap" }), h("td", { class: "mono", text: imp.longest_write_gap ? F.lat(imp.longest_write_gap.ms * 1000) + "  (" + F.clock(imp.longest_write_gap.start_ms, true) + " – " + F.clock(imp.longest_write_gap.end_ms, true) + ")" : "none over the threshold" })]),
      h("tr", null, [h("th", { text: "Longest read gap" }), h("td", { class: "mono", text: imp.longest_read_gap ? F.lat(imp.longest_read_gap.ms * 1000) + "  (" + F.clock(imp.longest_read_gap.start_ms, true) + " – " + F.clock(imp.longest_read_gap.end_ms, true) + ")" : "none over the threshold" })]),
      h("tr", null, [h("th", { text: "Analysis span" }), h("td", { class: "mono", text: F.clock(imp.start_ms) + " – " + F.clock(imp.span_end_ms) })]),
      h("tr", null, [h("th", { text: "Recovery rule" }), h("td", { class: "small", text: "Throughput within " + F.pct(settings.throughput_tolerance, 0) + " and p99 within " + F.pct(settings.latency_tolerance, 0) + " of the " + settings.baseline_seconds + " s baseline, sustained for " + settings.sustain_seconds + " s." })])
    ]));
    return h("tr", null, h("td", { colspan: "11", style: null }, h("div", { class: "impact-detail" }, [
      h("div", null, [h("h3", { text: "Latency and throughput" }), latTable]),
      h("div", null, [h("h3", { text: "Failed operations by class" }), failList, h("h3", { text: "Gaps", style: null }), gaps]),
      h("div", null, [h("h3", { text: "Primary changes" }), prim])
    ])));
  }

  function impactsPanel(analysis, opts) {
    var impacts = (analysis && analysis.impacts) || [];
    var settings = (analysis && analysis.settings) || {};
    if (!impacts.length) {
      return panel("Impact analysis", null, h("div", { class: "body" }, [
        h("p", { text: "No impact windows in this run." }),
        h("p", { class: "small muted", text: "Windows come from marker pairs (for example \"resize started\" then \"resize finished\") and from disruptions detected automatically: failed operations, throughput below half the median, or p99 above four times the median." })
      ]));
    }
    var expanded = {};
    var tbody = h("tbody");
    function render() {
      tbody.textContent = "";
      impacts.forEach(function (imp, i) {
        tbody.appendChild(impactRow(imp, settings, expanded[i], function () { expanded[i] = !expanded[i]; render(); }));
        if (expanded[i]) tbody.appendChild(impactDetail(imp, settings));
      });
    }
    render();
    var headers = ["Window", "Start – end", "First failure", "Last failure", "Failed ops", "Write gap", "Read gap", "Primary changes", "p99 before / during / after", "Recovery", ""];
    var t = h("table", { class: "data" }, [h("thead", null, h("tr", null, headers.map(function (x, i) { return h("th", { class: i >= 2 && i <= 9 ? "num" : "", scope: "col", text: x }); }))), tbody]);
    return panel("Impact analysis", impacts.length + " window" + (impacts.length === 1 ? "" : "s"), h("div", { class: "body flush scroll-x" }, t), "impacts");
  }

  function errorsPanel(summary) {
    var errors = (summary && summary.errors) || {};
    var labels = (summary && summary.labels) || {};
    var total = Object.keys(errors).reduce(function (a, k) { return a + errors[k]; }, 0);
    if (!total) return panel("Errors by class", null, h("div", { class: "body" }, h("p", { class: "muted", text: "No failed operations outside warm-up." })));
    var rows = Object.keys(errors).map(function (k) { return h("tr", null, [td(k), td(F.int(errors[k]), true), td(F.pct(errors[k] / total, 1), true)]); });
    var labelRows = Object.keys(labels).map(function (k) { return h("tr", null, [td(k), td(F.int(labels[k]), true)]); });
    return panel("Errors by class", F.int(total) + " failed", h("div", { class: "body flush" }, [
      table([{ label: "Class" }, { label: "Count", num: true }, { label: "Share", num: true }], rows),
      labelRows.length ? h("div", { class: "body" }, [h("h3", { text: "Error labels" }), table([{ label: "Label" }, { label: "Count", num: true }], labelRows)]) : null
    ]));
  }

  function opsPanel(summary) {
    var ops = (summary && summary.operations) || {};
    var secs = (summary && summary.measured_seconds) || 1;
    var rows = Object.keys(ops).map(function (k) {
      var o = ops[k], rt = o.response_time || {}, st = o.service_time || {};
      return h("tr", null, [td(F.opName(k)), td(F.int(o.ok), true), td(F.int(o.err), true), td(F.num(o.ok / secs, 1), true), td(F.lat(rt.p50_us), true), td(F.lat(rt.p99_us), true), td(F.lat(rt.p999_us), true), td(F.lat(rt.max_us), true), td(F.lat(st.p99_us), true)]);
    });
    if (!rows.length) return panel("Operations", null, h("div", { class: "body muted", text: "No operations recorded outside warm-up." }));
    return panel("Operations", "response time from intended start; service time from send", h("div", { class: "body flush" }, table([
      { label: "Operation" }, { label: "Successful", num: true }, { label: "Failed", num: true }, { label: "ops/s", num: true }, { label: "p50", num: true },
      { label: "p99", num: true }, { label: "p99.9", num: true }, { label: "max", num: true }, { label: "Service p99", num: true }
    ], rows)));
  }

  function eventsPanel(markers, events) {
    var items = [];
    (markers || []).forEach(function (m) { items.push({ t: m.t_ms, cls: "marker", text: "Marker: " + m.label + (m.note ? " — " + m.note : "") + (m.source ? " (" + m.source + ")" : "") }); });
    (events || []).forEach(function (e) {
      if (e.kind === "error_sample" && items.length > 400) return;
      items.push({ t: e.t_ms, cls: F.eventTone(e.kind), text: F.eventText(e) });
    });
    items.sort(function (a, b) { return a.t - b.t; });
    if (!items.length) return panel("Markers and events", null, h("div", { class: "body muted", text: "No markers or driver events were recorded." }));
    return panel("Markers and events", items.length + " entries", h("div", { class: "body flush" }, h("ul", { class: "event-list" }, items.map(function (it) {
      return h("li", null, [h("time", { text: F.clock(it.t, true) }), h("span", { class: "ek " + it.cls, text: it.text })]);
    }))));
  }

  function kvRows(pairs) {
    return h("table", { class: "kv" }, h("tbody", null, pairs.filter(function (p) { return p[1] !== undefined; }).map(function (p) {
      return h("tr", null, [h("th", { text: p[0] }), h("td", { class: p[2] ? "mono" : "", text: p[1] === null ? "–" : String(p[1]) })]);
    })));
  }

  function snapshotPanel(run) {
    var s = run.snapshot || {};
    var server = s.server || {}, host = s.generator_host || {}, app = s.app || {}, driver = s.driver || {};
    var members = (server.members || []).map(function (m) { return m.address + " (" + m.state + ")"; }).join(", ");
    var body = h("div", { class: "body" }, [
      h("div", { class: "grid-2" }, [
        kvRows([
          ["Target", s.target ? s.target.name : run.target_name], ["Connection", s.target ? s.target.uri : null, true], ["Database", run.database_name, true],
          ["Server version", server.version, true], ["Topology", server.topology], ["Replica set", server.set_name, true], ["Primary at start", server.primary, true], ["Members", members, true]
        ]),
        kvRows([
          ["Product", (app.name || "") + " " + (app.version || ""), true], ["Build", (app.commit || "") + ", " + (app.built || ""), true], ["Driver", (driver.name || "") + " " + (driver.version || ""), true],
          ["Generator host", host.hostname, true], ["CPUs", host.cpu_count, true], ["Memory", host.memory_bytes ? F.bytes(host.memory_bytes) : null, true], ["Platform", host.platform, true],
          ["Workload", s.profile_summary]
        ])
      ]),
      h("details", { class: "disclosure" }, [h("summary", { text: "Effective driver options" }), h("div", { class: "body" }, h("pre", { class: "json", text: JSON.stringify(s.effective_driver_options || {}, null, 2) }))]),
      h("details", { class: "disclosure" }, [h("summary", { text: "Full workload profile" }), h("div", { class: "body" }, h("pre", { class: "json", text: JSON.stringify(run.profile || {}, null, 2) }))])
    ]);
    return panel("Configuration snapshot", "everything needed to reproduce this run", body);
  }

  function render(root, payload, opts) {
    opts = opts || {};
    root.textContent = "";
    var run = payload.run, analysis = payload.analysis || {}, summary = analysis.summary || run.summary || {};
    var rt = summary.response_time || {}, st = summary.service_time || {};
    var duration = run.started_at && run.ended_at ? run.ended_at - run.started_at : null;
    if (opts.standalone) {
      root.appendChild(h("div", { class: "page-head" }, [h("div", null, [
        h("h1", { text: run.name }),
        h("div", { class: "sub" }, [h("span", { class: "chip " + run.status, text: F.statusText(run.status) }), " ", F.full(run.started_at || run.created_at), " – ", run.ended_at ? F.clock(run.ended_at) : "now", duration ? " (" + F.duration(duration) + ")" : "", ", target ", run.target_name || "", ", database ", run.database_name || ""])
      ])]));
    }
    if (run.error) root.appendChild(notice("fail", run.error.title || "Run failed", run.error.detail, run.error.fix));
    if (run.status === "interrupted") root.appendChild(notice("warn", "This run was interrupted", "The application stopped while the run was active. Everything recorded up to that point is kept and analysed.", null));
    var cb = analysis.client_bound;
    if (cb && cb.client_bound) {
      root.appendChild(notice("warn", "Client-bound: the load generator limited this run", cb.reasons.map(function (r) { return r.detail + " (" + F.clock(r.start * 1000) + " – " + F.clock((r.end + 1) * 1000) + ")"; }).join("; "),
        "Increase generator processes, raise maxPoolSize, or run from a larger host. Treat throughput and latency in these periods as limits of the client."));
    }
    (payload.events || []).filter(function (e) { return e.kind === "sampling_unavailable"; }).slice(0, 1).forEach(function (e) {
      root.appendChild(notice("", "Server sampling was unavailable", e.data.reason, e.data.fix));
    });

    var errRate = summary.error_rate || 0;
    root.appendChild(h("div", { class: "figures", "aria-label": "Summary" }, [
      figure("Operations", F.int((summary.ok || 0) + (summary.err || 0)), "", "measured " + F.seconds(summary.measured_seconds || 0)),
      figure("Throughput", F.num(summary.throughput || 0, 0), "ops/s", "successful, mean"),
      latFigure("p50", rt.p50_us, "response time"),
      latFigure("p99", rt.p99_us, "response time"),
      latFigure("p99.9", rt.p999_us, "response time"),
      latFigure("max", rt.max_us, "response time"),
      latFigure("Service p99", st.p99_us, "time on the wire"),
      figure("Errors", F.pct(errRate, errRate && errRate < 0.0001 ? 4 : 2), "", F.int(summary.err || 0) + " failed", errRate > 0 ? "bad" : "")
    ]));
    root.appendChild(h("p", { class: "xs muted", style: null, text: "Summary excludes warm-up, data load, pauses and the stop drain. Latency percentiles come from merged HDR-style histograms (precision 0.1%)." }));

    var chartHost = h("div", { class: "body flush" });
    var tableHost = h("div");
    var tableBtn = h("button", { class: "btn small", "aria-pressed": "false", text: "View as table" });
    var toolbar = h("div", { class: "chart-toolbar" }, [
      h("span", { class: "hint", text: "Drag across a chart to zoom. Double-click to reset." }), h("span", { class: "spacer" }),
      opts.onAnalyzeRange ? h("button", { class: "btn small", id: "analyze-range", text: "Analyze visible range", onclick: function () { opts.onAnalyzeRange(charts.range()); } }) : null,
      tableBtn
    ]);
    var chartsPanel = h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Throughput, latency and timeline" }), h("span", { class: "meta", text: payload.series && payload.series.step > 1 ? "each point is " + payload.series.step + " s (histograms merged)" : "1 s resolution" })]), toolbar, chartHost, tableHost]);
    root.appendChild(chartsPanel);
    var windows = (analysis.impacts || []).filter(function (i) { return i.source === "markers"; });
    var charts = window.BallastCharts.runCharts(chartHost, {
      points: (payload.series && payload.series.points) || [], step: (payload.series && payload.series.step) || 1,
      markers: payload.markers, events: payload.events, gaps: payload.gaps, windows: windows
    });
    tableBtn.addEventListener("click", function () {
      var on = tableBtn.getAttribute("aria-pressed") !== "true";
      tableBtn.setAttribute("aria-pressed", on ? "true" : "false");
      tableBtn.textContent = on ? "Hide table" : "View as table";
      tableHost.textContent = "";
      if (on) tableHost.appendChild(window.BallastCharts.pointsTable((payload.series && payload.series.points) || []));
    });
    var impactsHost = h("div");
    impactsHost.appendChild(impactsPanel(analysis, opts));
    root.appendChild(impactsHost);
    root.appendChild(h("div", { class: "grid-2" }, [errorsPanel(summary), eventsPanel(payload.markers, payload.events)]));
    root.appendChild(opsPanel(summary));
    root.appendChild(snapshotPanel(run));
    return {
      charts: charts,
      addImpact: function (imp) {
        analysis.impacts = (analysis.impacts || []).concat([imp]);
        impactsHost.textContent = "";
        impactsHost.appendChild(impactsPanel(analysis, opts));
        impactsHost.scrollIntoView({ block: "start" });
      },
      destroy: function () { charts.destroy(); }
    };
  }

  window.BallastReport = { render: render, h: h, panel: panel, figure: figure, latFigure: latFigure, notice: notice, table: table, td: td };
})();
