import { api } from "../api.js";
import { navigate, setTitle } from "../app.js";
import { h, F, chip, pageHead, button, errorBlock, loading, toast, dialog, download } from "../ui.js";

const LIVE = ["starting", "loading", "running", "paused", "stopping"];

export async function render(root, [runId]) {
  root.appendChild(loading("Loading report"));
  let run, analysis, series;
  try {
    [run, series] = await Promise.all([api.get(`api/runs/${runId}`), api.get(`api/runs/${runId}/series?max_points=2400`)]);
    analysis = run.analysis || (await api.get(`api/runs/${runId}/analysis`));
  } catch (err) {
    root.textContent = "";
    root.appendChild(pageHead("Run report"));
    root.appendChild(errorBlock(err, () => navigate(`#/runs/${runId}`)));
    root.appendChild(h("a", { href: "#/runs", text: "Back to runs" }));
    return null;
  }
  if (LIVE.includes(run.status)) {
    navigate(`#/runs/${runId}/live`);
    return null;
  }
  setTitle(run.name);
  root.textContent = "";
  const duration = run.started_at && run.ended_at ? run.ended_at - run.started_at : null;
  const sub = [chip(run.status), " ", F.full(run.started_at || run.created_at), " – ", run.ended_at ? F.clock(run.ended_at) : "", duration ? ` (${F.duration(duration)})` : "", " · ", run.target_name || "", " · ", h("span", { class: "mono", text: run.database_name || "" })];
  root.appendChild(pageHead(run.name, sub, [
    button("Export HTML", () => download(`api/runs/${runId}/export.html`)),
    button("CSV", () => download(`api/runs/${runId}/export.csv`)),
    button("JSON", () => download(`api/runs/${runId}/export.json`)),
    button("Compare with…", () => navigate(`#/compare?ids=${runId}`)),
    button("Delete", async () => {
      await dialog({ title: "Delete run", body: `Delete ${run.name} and all of its data? This cannot be undone.`, confirm: "Delete run", danger: true,
        onConfirm: async () => { await api.del(`api/runs/${runId}`); toast("Run deleted"); navigate("#/runs"); } });
    }, "danger"),
  ]));
  const host = h("div");
  root.appendChild(host);
  const view = window.BallastReport.render(host, { run, analysis, series, markers: run.markers, events: run.events, gaps: run.gaps }, {
    onAnalyzeRange: async (range) => {
      if (!range || range.min === null || range.max === null) return;
      try {
        const imp = await api.post(`api/runs/${runId}/window`, { start_ms: Math.round(range.min * 1000), end_ms: Math.round(range.max * 1000), label: "Selected range" });
        view.addImpact(imp);
        toast("Range analysed");
      } catch (err) {
        toast(err.title, "fail");
      }
    },
  });
  return () => view.destroy();
}
