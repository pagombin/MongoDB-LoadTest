/* Workload profile editing: the operation-mix editor and the full profile form. */
import { h, F, input, select, segmented, field, button } from "../ui.js";

export const OPS = ["find", "range", "insert", "update", "upsert", "delete", "aggregate", "find_and_modify"];

export function opsLabel(p) {
  const mix = p.operations || {};
  const total = OPS.reduce((a, k) => a + (Number(mix[k]) || 0), 0) || 1;
  return OPS.filter((k) => mix[k] > 0).map((k) => `${Math.round((mix[k] / total) * 100)}% ${F.opName(k).toLowerCase()}`).join(", ");
}

export function mixEditor(p, onChange) {
  p.operations = p.operations || {};
  const mix = p.operations;
  OPS.forEach((k) => { if (mix[k] === undefined) mix[k] = 0; });
  const wrap = h("div");
  const pcts = {};
  function refresh() {
    const total = OPS.reduce((a, k) => a + (Number(mix[k]) || 0), 0);
    OPS.forEach((k) => { pcts[k].textContent = total ? `${Math.round(((Number(mix[k]) || 0) / total) * 100)}%` : "0%"; });
    warn.textContent = total > 0 ? "" : "At least one operation needs a weight above 0.";
  }
  OPS.forEach((k) => {
    const id = `mix-${k}`;
    const range = h("input", { type: "range", min: "0", max: "100", step: "1", id, value: String(mix[k] || 0), "aria-describedby": `${id}-pct` });
    range.addEventListener("input", () => { mix[k] = Number(range.value); refresh(); onChange && onChange(); });
    pcts[k] = h("span", { class: "pct", id: `${id}-pct` });
    wrap.appendChild(h("div", { class: "mix-row" }, [h("label", { for: id, text: F.opName(k), style: null }), range, pcts[k]]));
  });
  const warn = h("div", { class: "problem small", role: "status" });
  wrap.appendChild(warn);
  wrap.appendChild(h("p", { class: "xs muted", text: "Weights are relative; the percentage shows each operation's share. Find by key and update target existing _id keys; range and aggregate scan a key range; upsert may create keys." }));
  refresh();
  return wrap;
}

function num(obj, key, attrs) {
  const el = input(Object.assign({ type: "number", class: "num-input", value: obj[key] === null || obj[key] === undefined ? "" : String(obj[key]) }, attrs || {}));
  el.addEventListener("input", () => { obj[key] = el.value === "" ? null : Number(el.value); });
  return el;
}

function text(obj, key, attrs) {
  const el = input(Object.assign({ value: obj[key] || "" }, attrs || {}));
  el.addEventListener("input", () => { obj[key] = el.value; });
  return el;
}

function bool(obj, key, label) {
  const id = "b-" + key + Math.random().toString(36).slice(2, 6);
  const cb = h("input", { type: "checkbox", id });
  cb.checked = !!obj[key];
  cb.addEventListener("change", () => { obj[key] = cb.checked; });
  return h("div", { class: "field" }, h("label", { class: "check", for: id }, [cb, h("span", { text: label })]));
}

function choose(obj, key, options, attrs) {
  const el = select(options.map((o) => (typeof o === "object" ? o : { value: o, label: o })), obj[key] === null || obj[key] === undefined ? "" : obj[key], attrs || {});
  el.addEventListener("change", () => { obj[key] = el.value === "" ? null : el.value; });
  return el;
}

function section(title, children) {
  return h("fieldset", null, [h("legend", { text: title }), ...children]);
}

