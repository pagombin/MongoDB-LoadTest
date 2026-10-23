import { api } from "../api.js";
import { setTitle } from "../app.js";
import { h, F, pageHead, button, empty, errorBlock, loading, toast, dialog, input, select, field } from "../ui.js";

function testView(r) {
  if (!r) return h("p", { class: "muted small", text: "Not tested yet. Test before starting a run." });
  if (!r.ok) {
    return h("div", { class: "notice fail", role: "alert" }, h("div", null, [h("div", { class: "notice-title", text: r.title }), h("div", { class: "small mono", text: r.detail }), h("div", { class: "small fix", text: r.fix }), h("div", { class: "xs muted", text: `Tested ${F.ago(r.tested_at)}, took ${F.int(r.elapsed_ms)} ms` })]));
  }
  return h("div", { class: "notice ok" }, h("div", null, [
    h("div", { class: "notice-title", text: `Connected: MongoDB ${r.server_version || "(version hidden)"}, ${r.topology}${r.set_name ? ", replica set " + r.set_name : ""}` }),
    h("div", { class: "small", text: `Primary ${r.primary || "none visible"}. Tested ${F.ago(r.tested_at)} in ${F.int(r.elapsed_ms)} ms.` }),
    h("div", { class: "members", style: null }, (r.members || []).map((m) => h("div", { class: "member" }, [h("div", { class: "addr", text: m.address }), h("div", { text: m.state + (m.round_trip_ms !== null && m.round_trip_ms !== undefined ? `, ${F.num(m.round_trip_ms, 1)} ms` : "") })]))),
  ]));
}

