import { api } from "../api.js";
import { navigate, setTitle } from "../app.js";
import { h, F, chip, pageHead, button, errorBlock, loading, toast, dialog, download, field, input } from "../ui.js";

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
    button("Run again", () => rerun(run), "primary"),
    button("Copy summary", () => copySummary(runId)),
    button("Export HTML", () => download(`api/runs/${runId}/export.html`)),
    button("Markdown", () => download(`api/runs/${runId}/export.md`)),
    button("CSV", () => download(`api/runs/${runId}/export.csv`)),
    button("JSON", () => download(`api/runs/${runId}/export.json`)),
    button("Compare with…", () => navigate(`#/compare?ids=${runId}`)),
    button("Delete", async () => {
      await dialog({ title: "Delete run", body: `Delete ${run.name} and all of its data? This cannot be undone.`, confirm: "Delete run", danger: true,
        onConfirm: async () => { await api.del(`api/runs/${runId}`); toast("Run deleted"); navigate("#/runs"); } });
    }, "danger"),
  ]));
  const metaHost = h("div");
  root.appendChild(metaHost);
  const drawMeta = () => {
    metaHost.textContent = "";
    metaHost.appendChild(metaBar(run, async (changes) => {
      const updated = await api.patch(`api/runs/${runId}`, changes);
      Object.assign(run, { name: updated.name, notes: updated.notes, tags: updated.tags });
      root.querySelector(".page-head h1").textContent = run.name;
      setTitle(run.name);
      drawMeta();
      toast("Run details saved");
    }));
  };
  drawMeta();
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

function metaBar(run, save) {
  const tags = (run.tags || []).map((t) => h("a", { class: "tag", href: `#/runs?tag=${encodeURIComponent(t)}`, text: t }));
  const edit = button(run.notes || (run.tags || []).length ? "Edit notes and tags" : "Add notes or tags", () => editMeta(run, save), "small");
  return h("div", { class: "meta-bar" }, [
    tags.length ? h("div", { class: "tags", "aria-label": "Tags" }, tags) : null,
    run.notes ? h("p", { class: "notes-text", text: run.notes }) : null,
    edit,
  ]);
}

function editMeta(run, save) {
  const name = input({ id: "meta-name", value: run.name, maxlength: "120" });
  const notes = h("textarea", { id: "meta-notes", rows: "5", maxlength: "5000" });
  notes.value = run.notes || "";
  const tags = input({ id: "meta-tags", value: (run.tags || []).join(", "), placeholder: "resize, m40-to-m50, retryable-writes-off" });
  const body = h("div", { class: "stack" }, [
    field("Name", name),
    field("Notes", notes, "What changed, what you expected, links. Shown on the report and in exports."),
    field("Tags", tags, "Comma-separated. Letters, digits, space and . _ : / -, up to 40 characters each."),
  ]);
  return dialog({
    title: "Run details", body, confirm: "Save",
    onConfirm: () => save({ name: name.value.trim(), notes: notes.value, tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean) }),
  });
}

async function copySummary(runId) {
  let text;
  try {
    text = await api.text(`api/runs/${runId}/export.md`);
  } catch (err) {
    toast("Could not build the summary", "fail");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    toast("Summary copied as Markdown");
  } catch (e) {
    const area = h("textarea", { rows: "14", class: "mono", readonly: "" });
    area.value = text;
    dialog({ title: "Summary (Markdown)", body: h("div", null, [h("p", { class: "small", text: "Your browser blocked clipboard access. Select the text and copy it." }), area]), confirm: "Done" });
    area.select();
  }
}

async function rerun(run, acknowledge) {
  try {
    const created = await api.post(`api/runs/${run.id}/rerun`, { acknowledge_foreign: !!acknowledge });
    toast(`Started ${created.name}`);
    navigate(`#/runs/${created.id}/live`);
  } catch (err) {
    if (err.status === 428) {
      const ok = await dialog({ title: err.title, body: [err.detail, err.fix].filter(Boolean).join(" "), confirm: "Start anyway" });
      if (ok) await rerun(run, true);
      return;
    }
    dialog({ title: err.title || "Could not start", body: errorBlock(err), confirm: "Close" });
  }
}
