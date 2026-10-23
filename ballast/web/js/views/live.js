import { api } from "../api.js";
import { app, navigate, setTitle } from "../app.js";
import { subscribe } from "../live.js";
import { h, F, chip, button, errorBlock, loading, toast, dialog, announce, segmented, input } from "../ui.js";

const LIVE = ["starting", "loading", "running", "paused", "stopping"];
const WINDOWS = [{ value: 300, label: "5 min" }, { value: 900, label: "15 min" }, { value: 3600, label: "1 h" }, { value: 0, label: "All" }];

export async function render(root, [runId]) {
  root.appendChild(loading("Connecting to the run"));
  let run;
  try {
    run = await api.get(`api/runs/${runId}`);
  } catch (err) {
    root.textContent = "";
    root.appendChild(errorBlock(err));
    return null;
  }
  if (!LIVE.includes(run.status)) { navigate(`#/runs/${runId}`); return null; }
  setTitle(`${run.name} (live)`);
  root.textContent = "";

  const profile = run.profile || {};
  const model = (profile.load || {}).model || "open";
  const state = {
    status: run.status, phase: (run.live && run.live.phase) || "", level: run.live ? run.live.level : null,
    elapsed: run.live ? run.live.elapsed_ms : 0, startedAt: run.started_at, points: [], markers: run.markers || [], events: run.events || [],
    gaps: run.gaps || [], errors: {}, last: null, load: null, samples: {}, windowSeconds: 900, samplingUnavailable: null,
    progress: run.live ? run.live.progress : null, progressAt: Date.now(), guardrails: run.live ? run.live.guardrails : null,
    cleanSeconds: 0,
  };
  try {
    const s = await api.get(`api/runs/${runId}/series?max_points=3600&start=${Math.floor(Date.now() / 1000) - 3600}`);
    state.points = s.points;
  } catch (e) { /* hello will fill it */ }
  state.points.forEach((p) => Object.entries(p.errors || {}).forEach(([k, v]) => { state.errors[k] = (state.errors[k] || 0) + v; }));
  state.last = state.points[state.points.length - 1] || null;
  state.samplingUnavailable = state.events.find((e) => e.kind === "sampling_unavailable") || null;

  /* ---- header ---- */
  const statusHost = h("span");
  const phaseHost = h("span", { class: "muted small" });
  const elapsedHost = h("span", { class: "mono muted small" });
  const figuresHost = h("div", { class: "figures" });
  const pauseBtn = h("button", { type: "button", class: "btn" });
  const stopBtn = h("button", { type: "button", class: "btn danger" }, ["Stop run ", h("kbd", { text: "S" })]);
  const markBtn = h("button", { type: "button", class: "btn primary", "aria-haspopup": "dialog" }, ["Mark ", h("kbd", { text: "M" })]);
  const notifyBtn = h("button", { type: "button", class: "btn", "aria-pressed": "false", title: "Desktop notification when operations start failing, a guardrail stops the run, or the run ends, while this tab is in the background" });
  const progressHost = h("div", { class: "progress-host" });
  const head = h("div", { class: "run-head" }, [
    h("div", { class: "title-row" }, [h("h1", { text: run.name }), statusHost, phaseHost, elapsedHost, h("div", { class: "controls" }, [markBtn, pauseBtn, stopBtn, notifyBtn])]),
    progressHost,
    figuresHost,
  ]);
  root.appendChild(head);
  const flagsHost = h("div");
  root.appendChild(flagsHost);

  /* ---- charts ---- */
  const chartHost = h("div", { class: "body flush" });
  let allTimer = null;
  async function loadAll() {
    try {
      const full = await api.get(`api/runs/${runId}/series?max_points=1500`);
      if (state.windowSeconds === 0) charts.setData(full.points, { step: full.step, markers: state.markers, events: state.events, gaps: state.gaps });
    } catch (e) { /* keep showing the live window */ }
  }
  const winSeg = segmented(WINDOWS, state.windowSeconds, (v) => {
    state.windowSeconds = Number(v);
    if (allTimer) { clearInterval(allTimer); allTimer = null; }
    if (state.windowSeconds === 0) {
      charts.setWindow(0);
      loadAll();
      allTimer = setInterval(loadAll, 30000);
    } else {
      charts.setData(state.points, { step: 1 });
      charts.setWindow(state.windowSeconds);
    }
  }, "Time window");
  const levelInput = input({ type: "number", min: "1", step: "1", class: "num-input", style: null, "aria-label": model === "open" ? "Target rate in operations per second" : "Workers" });
  levelInput.value = state.level ? String(Math.round(state.level)) : "";
  levelInput.addEventListener("input", () => { levelInput.dataset.touched = "1"; });
  const levelBtn = button("Apply", async () => {
    const v = Number(levelInput.value);
    if (!(v > 0)) { toast("Enter a number above 0", "fail"); return; }
    try {
      await api.post(`api/runs/${runId}/level`, { level: v });
      toast(model === "open" ? `Target rate set to ${F.num(v, 0)} ops/s` : `Workers set to ${F.num(v, 0)}`);
    } catch (err) { toast(err.title + (err.detail ? ": " + err.detail : ""), "fail"); }
  }, "small");
  const toolbar = h("div", { class: "chart-toolbar" }, [
    winSeg, h("span", { class: "hint", text: "Drag to zoom, double-click to follow live." }), h("span", { class: "spacer" }),
    h("label", { for: "level-input", class: "small", style: null, text: model === "open" ? "Target rate (ops/s)" : "Workers" }), h("div", { style: null }, levelInput), levelBtn,
  ]);
  levelInput.id = "level-input";
  levelInput.style.width = "110px";
  root.appendChild(h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Throughput, latency and timeline" }), h("span", { class: "meta", text: "1 s resolution, updated every second" })]), toolbar, chartHost]));
  const charts = window.BallastCharts.runCharts(chartHost, { points: state.points, markers: state.markers, events: state.events, gaps: state.gaps, live: true, windowSeconds: state.windowSeconds });

  /* ---- lower panels ---- */
  const errorsHost = h("div", { class: "body" });
  const eventsHost = h("div", { class: "body flush" });
  const clusterHost = h("div", { class: "body" });
  root.appendChild(h("div", { class: "grid-3" }, [
    h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Errors by class" }), h("span", { class: "meta", text: "since start" })]), errorsHost]),
    h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Recent markers and events" })]), eventsHost]),
    h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Cluster" }), h("span", { class: "meta", text: "from driver events and sampling" })]), clusterHost]),
  ]));

  function drawHeader() {
    statusHost.textContent = "";
    statusHost.appendChild(chip(state.status));
    phaseHost.textContent = state.status === "loading" ? "loading data" : state.phase || "";
    elapsedHost.textContent = state.elapsed ? F.duration(state.elapsed) : "";
    const last = state.last || {};
    const R = window.BallastReport;
    figuresHost.textContent = "";
    if (state.status === "loading" && state.load) {
      figuresHost.appendChild(R.figure("Documents loaded", F.int(state.load.done), "", `of ${F.int(state.load.target)}`));
    }
    figuresHost.appendChild(R.figure("Throughput", F.num(last.ok || 0, 0), "ops/s", model === "open" && last.target ? `target ${F.num(last.target, 0)}` : model));
    figuresHost.appendChild(R.latFigure("p50", last.p50, "last second"));
    figuresHost.appendChild(R.latFigure("p99", last.p99, "last second", last.p99 && state.baseP99 && last.p99 > state.baseP99 * 4 ? "warnish" : ""));
    figuresHost.appendChild(R.latFigure("max", last.max, "last second"));
    figuresHost.appendChild(R.figure("Errors", F.num(last.err || 0, 0), "/s", `${F.int(Object.values(state.errors).reduce((a, b) => a + b, 0))} total`, last.err ? "bad" : ""));
    if (model === "open") figuresHost.appendChild(R.figure("Backlog", F.int(last.backlog || 0), "ops", "behind schedule", last.backlog > 100 ? "warnish" : ""));
    figuresHost.appendChild(R.figure("Generator CPU", F.num(last.cpu || 0, 0), "%", "of one core", last.cpu > 90 ? "warnish" : ""));
    if (state.level && !levelInput.dataset.touched && document.activeElement !== levelInput) levelInput.value = String(Math.round(state.level));
    pauseBtn.textContent = "";
    pauseBtn.appendChild(document.createTextNode(state.status === "paused" ? "Resume " : "Pause "));
    pauseBtn.appendChild(h("kbd", { text: "P" }));
    pauseBtn.disabled = !["running", "paused"].includes(state.status);
    markBtn.disabled = !LIVE.includes(state.status);
    stopBtn.disabled = state.status === "stopping";
  }

  function remaining() {
    const pr = state.progress;
    if (!pr) return null;
    const drift = state.status === "running" ? (Date.now() - state.progressAt) / 1000 : 0;
    const phase = pr.phase_remaining_s === null ? null : Math.max(0, pr.phase_remaining_s - drift);
    const total = pr.total_remaining_s === null ? null : Math.max(0, pr.total_remaining_s - drift);
    return { pr, phase, total };
  }

  function drawProgress() {
    progressHost.textContent = "";
    const r = remaining();
    if (!r || state.status === "loading") return;
    const { pr, phase, total } = r;
    const name = (pr.phases[pr.index] || {}).name || state.phase;
    let fraction = null, text;
    if (pr.total_planned_s) {
      fraction = Math.min(1, Math.max(0, 1 - (total || 0) / pr.total_planned_s));
      text = `${name}: ${F.seconds(Math.round(phase || 0))} left in this phase, ${F.seconds(Math.round(total || 0))} left in the run`;
    } else if (phase !== null) {
      fraction = pr.phase_seconds ? Math.min(1, 1 - phase / pr.phase_seconds) : null;
      text = `${name}: ${F.seconds(Math.round(phase))} left in this phase, then steady until you stop it`;
    } else {
      text = `${name}: runs until you stop it`;
    }
    if (state.status === "paused") text += " (paused; the clock is stopped)";
    const bar = h("div", { class: "progress", role: "progressbar", "aria-label": "Run progress", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": fraction === null ? null : String(Math.round(fraction * 100)), "aria-valuetext": text });
    const fill = h("span", { class: "progress-fill" + (fraction === null ? " indeterminate" : "") });
    if (fraction !== null) fill.style.width = `${(fraction * 100).toFixed(1)}%`;
    bar.appendChild(fill);
    const ticks = h("div", { class: "progress-ticks", "aria-hidden": "true" });
    if (pr.total_planned_s) {
      let acc = 0;
      pr.phases.slice(0, -1).forEach((p) => { acc += p.seconds || 0; const t = h("span"); t.style.left = `${(acc / pr.total_planned_s) * 100}%`; ticks.appendChild(t); });
    }
    progressHost.appendChild(h("div", { class: "progress-wrap" }, [h("div", { class: "progress-track" }, [bar, ticks]), h("div", { class: "xs muted", text })]));
  }

  /* The tab title carries the live numbers, so the run can be watched from another tab. */
  function drawTitle() {
    const last = state.last || {};
    const mark = state.status === "paused" ? "❚❚" : last.err ? "▲" : "●";
    const product = (app.about && app.about.product) || "Ballast";
    document.title = `${mark} ${F.num(last.ok || 0, 0)} ops/s, p99 ${F.lat(last.p99)} – ${run.name} – ${product}`;
  }

  let notifyOn = false;
  try { notifyOn = localStorage.getItem("ballast.notify") === "1" && "Notification" in window && Notification.permission === "granted"; } catch (e) { notifyOn = false; }
  function drawNotify() {
    notifyBtn.textContent = notifyOn ? "Notifications on" : "Notify me";
    notifyBtn.setAttribute("aria-pressed", notifyOn ? "true" : "false");
    notifyBtn.hidden = !("Notification" in window);
  }
  notifyBtn.addEventListener("click", async () => {
    if (notifyOn) { notifyOn = false; } else {
      const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      notifyOn = perm === "granted";
      if (!notifyOn) toast("The browser did not allow notifications", "fail");
    }
    try { localStorage.setItem("ballast.notify", notifyOn ? "1" : "0"); } catch (e) { /* per-browser preference only */ }
    drawNotify();
  });
  function desktop(title, body) {
    if (!notifyOn || !document.hidden) return;
    try { new Notification(title, { body, tag: "ballast-" + runId }); } catch (e) { /* notifications are best effort */ }
  }

  function drawFlags() {
    flagsHost.textContent = "";
    if (state.guardrails) {
      const g = state.guardrails;
      const parts = [];
      if (g.max_error_percent !== null && g.max_error_percent !== undefined) parts.push(`more than ${F.num(g.max_error_percent, 2)}% of operations fail`);
      if (g.max_p99_ms !== null && g.max_p99_ms !== undefined) parts.push(`p99 is above ${F.num(g.max_p99_ms, 1)} ms`);
      flagsHost.appendChild(h("p", { class: "guard-note small", text: `Guardrail on: the run stops by itself if ${parts.join(" or ")} for ${g.sustain_seconds} seconds in a row.` }));
    }
    if (state.samplingUnavailable) {
      const d = state.samplingUnavailable.data || {};
      flagsHost.appendChild(window.BallastReport.notice("", "Server sampling is unavailable", d.reason, d.fix));
    }
    const recent = state.points.slice(-15);
    if (model === "open" && recent.length >= 10) {
      const short = recent.filter((p) => p.target && (p.ok + p.err) < p.target * 0.95 && !p.err).length;
      const hot = recent.filter((p) => p.cpu >= 90).length;
      if (short >= 10 || hot >= 10) {
        flagsHost.appendChild(window.BallastReport.notice("warn", "Client-bound: the generator may be limiting this run",
          hot >= 10 ? "Generator CPU has been at or above 90% of one core for 10 s." : "Achieved rate has been more than 5% below target for 10 s without errors.",
          "Add generator processes in the profile, raise maxPoolSize or concurrency, or run from a larger host."));
      }
    }
  }

  function drawErrors() {
    errorsHost.textContent = "";
    const entries = Object.entries(state.errors).sort((a, b) => b[1] - a[1]);
    if (!entries.length) { errorsHost.appendChild(h("p", { class: "muted small", text: "No failed operations so far." })); return; }
    const max = entries[0][1];
    errorsHost.appendChild(h("ul", { class: "bar-list" }, entries.map(([k, v]) => h("li", null, [
      h("span", { class: "bar-name", text: k }), h("span", { class: "bar-n", text: F.int(v) }),
      h("span", { class: "bar-track" }, h("span", { class: "bar-fill", style: null })),
    ]))));
    errorsHost.querySelectorAll(".bar-fill").forEach((n, i) => { n.style.width = `${Math.max(2, (entries[i][1] / max) * 100)}%`; });
  }

  function drawEvents() {
    eventsHost.textContent = "";
    const items = [];
    state.markers.forEach((m) => items.push({ t: m.t_ms, cls: "marker", text: `Marker: ${m.label}${m.note ? " — " + m.note : ""}` }));
    state.events.filter((e) => e.kind !== "error_sample" || true).forEach((e) => items.push({ t: e.t_ms, cls: F.eventTone(e.kind), text: F.eventText(e) }));
    items.sort((a, b) => b.t - a.t);
    if (!items.length) { eventsHost.appendChild(h("p", { class: "muted small body", style: null, text: "Nothing yet. Topology changes and pool events from the driver appear here." })); return; }
    eventsHost.appendChild(h("ul", { class: "event-list", tabindex: "0", "aria-label": "Events" }, items.slice(0, 60).map((it) => h("li", null, [h("time", { text: F.clock(it.t, true) }), h("span", { class: "ek " + it.cls, text: it.text })]))));
  }

  function drawCluster() {
    clusterHost.textContent = "";
    const rs = state.samples.replSetGetStatus;
    const ss = state.samples.serverStatus;
    const lastPrimary = [...state.events].reverse().find((e) => e.kind === "primary_changed" || e.kind === "primary_lost" || e.kind === "primary_restored");
    const snapPrimary = run.snapshot && run.snapshot.server ? run.snapshot.server.primary : null;
    const primary = lastPrimary ? (lastPrimary.kind === "primary_lost" ? "none" : lastPrimary.data.to) : snapPrimary;
    clusterHost.appendChild(h("table", { class: "kv" }, h("tbody", null, [
      h("tr", null, [h("th", { text: "Primary" }), h("td", { class: "mono" + (primary === "none" ? " hot" : ""), text: primary || "–" })]),
      ss ? h("tr", null, [h("th", { text: "Connections" }), h("td", { class: "mono", text: ss.data.connections ? `${F.int(ss.data.connections.current)} current, ${F.int(ss.data.connections.available)} available` : "–" })]) : null,
    ])));
    if (rs && rs.data.members) {
      clusterHost.appendChild(h("div", { class: "members", style: null }, rs.data.members.map((m) => h("div", { class: "member" }, [
        h("div", { class: "addr", text: m.name }),
        h("div", { class: m.health === 1 ? "" : "hot", text: `${m.state}${m.lag_seconds ? `, lag ${F.num(m.lag_seconds, 1)} s` : ""}` }),
      ]))));
      clusterHost.appendChild(h("p", { class: "xs muted", style: null, text: `Sampled ${F.ago(rs.t_ms)}` }));
    } else if (run.snapshot && run.snapshot.server) {
      clusterHost.appendChild(h("div", { class: "members" }, (run.snapshot.server.members || []).map((m) => h("div", { class: "member" }, [h("div", { class: "addr", text: m.address }), h("div", { text: `${m.state} at start` })]))));
    }
  }

  function redrawAll() {
    drawHeader();
    drawProgress();
    drawTitle();
    drawNotify();
    drawFlags();
    drawErrors();
    drawEvents();
    drawCluster();
  }

  /* ---- actions ---- */
  let markerOpen = null;
  function openMarker(phase) {
    if (markerOpen || !LIVE.includes(state.status)) return;
    const count = state.markers.length + 1;
    const label = input({ id: "marker-label", value: phase === "begin" ? "scale started" : phase === "end" ? "scale finished" : `marker ${count}`, maxlength: "120", autocomplete: "off" });
    const note = h("input", { type: "text", id: "marker-note", maxlength: "2000", placeholder: "Optional" });
    let chosen = phase || "auto";
    const seg = segmented([{ value: "auto", label: "From label" }, { value: "begin", label: "Begin" }, { value: "end", label: "End" }], chosen, (v) => { chosen = v; }, "Window phase");
    const at = Date.now();
    const save = async () => {
      const body = { label: label.value.trim() || `marker ${count}`, note: note.value.trim(), source: "ui" };
      if (chosen !== "auto") body.phase = chosen;
      try {
        await api.post(`api/runs/${runId}/markers`, body);
        toast(`Marker added: ${body.label}`);
        announce(`Marker added: ${body.label}`);
        close();
      } catch (err) { toast(err.title, "fail"); }
    };
    const pop = h("div", { class: "marker-pop", role: "dialog", "aria-label": "Add marker" }, [
      h("h2", { text: "Add marker" }),
      h("p", { class: "xs muted", text: `Pressed at ${F.clock(at, true)}. The marker is timestamped when you save it.` }),
      h("div", { class: "field" }, [h("label", { for: "marker-label", text: "Label" }), label, h("div", { class: "help", text: "Labels ending in \"started\" or \"finished\" pair up into an impact window." })]),
      h("div", { class: "field" }, [h("label", { for: "marker-note", text: "Note" }), note]),
      h("div", { class: "field" }, [h("div", { class: "label", text: "Window phase" }), seg]),
      h("div", { class: "row end" }, [button("Cancel", () => close()), button("Add marker", save, "primary")]),
    ]);
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      if (e.key === "Enter" && (e.target === label || e.target === note)) { e.preventDefault(); save(); }
    };
    pop.addEventListener("keydown", onKey);
    function close() { pop.remove(); markerOpen = null; markBtn.focus(); }
    head.appendChild(pop);
    markerOpen = pop;
    label.focus();
    label.select();
  }

  async function togglePause() {
    try {
      if (state.status === "running") { await api.post(`api/runs/${runId}/pause`); toast("Run paused"); announce("Run paused"); }
      else if (state.status === "paused") { await api.post(`api/runs/${runId}/resume`); toast("Run resumed"); announce("Run resumed"); }
    } catch (err) { toast(err.title, "fail"); }
  }

  async function stopRun() {
    await dialog({
      title: "Stop run", body: `Stop ${run.name}? In-flight operations get ${(profile.schedule || {}).drain_timeout_seconds || 15} s to finish, then the report is built.`,
      confirm: "Stop run", danger: true,
      onConfirm: async () => { await api.post(`api/runs/${runId}/stop`); toast("Run stopping"); announce("Run stopping"); },
    });
  }

  markBtn.addEventListener("click", () => openMarker(null));
  pauseBtn.addEventListener("click", togglePause);
  stopBtn.addEventListener("click", stopRun);
  app.keyHandler = (e) => {
    if (e.key === "m" || e.key === "M") { e.preventDefault(); openMarker(null); }
    else if (e.key === "p" || e.key === "P") { e.preventDefault(); togglePause(); }
    else if (e.key === "s" || e.key === "S") { e.preventDefault(); stopRun(); }
  };

  redrawAll();
  try {
    const samples = await api.get(`api/runs/${runId}/samples`);
    samples.forEach((s) => { state.samples[s.kind] = s; });
    drawCluster();
  } catch (e) { /* optional */ }

  /* ---- live updates ---- */
  let ticks = 0;
  const unsub = subscribe((kind, d) => {
    if (kind === "connection" && !d.connected) toast("Live connection lost; reconnecting", "fail");
    if (kind === "hello" && d.active && d.active.id === runId && d.points && !state.points.length) {
      state.points = d.points;
      charts.setData(state.points);
    }
    if (kind === "state" && d.id === runId) {
      state.status = d.status;
      if (d.phase) state.phase = d.phase;
      if (d.level) state.level = d.level;
      if (d.elapsed_ms) state.elapsed = d.elapsed_ms;
      if (d.progress !== undefined) { state.progress = d.progress; state.progressAt = Date.now(); }
      if (d.guardrails !== undefined) state.guardrails = d.guardrails;
      if (!LIVE.includes(d.status)) {
        const ended = d.status === "completed" ? "Run completed" : d.status === "failed" ? "Run failed" : "Run stopped";
        desktop(ended, run.name);
        toast(ended);
        setTimeout(() => navigate(`#/runs/${runId}`), 600);
        return;
      }
      drawHeader();
      drawProgress();
    }
    if (d && d.run !== runId) return;
    if (kind === "point") {
      const p = d.point;
      state.points.push(p);
      if (state.points.length > 7200) state.points.splice(0, state.points.length - 7200);
      state.last = p;
      state.elapsed = (state.elapsed || 0) + 1000;
      Object.entries(p.errors || {}).forEach(([k, v]) => { state.errors[k] = (state.errors[k] || 0) + v; });
      if (p.phase === "steady" && p.p99) state.baseP99 = state.baseP99 ? state.baseP99 * 0.98 + p.p99 * 0.02 : p.p99;
      if (state.windowSeconds !== 0) charts.setData(state.points, { markers: state.markers, events: state.events, gaps: state.gaps });
      if (p.err && state.cleanSeconds >= 30) desktop("Operations are failing", `${run.name}: ${F.num(p.err, 0)} failed in the last second`);
      state.cleanSeconds = p.err ? 0 : state.cleanSeconds + 1;
      drawHeader();
      drawProgress();
      drawTitle();
      if (++ticks % 2 === 0) { drawFlags(); drawErrors(); }
    } else if (kind === "marker") {
      state.markers.push(d);
      charts.setData(state.points, { markers: state.markers });
      drawEvents();
    } else if (kind === "event") {
      state.events.push(d);
      if (state.events.length > 2000) state.events.shift();
      if (d.kind === "phase") { state.phase = d.data.phase; if (d.data.level) { state.level = d.data.level; } }
      if (d.kind === "level_changed") state.level = d.data.level;
      if (d.kind === "sampling_unavailable") { state.samplingUnavailable = d; drawFlags(); }
      if (d.kind === "guardrail") { desktop("Guardrail stopped the run", (d.data.reasons || []).join("; ")); toast("Guardrail: " + (d.data.reasons || []).join("; "), "fail"); }
      if (d.kind === "primary_changed") desktop("Primary changed", `${run.name}: ${d.data.from || "none"} → ${d.data.to}`);
      drawEvents();
      drawCluster();
    } else if (kind === "gap") {
      state.gaps.push(d);
    } else if (kind === "load") {
      state.load = d;
      drawHeader();
    } else if (kind === "sample") {
      state.samples[d.kind] = d;
      if (d.kind !== "hello") drawCluster();
    }
  });

  return () => { unsub(); if (allTimer) clearInterval(allTimer); charts.destroy(); app.keyHandler = null; if (markerOpen) markerOpen.remove(); };
}
