import { api } from "../api.js";
import { navigate, setTitle } from "../app.js";
import { h, F, pageHead, button, empty, errorBlock, loading, toast, dialog, download, sortableTable } from "../ui.js";
import { profileForm, cleanProfile, opsLabel } from "./profile-form.js";

export async function render(root) {
  setTitle("Workload profiles");
  const fileInput = h("input", { type: "file", accept: "application/json,.json", class: "visually-hidden", id: "profile-import", "aria-label": "Import profile file" });
  root.appendChild(pageHead("Workload profiles", "What to send, how fast, and with which driver settings. Saved profiles are starting points for new runs.", [
    h("label", { for: "profile-import", class: "btn", text: "Import", role: "button", tabindex: "0" }), fileInput,
    button("New profile", () => edit(null), "primary"),
  ]));
  const listHost = h("div");
  const editHost = h("div");
  root.appendChild(h("div", { class: "split editor-wide" }, [listHost, editHost]));
  let profiles = [];
  let editing = null;

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    try {
      const textBody = await file.text();
      let parsed;
      try { parsed = JSON.parse(textBody); } catch (e) { throw { title: "The file is not valid JSON", detail: e.message }; }
      const created = await api.post("api/profiles/import", parsed);
      toast(`Profile imported: ${created.name}`);
      await load();
      edit(created);
    } catch (err) {
      toast(err.title + (err.detail ? ": " + err.detail : ""), "fail");
    }
    fileInput.value = "";
  });

  async function load() {
    listHost.textContent = "";
    listHost.appendChild(loading("Loading profiles"));
    try {
      profiles = await api.get("api/profiles");
    } catch (err) {
      listHost.textContent = "";
      listHost.appendChild(errorBlock(err, load));
      return;
    }
    drawList();
  }

  function drawList() {
    listHost.textContent = "";
    if (!profiles.length) {
      listHost.appendChild(h("section", { class: "panel" }, empty("No profiles", "Create a profile to reuse a workload across runs.", [button("New profile", () => edit(null), "primary")])));
      return;
    }
    const table = sortableTable([
      { key: "name", label: "Name", value: (p) => p.name.toLowerCase(), render: (p) => [h("button", { type: "button", class: "btn quiet small", style: null, text: p.name, onclick: () => edit(p) }), h("div", { class: "xs muted", text: p.body.description || "" }), h("div", { class: "xs muted", text: `${opsLabel(p.body)}; updated ${F.dateTime(p.updated_at)}` })], cls: "wrap" },
      { key: "model", label: "Load", value: (p) => p.body.load.model, render: (p) => (p.body.load.model === "open" ? `${F.num(p.body.load.rate, 0)} ops/s` : `${F.int(p.body.load.concurrency)} workers`) },
      { key: "act", label: "", render: (p) => h("div", { class: "row-actions" }, [
        button("Duplicate", async () => { try { const c = await api.post(`api/profiles/${p.id}/duplicate`); toast(`Profile duplicated: ${c.name}`); await load(); edit(c); } catch (err) { toast(err.title, "fail"); } }, "small"),
        button("Export", () => download(`api/profiles/${p.id}/export`), "small"),
      ]) },
    ], profiles, { sortKey: "name", dir: "asc", rowClass: (p) => (editing && editing.id === p.id ? "selected" : "") });
    listHost.appendChild(h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Saved profiles" }), h("span", { class: "meta", text: `${profiles.length}` })]), h("div", { class: "body flush" }, table.node)]));
  }

  function edit(row) {
    editing = row;
    drawList();
    editHost.textContent = "";
    const body = row ? JSON.parse(JSON.stringify(row.body)) : { name: "new profile", description: "", operations: { find: 60, update: 30, insert: 10 }, load: { model: "open", rate: 1000, concurrency: 64, processes: 1 }, schedule: { warmup_seconds: 30, steady_seconds: null, drain_timeout_seconds: 15 }, dataset: { database: "ballast_bench", collections: 1, collection_prefix: "bench", load: { documents: 100000 } } };
    const problems = h("div");
    let jsonMode = false;
    const formHost = h("div");
    const jsonArea = h("textarea", { rows: "28", spellcheck: "false", "aria-label": "Profile JSON" });
    function showForm() { formHost.textContent = ""; formHost.appendChild(profileForm(body)); }
    showForm();
    const toggle = button("Edit as JSON", () => {
      if (!jsonMode) {
        jsonArea.value = JSON.stringify(cleanProfile(body), null, 2);
        formHost.textContent = "";
        formHost.appendChild(jsonArea);
        toggle.textContent = "Edit as form";
      } else {
        try { Object.assign(body, JSON.parse(jsonArea.value)); } catch (e) { toast("JSON is not valid: " + e.message, "fail"); return; }
        showForm();
        toggle.textContent = "Edit as JSON";
      }
      jsonMode = !jsonMode;
    }, "small");
    const save = button(row ? "Save profile" : "Create profile", async () => {
      problems.textContent = "";
      let payload = body;
      if (jsonMode) {
        try { payload = JSON.parse(jsonArea.value); } catch (e) { problems.appendChild(errorBlock({ title: "JSON is not valid", detail: e.message })); return; }
      }
      payload = cleanProfile(payload);
      try {
        const saved = row ? await api.put(`api/profiles/${row.id}`, payload) : await api.post("api/profiles", payload);
        toast(row ? `Profile saved: ${saved.name}` : `Profile created: ${saved.name}`);
        await load();
        edit(saved);
      } catch (err) {
        problems.appendChild(errorBlock(err));
        problems.scrollIntoView({ block: "nearest" });
      }
    }, "primary");
    const actions = [toggle, h("span", { class: "spacer" })];
    if (row) {
      actions.push(button("Delete", () => dialog({ title: "Delete profile", body: `Delete ${row.name}? Runs that used it keep their own copy.`, confirm: "Delete profile", danger: true,
        onConfirm: async () => { await api.del(`api/profiles/${row.id}`); toast("Profile deleted"); editHost.textContent = ""; editing = null; await load(); } }), "danger"));
      actions.push(button("Use in new run", () => navigate("#/new")));
    }
    actions.push(save);
    editHost.appendChild(h("section", { class: "panel" }, [
      h("header", null, [h("h2", { text: row ? `Edit ${row.name}` : "New profile" }), button("Close", () => { editHost.textContent = ""; editing = null; drawList(); }, "quiet small")]),
      h("div", { class: "body" }, [formHost, problems, h("div", { class: "row" }, actions)]),
    ]));
  }

  await load();
  if (!editing && profiles.length) editHost.appendChild(h("section", { class: "panel" }, empty("Select a profile", "Pick a profile on the left to edit it, or create a new one.", [])));
  return null;
}
