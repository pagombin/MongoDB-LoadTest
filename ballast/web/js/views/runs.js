import { api } from "../api.js";
import { app, navigate, setTitle } from "../app.js";
import { subscribe } from "../live.js";
import { h, F, chip, pageHead, button, empty, errorBlock, loading, sortableTable, dialog, toast, select } from "../ui.js";

const LIVE = ["starting", "loading", "running", "paused", "stopping"];

function activeBand(active) {
  if (!active || !LIVE.includes(active.status)) return null;
  const last = active.last || {};
  const figures = h("div", { class: "figures" }, [
    window.BallastReport.figure("Elapsed", F.duration(active.elapsed_ms || 0), "", active.phase || ""),
    window.BallastReport.figure("Throughput", F.num(last.ok || 0, 0), "ops/s", active.target_rate ? "target " + F.num(active.target_rate, 0) : "closed model"),
    window.BallastReport.latFigure("p99", last.p99, "last second"),
    window.BallastReport.figure("Errors", F.num(last.err || 0, 0), "/s", "last second", last.err ? "bad" : ""),
  ]);
  return h("section", { class: "panel", "aria-label": "Active run" }, [
    h("header", null, [h("div", { class: "row" }, [chip(active.status), h("h2", { text: active.name })]), h("a", { class: "btn primary", href: `#/runs/${active.id}/live`, text: "Open live view" })]),
    h("div", { class: "body" }, active.status === "loading" && active.load ? h("p", { text: `Loading data: ${F.int(active.load.done)} documents written.` }) : figures),
  ]);
}