export async function render(root) {
  setTitle("Targets");
  root.appendChild(pageHead("Targets", "MongoDB deployments to test. Passwords are stored under data/ with owner-only permissions and are never shown after saving.", [button("Add target", () => edit(null), "primary")]));
  const listHost = h("div");
  const editHost = h("div");
  root.appendChild(h("div", { class: "split" }, [listHost, editHost]));
  let targets = [];
  let editing = null;
  let formOpen = false;

  async function load() {
    listHost.textContent = "";
    listHost.appendChild(loading("Loading targets"));
    try { targets = await api.get("api/targets"); } catch (err) { listHost.textContent = ""; listHost.appendChild(errorBlock(err, load)); return; }
    drawList();
  }

  function drawList() {
    listHost.textContent = "";
    if (!targets.length) {
      listHost.appendChild(h("section", { class: "panel" }, empty("No targets yet", formOpen ? "Fill in the form to add the cluster you want to test. mongodb:// and mongodb+srv:// both work." : "Add a connection string for the cluster you want to test. mongodb:// and mongodb+srv:// both work.", formOpen ? [] : [button("Add target", () => edit(null), "primary")])));
      return;
    }
    const rows = targets.map((t) => {
      const r = t.last_test;
      const status = !r ? h("span", { class: "muted", text: "not tested" }) : r.ok ? h("span", { class: "chip ok", text: `${r.server_version || "ok"} ${r.set_name || r.topology}` }) : h("span", { class: "chip fail", text: r.title });
      return h("tr", { class: editing && editing.id === t.id ? "selected" : "" }, [
        h("td", null, h("button", { type: "button", class: "btn quiet small", text: t.name, onclick: () => edit(t) })),
        h("td", { class: "wrap mono xs", text: t.srv ? `${t.hosts.join(", ")} (SRV)` : t.hosts.join(", ") }),
        h("td", { text: t.tls }),
        h("td", null, [status, h("div", { class: "xs muted", text: r ? F.ago(r.tested_at) : "" })]),
      ]);
    });
    listHost.appendChild(h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Saved targets" }), h("span", { class: "meta", text: String(targets.length) })]),
      h("div", { class: "body flush scroll-x" }, h("table", { class: "data" }, [h("thead", null, h("tr", null, ["Name", "Hosts", "TLS", "Last test"].map((x) => h("th", { scope: "col", text: x })))), h("tbody", null, rows)]))]));
  }

  function edit(t) {
    editing = t;
    formOpen = true;
    drawList();
    editHost.textContent = "";
    const name = input({ id: "t-name", value: t ? t.name : "", maxlength: "80", autocomplete: "off" });
    const uri = input({ id: "t-uri", value: t ? t.uri : "", class: "mono", spellcheck: "false", autocomplete: "off", placeholder: "mongodb+srv://user:password@cluster.example.net/" });
    const tls = select([{ value: "auto", label: "From connection string" }, { value: "on", label: "On" }, { value: "off", label: "Off" }], t ? t.tls : "auto", { id: "t-tls" });
    const ca = input({ id: "t-ca", value: t && t.tls_ca_file ? t.tls_ca_file : "", class: "mono", placeholder: "/home/me/ca.pem" });
    const cert = input({ id: "t-cert", value: t && t.tls_certificate_key_file ? t.tls_certificate_key_file : "", class: "mono" });
    const invalid = h("input", { type: "checkbox", id: "t-invalid" });
    invalid.checked = !!(t && t.tls_allow_invalid_hostnames);
    const notes = input({ id: "t-notes", value: t ? t.notes : "", maxlength: "2000" });
    const result = h("div", { "aria-live": "polite" }, t ? testView(t.last_test) : null);
    const problems = h("div");
    function payload() {
      return { name: name.value.trim(), uri: uri.value.trim(), tls: tls.value, tls_ca_file: ca.value.trim() || null, tls_certificate_key_file: cert.value.trim() || null, tls_allow_invalid_hostnames: invalid.checked, notes: notes.value };
    }
    const testBtn = button("Test connection", async () => {
      testBtn.disabled = true;
      testBtn.textContent = "Testing…";
      result.textContent = "";
      result.appendChild(h("div", { class: "loading", text: "Connecting (up to 8 s)" }));
      try {
        const body = t ? { target_id: t.id, target: payload() } : { target: payload() };
        const r = await api.post("api/targets/test", body);
        result.textContent = "";
        result.appendChild(testView(r));
      } catch (err) {
        result.textContent = "";
        result.appendChild(errorBlock(err));
      }
      testBtn.disabled = false;
      testBtn.textContent = "Test connection";
    });
    const save = button(t ? "Save target" : "Add target", async () => {
      problems.textContent = "";
      try {
        const saved = t ? await api.put(`api/targets/${t.id}`, payload()) : await api.post("api/targets", payload());
        toast(t ? `Target saved: ${saved.name}` : `Target added: ${saved.name}`);
        try { const r = await api.post("api/targets/test", { target_id: saved.id }); saved.last_test = r; } catch (e) { /* shown on next view */ }
        await load();
        edit(targets.find((x) => x.id === saved.id) || saved);
      } catch (err) { problems.appendChild(errorBlock(err)); }
    }, "primary");
    const actions = [testBtn, h("span", { class: "spacer" })];
    if (t) {
      actions.push(button("Clean up data", () => cleanup(t), "danger"));
      actions.push(button("Delete", () => dialog({ title: "Delete target", body: `Delete ${t.name}? Runs that used it keep their records.`, confirm: "Delete target", danger: true,
        onConfirm: async () => { await api.del(`api/targets/${t.id}`); toast("Target deleted"); editHost.textContent = ""; editing = null; formOpen = false; await load(); } }), "danger"));
    }
    actions.push(save);
    editHost.appendChild(h("section", { class: "panel" }, [
      h("header", null, [h("h2", { text: t ? `Edit ${t.name}` : "Add target" }), button("Close", () => { editHost.textContent = ""; editing = null; formOpen = false; drawList(); }, "quiet small")]),
      h("div", { class: "body" }, [
        field("Name", name),
        field("Connection string", uri, t && t.has_password ? "The saved password shows as ****. Leave it to keep the saved one, or type a new one." : "Include user and password. Percent-encode special characters in the password."),
        h("div", { class: "inline-fields" }, [field("TLS", tls), field("CA file", ca, "For a private CA."), field("Client certificate", cert, "PEM with key, for x.509.")]),
        h("div", { class: "field" }, h("label", { class: "check", for: "t-invalid" }, [invalid, h("span", { text: "Allow certificate host name mismatch (testing only)" })])),
        field("Notes", notes),
        problems, result, h("div", { class: "row", style: null }, actions),
      ]),
    ]));
    name.focus();
  }

  async function cleanup(t) {
    const db = input({ id: "c-db", value: "ballast_bench", class: "mono" });
    /** @type {any} */
    let found = null;
    const preview = h("div", { class: "small", "aria-live": "polite" });
    const look = button("Show what would be dropped", async () => {
      preview.textContent = "Checking\u2026";
      try {
        found = await api.get(`api/targets/${t.id}/datasets?database=${encodeURIComponent(db.value.trim())}`);
        preview.textContent = found.ballast_collections.length
          ? `Will drop: ${found.ballast_collections.join(", ")}. Kept: ${found.foreign_collections.join(", ") || "no other collections"}.`
          : "No Ballast collections in this database. Nothing would be dropped.";
      } catch (err) { preview.textContent = err.title + (err.detail ? ": " + err.detail : ""); }
    }, "small");
    const body = h("div", null, [h("p", { text: "Drops only collections Ballast created, as listed in its _ballast_collections registry. Other collections are never touched." }), field("Database", db), look, preview]);
    const next = await dialog({ title: `Clean up data on ${t.name}`, body, confirm: "Continue" });
    if (!next) return;
    const name = db.value.trim();
    await dialog({
      title: `Drop Ballast collections in ${name}`,
      body: h("p", { text: `This permanently drops every collection Ballast created in ${name} on ${t.name}${found && found.ballast_collections.length ? ` (${found.ballast_collections.join(", ")})` : ""}.` }),
      confirm: "Drop Ballast collections", danger: true, typeToConfirm: name,
      onConfirm: async () => {
        const r = await api.post(`api/targets/${t.id}/cleanup`, { database: name, confirm: name });
        toast(r.dropped.length ? `Collections dropped: ${r.dropped.join(", ")}` : "Nothing to drop");
      },
    });
  }

  await load();
  if (!targets.length) edit(null);
  return null;
}
