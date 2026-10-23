import { api } from "../api.js";
import { navigate, setTitle } from "../app.js";
import { h, F, pageHead, button, empty, errorBlock, loading, select, segmented } from "../ui.js";

export async function render(root, [query]) {
  setTitle("Compare");
  const params = new URLSearchParams(query || "");
  let ids = (params.get("ids") || "").split(",").filter(Boolean);
  let align = params.get("align") || "start";
  let marker = params.get("marker") || "";
  root.appendChild(pageHead("Compare runs", "Two or more runs on the same axes, aligned at their start or at a marker."));
  const pickHost = h("div");
  const chartHost = h("div");
  root.appendChild(pickHost);
  root.appendChild(chartHost);
  let runs = [];
  try { runs = (await api.get("api/runs")).runs.filter((r) => r.started_at); } catch (err) { pickHost.appendChild(errorBlock(err)); return null; }
  if (runs.length < 2) {
    pickHost.appendChild(h("section", { class: "panel" }, empty("Not enough runs", "Comparison needs at least two runs with data.", [h("a", { class: "btn primary", href: "#/new", text: "New run" })])));
    return null;
  }
  let destroyers = [];

  function go() {
    const q = new URLSearchParams({ ids: ids.join(","), align });
    if (align === "marker" && marker) q.set("marker", marker);
    navigate(`#/compare?${q.toString()}`);
  }

  async function drawPicker() {
    pickHost.textContent = "";
    const chips = h("div", { class: "row" }, ids.map((id) => {
      const r = runs.find((x) => x.id === id);
      return h("span", { class: "chip plain" }, [r ? r.name : id, h("button", { type: "button", class: "btn quiet small", "aria-label": `Remove ${r ? r.name : id}`, text: "×", onclick: () => { ids = ids.filter((x) => x !== id); go(); } })]);
    }));
    const addSel = select([{ value: "", label: "Add a run…" }, ...runs.filter((r) => !ids.includes(r.id)).map((r) => ({ value: r.id, label: `${r.name} (${F.dateTime(r.started_at)})` }))], "", { "aria-label": "Add a run" });
    addSel.addEventListener("change", () => { if (addSel.value && ids.length < 6) { ids.push(addSel.value); go(); } });
    let markerSel = null;
    if (ids.length >= 1) {
      let labels = [];
      try { labels = await api.get(`api/compare/markers?ids=${ids.join(",")}`); } catch (e) { labels = []; }
      markerSel = select([{ value: "", label: labels.length ? "Choose a marker" : "No markers in these runs" }, ...labels.map((l) => ({ value: l, label: l }))], marker, { "aria-label": "Marker to align on", disabled: align !== "marker" ? true : null });
      markerSel.addEventListener("change", () => { marker = markerSel.value; go(); });
    }
    const alignSeg = segmented([{ value: "start", label: "Align at run start" }, { value: "marker", label: "Align at marker" }], align, (v) => { align = v; go(); }, "Alignment");
    pickHost.appendChild(h("section", { class: "panel" }, h("div", { class: "chart-toolbar" }, [chips, h("div", null, addSel), h("span", { class: "spacer" }), alignSeg, markerSel ? h("div", null, markerSel) : null])));
  }

  async function drawCharts() {
    destroyers.forEach((d) => d());
    destroyers = [];
    chartHost.textContent = "";
    if (ids.length < 2) {
      chartHost.appendChild(h("section", { class: "panel" }, empty("Pick runs to compare", "Add at least two runs above, or select runs on the Runs page and choose Compare selected.", [])));
      return;
    }
    chartHost.appendChild(loading("Loading runs"));
    let data;
    try { data = await api.get(`api/compare?ids=${ids.join(",")}&align=${align}&marker=${encodeURIComponent(marker)}`); } catch (err) { chartHost.textContent = ""; chartHost.appendChild(errorBlock(err)); return; }
    chartHost.textContent = "";
    const t = window.BallastCharts.theme();
    const colors = [t.c1, t.c2, t.c3, t.c4, t.c5];
    const dashes = ["", "dashed", "dotted", "dashed", "dotted"];
    const dupes = new Set(data.runs.map((r) => r.name).filter((n, i, all) => all.indexOf(n) !== i));
    data.runs.forEach((r) => { r.label = dupes.has(r.name) ? `${r.name} (${F.dateTime(r.started_at || r.created_at)})` : r.name; });
    const legend = h("div", { class: "legend" }, data.runs.map((r, i) => {
      const sw = h("span", { class: "swatch " + dashes[i % dashes.length] });
      sw.style.color = colors[i % colors.length];
      return h("span", { class: "key" }, [sw, r.label + (align === "marker" && r.aligned_on !== "marker" ? " (marker not found, aligned at start)" : "")]);
    }));
    const tHost = h("div", { class: "chart-host" });
    const lHost = h("div", { class: "chart-host" });
    chartHost.appendChild(h("section", { class: "panel" }, [
      h("header", null, [h("h2", { text: "Throughput and p99 response time" }), legend]),
      h("div", { class: "body" }, [h("div", { class: "chart-title" }, h("strong", { text: "Successful ops/s" })), tHost, h("div", { class: "chart-title" }, h("strong", { text: "p99 response time (log scale)" })), lHost,
        h("p", { class: "xs muted", text: "x axis: time from the alignment point. Drag to zoom, double-click to reset." })]),
    ]));
    const a = window.BallastCharts.compareChart(tHost, data.runs, "throughput");
    const b = window.BallastCharts.compareChart(lHost, data.runs, "p99");
    destroyers.push(a.destroy, b.destroy);
    const metrics = data.runs.map((r) => {
      const s = r.summary || {}, rt = s.response_time || {};
      const impacts = r.impacts || [];
      const worstGap = impacts.reduce((m, i) => Math.max(m, i.longest_write_gap ? i.longest_write_gap.ms : 0), 0);
      const recov = impacts.filter((i) => i.recovery_seconds !== null && i.recovery_seconds !== undefined).map((i) => i.recovery_seconds);
      return { tput: s.throughput, p50: rt.p50_us, p99: rt.p99_us, max: rt.max_us, err: s.error_rate || 0, gap: worstGap, recov: recov.length ? Math.max(...recov) : null };
    });
    const base = metrics[0];
    /* Each later run shows its change against the first run; green is better, red is worse. */
    function cell(key, text, higherIsBetter, i) {
      const v = metrics[i][key], b = base[key];
      const out = h("td", { class: "num" }, [text]);
      if (i === 0 || v === null || v === undefined || b === null || b === undefined) return out;
      if (b === 0 && v === 0) return out;
      const change = b === 0 ? null : (v - b) / b;
      const better = higherIsBetter ? v > b : v < b;
      const same = change !== null && Math.abs(change) < 0.005;
      const label = change === null ? (v > b ? "new" : "") : `${change > 0 ? "+" : ""}${F.num(change * 100, Math.abs(change) < 0.1 ? 1 : 0)}%`;
      if (label) out.appendChild(h("span", { class: "delta " + (same ? "" : better ? "better" : "worse"), text: label, title: same ? "about the same as the first run" : (better ? "better" : "worse") + " than the first run" }));
      return out;
    }
    const rows = data.runs.map((r, i) => {
      const m = metrics[i];
      return h("tr", null, [
        h("td", null, [h("a", { class: "rowlink", href: `#/runs/${r.id}`, text: r.label }), i === 0 && data.runs.length > 1 ? h("span", { class: "xs muted", text: " baseline" }) : null]),
        cell("tput", F.num(m.tput, 0), true, i), cell("p50", F.lat(m.p50), false, i), cell("p99", F.lat(m.p99), false, i),
        cell("max", F.lat(m.max), false, i), cell("err", F.pct(m.err), false, i),
        cell("gap", m.gap ? F.lat(m.gap * 1000) : "none", false, i), cell("recov", m.recov !== null ? F.seconds(m.recov) : "–", false, i),
      ]);
    });
    chartHost.appendChild(h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Summary" })), h("div", { class: "body flush scroll-x" }, h("table", { class: "data" }, [
      h("thead", null, h("tr", null, ["Run", "ops/s", "p50", "p99", "max", "Error rate", "Longest write gap", "Slowest recovery"].map((x, i) => h("th", { scope: "col", class: i ? "num" : "", text: x })))),
      h("tbody", null, rows),
    ]))]));
  }

  await drawPicker();
  await drawCharts();
  return () => destroyers.forEach((d) => d());
}