export async function render(root, [query]) {
  setTitle("Runs");
  root.appendChild(pageHead("Runs", "Every run, newest first. Open one to see its report.", [h("a", { class: "btn primary", href: "#/new" }, ["New run ", h("kbd", { text: "N" })])]));
  const bandHost = h("div");
  const listHost = h("div");
  root.appendChild(bandHost);
  root.appendChild(listHost);
  listHost.appendChild(loading("Loading runs"));
  let data;
  const load = async () => {
    try {
      data = await api.get("api/runs");
    } catch (err) {
      listHost.textContent = "";
      listHost.appendChild(errorBlock(err, load));
      return;
    }
    draw();
  };
  const selected = new Set();
  const params = new URLSearchParams(query || "");
  let filterText = params.get("q") || "", statusFilter = params.get("status") || "all", targetFilter = params.get("target") || "all", tagFilter = params.get("tag") || "all";
  let table = null;

  /* Filters live in the address, so a filtered list can be bookmarked or shared. */
  function remember() {
    const q = new URLSearchParams();
    if (filterText) q.set("q", filterText);
    if (statusFilter !== "all") q.set("status", statusFilter);
    if (targetFilter !== "all") q.set("target", targetFilter);
    if (tagFilter !== "all") q.set("tag", tagFilter);
    const hash = "#/runs" + (q.toString() ? "?" + q.toString() : "");
    if (window.location.hash !== hash) history.replaceState(null, "", hash);
  }

  function rows() {
    return data.runs.filter((r) => {
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (targetFilter !== "all" && r.target_name !== targetFilter) return false;
      if (tagFilter !== "all" && !(r.tags || []).includes(tagFilter)) return false;
      if (filterText) {
        const hay = (r.name + " " + (r.target_name || "") + " " + (r.database_name || "") + " " + r.id + " " + (r.tags || []).join(" ") + " " + (r.notes || "")).toLowerCase();
        if (!hay.includes(filterText.toLowerCase())) return false;
      }
      return true;
    });
  }

  function draw() {
    bandHost.textContent = "";
    const band = activeBand(data.active);
    if (band) bandHost.appendChild(band);
    listHost.textContent = "";
    if (!data.runs.length) {
      listHost.appendChild(h("section", { class: "panel" }, empty("No runs yet", "Add a target, then start a run. Runs and their per-second data are kept under data/ on this server.", [
        h("a", { class: "btn", href: "#/targets", text: "Add target" }), h("a", { class: "btn primary", href: "#/new", text: "New run" }),
      ])));
      return;
    }
    const filter = h("input", { type: "search", placeholder: "Filter by name, target, tag, notes or id", "aria-label": "Filter runs", "data-filter": "", value: filterText });
    filter.addEventListener("input", () => { filterText = filter.value; table.setRows(rows()); foot(); remember(); });
    const statuses = ["all", "running", "completed", "stopped", "failed", "interrupted"];
    const statusSel = select(statuses.map((s) => ({ value: s, label: s === "all" ? "All statuses" : F.statusText(s) })), statusFilter, { "aria-label": "Status" });
    statusSel.addEventListener("change", () => { statusFilter = statusSel.value; table.setRows(rows()); foot(); remember(); });
    const targetsList = ["all", ...new Set(data.runs.map((r) => r.target_name).filter(Boolean))];
    const targetSel = select(targetsList.map((t) => ({ value: t, label: t === "all" ? "All targets" : t })), targetFilter, { "aria-label": "Target" });
    targetSel.addEventListener("change", () => { targetFilter = targetSel.value; table.setRows(rows()); foot(); remember(); });
    const tagList = [...new Set(data.runs.flatMap((r) => r.tags || []))].sort();
    if (tagFilter !== "all" && !tagList.includes(tagFilter)) tagList.push(tagFilter);
    const tagSel = select([{ value: "all", label: "All tags" }, ...tagList.map((t) => ({ value: t, label: t }))], tagFilter, { "aria-label": "Tag" });
    tagSel.addEventListener("change", () => { tagFilter = tagSel.value; table.setRows(rows()); foot(); remember(); });
    const toolbar = h("div", { class: "chart-toolbar" }, [h("div", { style: null, class: "spacer" }, filter), h("div", null, statusSel), h("div", null, targetSel), tagList.length ? h("div", null, tagSel) : null]);
    const cols = [
      { key: "sel", label: "", render: (r) => {
        const cb = h("input", { type: "checkbox", "aria-label": `Select ${r.name}` });
        cb.checked = selected.has(r.id);
        cb.addEventListener("change", () => { if (cb.checked) selected.add(r.id); else selected.delete(r.id); foot(); });
        return cb;
      } },
      { key: "name", label: "Name", value: (r) => r.name.toLowerCase(), render: (r) => [h("a", { class: "rowlink", href: LIVE.includes(r.status) ? `#/runs/${r.id}/live` : `#/runs/${r.id}`, text: r.name }), (r.tags || []).length ? h("div", { class: "tags small" }, r.tags.map((t) => h("span", { class: "tag", text: t }))) : null] },
      { key: "status", label: "Status", value: (r) => r.status, render: (r) => [chip(r.status), r.stop_reason === "guardrail" ? h("span", { class: "chip warn plain", text: "guardrail" }) : null, r.client_bound ? h("span", { class: "chip warn plain", style: null, text: "client-bound" }) : null] },
      { key: "slo", label: "SLO", value: (r) => (r.slo_passed === null || r.slo_passed === undefined ? null : r.slo_passed ? 1 : 0), render: (r) => (r.slo_passed === true ? h("span", { class: "verdict ok", text: "pass" }) : r.slo_passed === false ? h("span", { class: "verdict fail", text: "fail" }) : h("span", { class: "muted", text: "–" })) },
      { key: "target", label: "Target", value: (r) => r.target_name || "", render: (r) => [r.target_name || "–", h("div", { class: "xs muted mono", text: r.database_name || "" })] },
      { key: "started", label: "Started", value: (r) => r.started_at || r.created_at, render: (r) => F.dateTime(r.started_at || r.created_at) },
      { key: "dur", label: "Duration", num: true, value: (r) => (r.ended_at && r.started_at ? r.ended_at - r.started_at : null), render: (r) => (r.ended_at && r.started_at ? F.duration(r.ended_at - r.started_at) : LIVE.includes(r.status) ? "live" : "–") },
      { key: "tput", label: "ops/s", num: true, value: (r) => r.summary && r.summary.throughput, render: (r) => (r.summary ? F.num(r.summary.throughput, 0) : "–") },
      { key: "p99", label: "p99", num: true, value: (r) => r.summary && r.summary.response_time && r.summary.response_time.p99_us, render: (r) => (r.summary ? F.lat(r.summary.response_time.p99_us) : "–") },
      { key: "err", label: "Errors", num: true, value: (r) => r.summary && r.summary.err, render: (r) => (r.summary ? F.int(r.summary.err) : "–") },
      { key: "imp", label: "Windows", num: true, value: (r) => r.impact_count, render: (r) => String(r.impact_count || 0) },
    ];
    table = sortableTable(cols, rows(), { sortKey: "started", dir: "desc" });
    const footer = h("div", { class: "table-foot" });
    function foot() {
      footer.textContent = "";
      const shown = rows().length;
      footer.appendChild(h("span", { text: `${shown} of ${data.runs.length} runs` + (selected.size ? `, ${selected.size} selected` : "") }));
      footer.appendChild(h("span", { class: "spacer" }));
      const cmp = button("Compare selected", () => navigate(`#/compare?ids=${[...selected].join(",")}`), "", { disabled: selected.size < 2 ? true : null });
      const del = button("Delete selected", deleteSelected, "danger", { disabled: selected.size < 1 ? true : null });
      footer.appendChild(cmp);
      footer.appendChild(del);
    }
    async function deleteSelected() {
      const ids = [...selected];
      const names = data.runs.filter((r) => ids.includes(r.id)).map((r) => r.name);
      await dialog({
        title: `Delete ${ids.length} run${ids.length === 1 ? "" : "s"}`,
        body: `This permanently deletes ${names.slice(0, 5).join(", ")}${names.length > 5 ? " and others" : ""}, including per-second data and markers. Export reports first if you need them.`,
        confirm: "Delete runs", danger: true,
        onConfirm: async () => {
          for (const id of ids) await api.del(`api/runs/${id}`);
          selected.clear();
          toast(`Runs deleted: ${ids.length}`);
          await load();
        },
      });
    }
    foot();
    listHost.appendChild(h("section", { class: "panel" }, [toolbar, h("div", { class: "body flush" }, table.node), footer]));
  }

  await load();
  let timer = null;
  const unsub = subscribe((kind, d) => {
    if (!data) return;
    if (kind === "state") {
      if (timer) clearTimeout(timer);
      timer = setTimeout(load, 300);
    }
    if (kind === "point" && data.active && d.run === data.active.id) {
      data.active.last = d.point;
      data.active.elapsed_ms = (data.active.elapsed_ms || 0) + 1000;
      bandHost.textContent = "";
      const band = activeBand(data.active);
      if (band) bandHost.appendChild(band);
    }
  });
  return () => { unsub(); if (timer) clearTimeout(timer); };
}