export function profileForm(p) {
  p.document = p.document || {};
  p.keys = p.keys || {};
  p.dataset = p.dataset || {};
  p.dataset.load = p.dataset.load || {};
  p.driver = p.driver || {};
  p.driver.write_concern = p.driver.write_concern || {};
  p.load = p.load || {};
  p.schedule = p.schedule || {};
  p.retry = p.retry || {};
  p.sampling = p.sampling || {};
  const d = p.driver, wc = d.write_concern;
  const wSel = select([{ value: "majority", label: "majority" }, { value: "1", label: "1" }, { value: "0", label: "0 (unacknowledged)" }, { value: "2", label: "2" }, { value: "3", label: "3" }], String(wc.w ?? "majority"), { id: "pf-w" });
  wSel.addEventListener("change", () => { wc.w = wSel.value === "majority" ? "majority" : Number(wSel.value); });
  const jSel = select([{ value: "", label: "server default" }, { value: "true", label: "true" }, { value: "false", label: "false" }], wc.j === null || wc.j === undefined ? "" : String(wc.j), { id: "pf-j" });
  jSel.addEventListener("change", () => { wc.j = jSel.value === "" ? null : jSel.value === "true"; });
  const docMode = segmented([{ value: "simple", label: "Simple (size and field count)" }, { value: "schema", label: "Schema (field definitions)" }], p.document.mode || "simple", (v) => { p.document.mode = v; schemaBox.hidden = v !== "schema"; }, "Document shape");
  const schemaText = h("textarea", { id: "pf-schema", rows: "8", spellcheck: "false" });
  schemaText.value = JSON.stringify(p.document.schema_fields || [{ name: "tenant_id", field_type: "string", length: 8 }, { name: "total", field_type: "float", min_value: 1, max_value: 500 }, { name: "status", field_type: "choice", choices: ["new", "paid", "shipped"] }], null, 2);
  schemaText.addEventListener("input", () => { try { p.document.schema_fields = JSON.parse(schemaText.value); schemaProblem.textContent = ""; } catch (e) { schemaProblem.textContent = "Not valid JSON yet: " + e.message; } });
  const schemaProblem = h("div", { class: "problem" });
  const schemaBox = h("div", null, [field("Schema fields (JSON)", schemaText, "Same field types as the legacy schema: string, int, float, decimal, bool, choice, datetime, objectid, uuid, faker, object, array, null."), schemaProblem]);
  schemaBox.hidden = (p.document.mode || "simple") !== "schema";
  if (schemaBox.hidden && !p.document.schema_fields) p.document.schema_fields = null;
  const indexes = h("textarea", { id: "pf-indexes", rows: "3", spellcheck: "false" });
  indexes.value = JSON.stringify(p.dataset.indexes || [], null, 2);
  const indexProblem = h("div", { class: "problem" });
  indexes.addEventListener("input", () => { try { p.dataset.indexes = JSON.parse(indexes.value); indexProblem.textContent = ""; } catch (e) { indexProblem.textContent = "Not valid JSON yet: " + e.message; } });
  const untilStop = p.schedule.steady_seconds === null || p.schedule.steady_seconds === undefined;
  const steadyInput = num(p.schedule, "steady_seconds", { id: "pf-steady", min: "1", placeholder: "until stopped" });
  if (untilStop) steadyInput.value = "";
  return h("div", { class: "stack" }, [
    section("General", [h("div", { class: "inline-fields" }, [field("Name", text(p, "name", { id: "pf-name", maxlength: "80" })), field("Seed", num(p, "seed", { id: "pf-seed", min: "0" }), "Same seed, same key and document sequence.")]),
      field("Description", text(p, "description", { id: "pf-desc", maxlength: "2000" }))]),
    section("Operations", [mixEditor(p)]),
    section("Documents", [h("div", { class: "field" }, docMode), h("div", { class: "inline-fields" }, [field("Size (bytes)", num(p.document, "size_bytes", { id: "pf-size", min: "64" }), "Simple shape only."), field("Fields", num(p.document, "fields", { id: "pf-fields", min: "1", max: "200" }))]), schemaBox]),
    section("Keys", [h("div", { class: "inline-fields" }, [
      field("Distribution", choose(p.keys, "distribution", ["uniform", "zipfian", "sequential"], { id: "pf-dist" }), "Zipfian concentrates load on a few hot keys."),
      field("Zipf exponent", num(p.keys, "zipf_theta", { id: "pf-theta", step: "0.01", min: "0.01" })),
      field("Range size", num(p.keys, "range_size", { id: "pf-range", min: "1" }), "Documents per range find or aggregate."),
    ])]),
    section("Dataset", [h("div", { class: "inline-fields" }, [
      field("Database", text(p.dataset, "database", { id: "pf-db", class: "mono" })), field("Collections", num(p.dataset, "collections", { id: "pf-colls", min: "1" })),
      field("Collection prefix", text(p.dataset, "collection_prefix", { id: "pf-prefix", class: "mono" })),
      field("Load documents", num(p.dataset.load, "documents", { id: "pf-loaddocs", min: "0" }), "Per collection, before the run."),
      field("Or load size (MB)", num(p.dataset.load, "size_mb", { id: "pf-loadmb", min: "0", step: "0.1" }), "Per collection."),
    ]), field("Secondary indexes (JSON)", indexes, "For example [{\"keys\": {\"v\": 1}}]. _id is always indexed."), indexProblem]),
    section("Driver", [h("div", { class: "inline-fields" }, [
      field("Write concern w", wSel), field("Journal (j)", jSel), field("wtimeout (ms)", num(wc, "wtimeout_ms", { id: "pf-wt", min: "0" })),
      field("Read concern", choose(d, "read_concern", [{ value: "", label: "server default" }, "local", "available", "majority", "linearizable", "snapshot"], { id: "pf-rc" })),
      field("Read preference", choose(d, "read_preference", ["primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest"], { id: "pf-rp" })),
      field("maxPoolSize", num(d, "max_pool_size", { id: "pf-maxpool", min: "1" })), field("minPoolSize", num(d, "min_pool_size", { id: "pf-minpool", min: "0" })),
      field("serverSelectionTimeoutMS", num(d, "server_selection_timeout_ms", { id: "pf-sst", min: "100" })), field("connectTimeoutMS", num(d, "connect_timeout_ms", { id: "pf-ct", min: "100" })),
      field("socketTimeoutMS", num(d, "socket_timeout_ms", { id: "pf-st", min: "0" }), "0 means no limit."), field("Operation timeout (ms)", num(d, "operation_timeout_ms", { id: "pf-ot", min: "1" }), "timeoutMS; empty means none."),
      field("heartbeatFrequencyMS", num(d, "heartbeat_frequency_ms", { id: "pf-hb", min: "500" })),
    ]), bool(d, "retry_writes", "Retryable writes"), bool(d, "retry_reads", "Retryable reads")]),
    section("Load", [h("div", { class: "inline-fields" }, [
      field("Model", choose(p.load, "model", [{ value: "open", label: "open (target rate)" }, { value: "closed", label: "closed (workers)" }], { id: "pf-model" })),
      field("Rate (ops/s)", num(p.load, "rate", { id: "pf-rate", min: "1" }), "Open model."), field("Concurrency", num(p.load, "concurrency", { id: "pf-conc", min: "1" }), "Workers, or max in flight."),
      field("Think time (ms)", num(p.load, "think_time_ms", { id: "pf-think", min: "0" }), "Closed model."), field("Generator processes", num(p.load, "processes", { id: "pf-procs", min: "1", max: "64" })),
    ])]),
    section("Schedule", [h("div", { class: "inline-fields" }, [
      field("Warm-up (s)", num(p.schedule, "warmup_seconds", { id: "pf-warm", min: "0" })), field("Steady (s)", steadyInput, "Empty runs until stopped."),
      field("Drain timeout (s)", num(p.schedule, "drain_timeout_seconds", { id: "pf-drain", min: "1" }), "How long in-flight operations get at stop."),
    ])]),
    section("Application retries", [h("div", { class: "inline-fields" }, [
      field("Attempts", num(p.retry, "max_attempts", { id: "pf-att", min: "1", max: "20" }), "1 means only the driver's own retry."),
      field("Initial backoff (ms)", num(p.retry, "initial_backoff_ms", { id: "pf-ib", min: "0" })), field("Max backoff (ms)", num(p.retry, "max_backoff_ms", { id: "pf-mb", min: "0" })),
    ])]),
    section("Server sampling", [bool(p.sampling, "enabled", "Poll hello, serverStatus and replSetGetStatus (needs clusterMonitor; degrades without it)"), h("div", { class: "inline-fields" }, [field("Interval (s)", num(p.sampling, "interval_seconds", { id: "pf-si", min: "1" }))])]),
  ]);
}

export function cleanProfile(p) {
  const out = JSON.parse(JSON.stringify(p));
  if (out.document && out.document.mode !== "schema") out.document.schema_fields = null;
  if (out.schedule && (out.schedule.steady_seconds === 0 || out.schedule.steady_seconds === "")) out.schedule.steady_seconds = null;
  return out;
}

export { button };
