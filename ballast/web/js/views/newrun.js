import { api } from "../api.js";
import { navigate, setTitle } from "../app.js";
import { h, F, pageHead, button, empty, errorBlock, loading, toast, input, select, segmented, field } from "../ui.js";
import { mixEditor, opsLabel, sizingNotes } from "./profile-form.js";

const STEPS = ["Target", "Workload", "Schedule", "Review"];

function clone(o) { return JSON.parse(JSON.stringify(o)); }

export async function render(root) {
  setTitle("New run");
  root.appendChild(pageHead("New run", "Pick where to send load, what to send, and for how long."));
  const host = h("div");
  root.appendChild(host);
  host.appendChild(loading("Loading targets and profiles"));
  let targets, profiles;
  try {
    [targets, profiles] = await Promise.all([api.get("api/targets"), api.get("api/profiles")]);
  } catch (err) {
    host.textContent = "";
    host.appendChild(errorBlock(err, () => navigate("#/new")));
    return null;
  }
  host.textContent = "";
  if (!targets.length) {
    host.appendChild(h("section", { class: "panel" }, empty("Add a target first", "A target is a MongoDB connection string. Ballast tests it before you start a run.", [h("a", { class: "btn primary", href: "#/targets", text: "Add target" })])));
    return null;
  }
  const preferred = profiles.find((p) => p.body.load && p.body.load.model === "open") || profiles[0];
  const s = {
    step: 0,
    targetId: (targets.find((t) => t.last_test && t.last_test.ok) || targets[0]).id,
    profileId: preferred ? preferred.id : null,
    profile: preferred ? clone(preferred.body) : null,
    name: "",
    ack: false,
    preflight: null,
    preflightError: null,
    testing: false,
  };
  if (!s.profile) s.profile = { name: "custom", operations: { find: 60, update: 30, insert: 10 }, load: { model: "open", rate: 1000, concurrency: 64 }, schedule: { warmup_seconds: 30, steady_seconds: null }, dataset: { database: "ballast_bench", collections: 1, collection_prefix: "bench", load: { documents: 100000 } } };

  const stepsNav = h("ol", { class: "steps", "aria-label": "Steps" });
  const body = h("div");
  host.appendChild(stepsNav);
  host.appendChild(body);

  function target() { return targets.find((t) => t.id === s.targetId); }

  function drawSteps() {
    stepsNav.textContent = "";
    STEPS.forEach((name, i) => {
      const li = h("li", { class: i < s.step ? "done" : "", "aria-current": i === s.step ? "step" : null }, h("button", { type: "button", disabled: i > s.step ? true : null, onclick: () => { s.step = i; draw(); } }, [h("span", { class: "n", text: String(i + 1) }), " ", name]));
      stepsNav.appendChild(li);
    });
  }

  function nav(nextLabel, onNext, extra) {
    return h("div", { class: "row end", style: null }, [
      s.step > 0 ? button("Back", () => { s.step--; draw(); }) : null,
      extra || null,
      nextLabel ? button(nextLabel, onNext, "primary") : null,
    ]);
  }

  function testResult(t) {
    const r = t.last_test;
    if (!r) return h("span", { class: "muted small", text: "Not tested yet" });
    if (r.ok) return h("span", { class: "small" }, [h("span", { class: "chip ok", text: "Reachable" }), ` ${r.server_version || ""}, ${r.topology}${r.set_name ? " " + r.set_name : ""}, primary ${r.primary || "none"} (${F.ago(r.tested_at)})`]);
    return h("span", { class: "small" }, [h("span", { class: "chip fail", text: r.title }), ` ${F.ago(r.tested_at)}`]);
  }

  function stepTarget() {
    const list = h("ul", { class: "choice-list" }, targets.map((t) => {
      const radio = h("input", { type: "radio", name: "target", value: t.id });
      radio.checked = t.id === s.targetId;
      radio.addEventListener("change", () => { s.targetId = t.id; s.preflight = null; });
      const testBtn = button("Test", async (e) => {
        e.preventDefault();
        testBtn.disabled = true;
        testBtn.textContent = "Testing…";
        try { t.last_test = await api.post("api/targets/test", { target_id: t.id }); } catch (err) { toast(err.title, "fail"); }
        draw();
      }, "small");
      return h("li", null, h("label", null, [radio, h("div", null, [h("div", { class: "choice-name", text: t.name }), h("div", { class: "mono xs muted", text: t.uri }), testResult(t)]), testBtn]));
    }));
    const db = input({ id: "db-name", value: s.profile.dataset.database || "ballast_bench", class: "mono", spellcheck: "false" });
    db.addEventListener("input", () => { s.profile.dataset.database = db.value.trim(); s.preflight = null; });
    const prefix = input({ id: "coll-prefix", value: s.profile.dataset.collection_prefix || "bench", class: "mono", spellcheck: "false" });
    prefix.addEventListener("input", () => { s.profile.dataset.collection_prefix = prefix.value.trim(); s.preflight = null; });
    const colls = input({ id: "coll-count", type: "number", min: "1", max: "256", value: String(s.profile.dataset.collections || 1), class: "num-input" });
    colls.addEventListener("input", () => { s.profile.dataset.collections = Number(colls.value); s.preflight = null; });
    return [
      h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Target" }), h("a", { href: "#/targets", class: "small", text: "Manage targets" })]), h("div", { class: "body flush" }, list)]),
      h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Benchmark data" })), h("div", { class: "body" }, [
        h("div", { class: "inline-fields" }, [
          field("Database", db, "Ballast writes only here."),
          field("Collection prefix", prefix, "Collections are named prefix_1, prefix_2, …"),
          field("Collections", colls, "Load is spread evenly."),
        ]),
      ])]),
      nav("Next: workload", () => { if (!s.profile.dataset.database) { toast("Enter a database name", "fail"); return; } s.step = 1; draw(); }),
    ];
  }

  function stepWorkload() {
    const profSel = select(profiles.map((p) => ({ value: p.id, label: p.name })), s.profileId, { id: "profile-select" });
    profSel.addEventListener("change", () => {
      const p = profiles.find((x) => x.id === profSel.value);
      const ds = s.profile.dataset;
      s.profileId = p.id;
      s.profile = clone(p.body);
      s.profile.dataset = Object.assign({}, s.profile.dataset, { database: ds.database, collection_prefix: ds.collection_prefix, collections: ds.collections });
      draw();
    });
    const p = s.profile;
    p.load = p.load || {};
    const modelSeg = segmented([{ value: "open", label: "Open model (target rate)" }, { value: "closed", label: "Closed model (workers)" }], p.load.model || "open", (v) => { p.load.model = v; draw(); }, "Load model");
    const rate = input({ id: "rate", type: "number", min: "1", value: String(p.load.rate || 1000), class: "num-input" });
    rate.addEventListener("input", () => { p.load.rate = Number(rate.value); });
    const conc = input({ id: "conc", type: "number", min: "1", value: String(p.load.concurrency || 64), class: "num-input" });
    conc.addEventListener("input", () => { p.load.concurrency = Number(conc.value); });
    const procs = input({ id: "procs", type: "number", min: "1", max: "64", value: String(p.load.processes || 1), class: "num-input" });
    procs.addEventListener("input", () => { p.load.processes = Number(procs.value); });
    const think = input({ id: "think", type: "number", min: "0", value: String(p.load.think_time_ms || 0), class: "num-input" });
    think.addEventListener("input", () => { p.load.think_time_ms = Number(think.value); });
    const open = (p.load.model || "open") === "open";
    const loadFields = h("div", { class: "inline-fields" }, open ? [
      field("Target rate (ops/s)", rate, "Sent on schedule whatever the response time, so stalls show as latency."),
      field("Max in flight", conc, "Upper bound on concurrent operations."),
      field("Generator processes", procs, "Use more on large hosts above about 8,000 ops/s."),
    ] : [
      field("Workers", conc, "Each sends its next operation when the previous one returns."),
      field("Think time (ms)", think, "Pause between a worker's operations."),
      field("Generator processes", procs, "Workers are split across processes."),
    ]);
    const notes = h("div", { class: "notes", role: "status", "aria-live": "polite", "data-sizing": "" });
    const drawNotes = () => { notes.textContent = ""; sizingNotes(p).forEach((n) => notes.appendChild(h("p", { class: "note " + n.kind, text: n.text }))); };
    drawNotes();
    const loadBody = h("div", { class: "body" }, [h("div", { class: "field" }, modelSeg), loadFields, notes]);
    loadBody.addEventListener("input", drawNotes);
    return [
      h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Workload profile" }), h("a", { href: "#/profiles", class: "small", text: "Manage profiles" })]), h("div", { class: "body" }, [
        field("Start from", profSel, (profiles.find((x) => x.id === s.profileId) || {}).body ? (profiles.find((x) => x.id === s.profileId).body.description || "") : ""),
        h("p", { class: "small muted", text: "Changes here apply to this run only. Save them as a profile from the Workload profiles page." }),
      ])]),
      h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Load model" })), loadBody]),
      h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Operation mix" }), h("span", { class: "meta", text: opsLabel(p) })]), h("div", { class: "body" }, mixEditor(p, drawNotes))]),
      nav("Next: schedule", () => { s.step = 2; draw(); }),
    ];
  }

  function stepSchedule() {
    const p = s.profile;
    p.schedule = p.schedule || {};
    p.dataset.load = p.dataset.load || {};
    const warm = input({ id: "warmup", type: "number", min: "0", value: String(p.schedule.warmup_seconds ?? 30), class: "num-input" });
    warm.addEventListener("input", () => { p.schedule.warmup_seconds = Number(warm.value); });
    const untilStop = p.schedule.steady_seconds === null || p.schedule.steady_seconds === undefined;
    const steady = input({ id: "steady", type: "number", min: "1", value: String(p.schedule.steady_seconds || 600), class: "num-input", disabled: untilStop ? true : null });
    steady.addEventListener("input", () => { p.schedule.steady_seconds = Number(steady.value); });
    const durSeg = segmented([{ value: "until", label: "Until I stop it" }, { value: "fixed", label: "Fixed duration" }], untilStop ? "until" : "fixed", (v) => {
      p.schedule.steady_seconds = v === "until" ? null : Number(steady.value) || 600;
      draw();
    }, "Steady duration");
    const docs = input({ id: "load-docs", type: "number", min: "0", value: String(p.dataset.load.documents || 0), class: "num-input" });
    docs.addEventListener("input", () => { p.dataset.load.documents = Number(docs.value); });
    const ramp = p.schedule.ramp || (p.schedule.ramp = []);
    const rampList = h("div");
    function drawRamp() {
      rampList.textContent = "";
      ramp.forEach((st, i) => {
        const lv = input({ type: "number", min: "1", value: String(st.level), class: "num-input", "aria-label": `Step ${i + 1} level` });
        lv.addEventListener("input", () => { st.level = Number(lv.value); });
        const sec = input({ type: "number", min: "1", value: String(st.seconds), class: "num-input", "aria-label": `Step ${i + 1} seconds` });
        sec.addEventListener("input", () => { st.seconds = Number(sec.value); });
        rampList.appendChild(h("div", { class: "row", style: null }, [h("span", { class: "small mono", text: `Step ${i + 1}` }), lv, h("span", { class: "small muted", text: (p.load.model || "open") === "open" ? "ops/s for" : "workers for" }), sec, h("span", { class: "small muted", text: "s" }), button("Remove", () => { ramp.splice(i, 1); drawRamp(); }, "quiet small")]));
      });
      rampList.appendChild(button("Add ramp step", () => {
        const target = (p.load.model || "open") === "open" ? p.load.rate || 1000 : p.load.concurrency || 32;
        ramp.push({ level: Math.max(1, Math.round(target * (ramp.length + 1) / 4)), seconds: 60 });
        drawRamp();
      }, "small"));
    }
    drawRamp();
    p.guardrails = p.guardrails || {};
    p.slo = p.slo || {};
    const numIn = (obj, key, id, attrs) => {
      const el = input(Object.assign({ id, type: "number", class: "num-input", step: "any", placeholder: "off", value: obj[key] === null || obj[key] === undefined ? "" : String(obj[key]) }, attrs || {}));
      el.addEventListener("input", () => { obj[key] = el.value === "" ? null : Number(el.value); });
      return el;
    };
    const safety = h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Guardrails and pass/fail" }), h("span", { class: "meta", text: "optional" })]), h("div", { class: "body" }, [
      h("p", { class: "small muted", text: "Guardrails stop the run by themselves when the cluster is clearly being hurt. Pass/fail criteria are checked on the finished run and shown on its report." }),
      h("div", { class: "inline-fields" }, [
        field("Stop above failed (%)", numIn(p.guardrails, "max_error_percent", "g-err", { min: "0.01", max: "100" })),
        field("Stop above p99 (ms)", numIn(p.guardrails, "max_p99_ms", "g-p99", { min: "0.1" }), "For 10 seconds in a row by default."),
        field("Pass if p99 at most (ms)", numIn(p.slo, "p99_ms", "s-p99", { min: "0", placeholder: "" })),
        field("Pass if failed at most (%)", numIn(p.slo, "error_percent", "s-err", { min: "0", max: "100", placeholder: "" })),
      ]),
    ])]);
    return [
      h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Data load" })), h("div", { class: "body" }, [
        h("div", { class: "inline-fields" }, [field("Documents per collection", docs, "Inserted before the measured run if the collection has fewer. 0 skips loading.")]),
      ])]),
      h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Schedule" })), h("div", { class: "body" }, [
        h("div", { class: "inline-fields" }, [field("Warm-up (s)", warm, "Runs at the first level and is left out of every summary.")]),
        h("fieldset", null, [h("legend", { text: "Ramp steps (optional)" }), rampList]),
        h("fieldset", null, [h("legend", { text: "Steady phase" }), h("div", { class: "field" }, durSeg), h("div", { class: "inline-fields" }, [field("Steady duration (s)", steady)])]),
      ])]),
      safety,
      nav("Next: review", () => { s.step = 3; s.preflight = null; draw(); }),
    ];
  }

  function reviewText(pre) {
    const p = s.profile, t = target();
    const lt = t.last_test && t.last_test.ok ? t.last_test : null;
    const colls = p.dataset.collections || 1;
    const names = colls === 1 ? `${p.dataset.collection_prefix}_1` : `${p.dataset.collection_prefix}_1 to ${p.dataset.collection_prefix}_${colls}`;
    const paras = [];
    paras.push(`${"Ballast"} will connect to ${t.name}${lt ? ` (${lt.topology}${lt.members ? ", " + lt.members.length + " members" : ""}, primary ${lt.primary || "unknown"})` : ""} and write only to database ${p.dataset.database}, collection${colls === 1 ? "" : "s"} ${names}.`);
    if (pre && pre.load_documents > 0) {
      const have = Object.values(pre.document_counts || {});
      paras.push(`It will first make sure each collection has ${F.int(pre.load_documents)} documents of about ${F.bytes((p.document && p.document.size_bytes) || 512)}${have.length ? ` (they now have ${have.map(F.int).join(", ")})` : ""}. Loading is not measured.`);
    }
    const sched = p.schedule || {};
    const ramp = (sched.ramp || []).map((r) => `${F.num(r.level, 0)} for ${F.seconds(r.seconds)}`).join(", then ");
    paras.push(`Then it will send ${pre ? pre.summary : ""}.`);
    paras.push(`${sched.warmup_seconds ? `Warm-up ${F.seconds(sched.warmup_seconds)} (not counted)` : "No warm-up"}${ramp ? `, ramp ${ramp}` : ""}, then steady ${sched.steady_seconds ? "for " + F.seconds(sched.steady_seconds) : "until you stop it"}.`);
    paras.push(`Write concern ${((p.driver || {}).write_concern || {}).w || "majority"}, read preference ${(p.driver || {}).read_preference || "primary"}, retryable writes ${(p.driver || {}).retry_writes === false ? "off" : "on"}.`);
    const g = p.guardrails || {};
    const stops = [];
    if (g.max_error_percent) stops.push(`more than ${F.num(g.max_error_percent, 2)}% of operations fail`);
    if (g.max_p99_ms) stops.push(`p99 goes above ${F.num(g.max_p99_ms, 1)} ms`);
    paras.push(stops.length ? `It will stop by itself if ${stops.join(" or ")} for ${g.sustain_seconds || 10} seconds in a row.` : "No guardrail is set; the run continues whatever happens until you stop it or its schedule ends.");
    const slo = Object.entries(p.slo || {}).filter(([, v]) => v !== null && v !== undefined);
    if (slo.length) paras.push(`The report will mark it passed or failed against ${slo.length} criteri${slo.length === 1 ? "on" : "a"}.`);
    return paras;
  }

  function stepReview() {
    const out = [];
    if (!s.preflight && !s.preflightError) {
      api.post("api/preflight", { target_id: s.targetId, profile: s.profile }).then((pre) => { s.preflight = pre; draw(); }).catch((err) => { s.preflightError = err; draw(); });
      return [loading("Checking the target and database")];
    }
    if (s.preflightError) {
      const err = s.preflightError;
      return [errorBlock(err, () => { s.preflightError = null; draw(); }), nav(null, null)];
    }
    const pre = s.preflight;
    out.push(h("section", { class: "panel" }, [h("header", null, h("h2", { text: "What will happen" })), h("div", { class: "body review-text" }, reviewText(pre).map((t) => h("p", { text: t })))]));
    const ack = h("input", { type: "checkbox", id: "ack-foreign" });
    ack.checked = s.ack;
    ack.addEventListener("change", () => { s.ack = ack.checked; });
    if (pre.name_clash && pre.name_clash.length) {
      out.push(window.BallastReport.notice("fail", "Collection name clash", `${pre.name_clash.join(", ")} already exist in ${pre.database} and were not created by Ballast.`, "Go back and choose another collection prefix or database."));
    } else if (pre.writes && pre.foreign_collections.length) {
      out.push(h("div", { class: "notice warn", role: "alert" }, h("div", null, [
        h("div", { class: "notice-title", text: `${pre.database} already contains ${pre.foreign_collections.length} collection${pre.foreign_collections.length === 1 ? "" : "s"} Ballast did not create` }),
        h("div", { class: "small mono", text: pre.foreign_collections.slice(0, 12).join(", ") + (pre.foreign_collections.length > 12 ? ", …" : "") }),
        h("div", { class: "small", style: null, text: "Ballast writes only to its own collections, but they share this database's cache, disk and oplog, so this run can slow the applications using it." }),
        h("label", { class: "check", for: "ack-foreign" }, [ack, h("span", { text: "I understand the risk and want to write to this database" })]),
      ])));
    }
    const nameIn = input({ id: "run-name", value: s.name || `${s.profile.name} ${new Date().toISOString().slice(5, 16).replace("T", " ")}`, maxlength: "120" });
    nameIn.addEventListener("input", () => { s.name = nameIn.value; });
    s.name = nameIn.value;
    const problems = h("div");
    out.push(h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Run" })), h("div", { class: "body" }, [field("Run name", nameIn, "Shown in the runs list and report. Include what you are about to change."), problems])]));
    const startBtn = button("Start run", async () => {
      if (pre.writes && pre.foreign_collections.length && !s.ack) { toast("Confirm the warning about existing collections first", "fail"); ack.focus(); return; }
      startBtn.disabled = true;
      startBtn.textContent = "Starting…";
      problems.textContent = "";
      try {
        const run = await api.post("api/runs", { name: s.name.trim() || "run", target_id: s.targetId, profile: s.profile, acknowledge_foreign: s.ack });
        toast("Run started");
        navigate(`#/runs/${run.id}/live`);
      } catch (err) {
        startBtn.disabled = false;
        startBtn.textContent = "Start run";
        problems.appendChild(errorBlock(err));
      }
    }, "primary");
    if (pre.name_clash && pre.name_clash.length) startBtn.disabled = true;
    out.push(nav(null, null, startBtn));
    return out;
  }

  function draw() {
    drawSteps();
    body.textContent = "";
    const parts = [stepTarget, stepWorkload, stepSchedule, stepReview][s.step]();
    parts.forEach((p) => body.appendChild(p));
    const first = body.querySelector("input, select, button");
    if (first && s.step !== 3) first.focus({ preventScroll: true });
  }
  draw();
  return null;
}
