import { api } from "../api.js";
import { setTitle } from "../app.js";
import { h, F, pageHead, button, errorBlock, loading, download, segmented } from "../ui.js";

export async function render(root) {
  setTitle("System");
  root.appendChild(pageHead("System", "Health, version, storage and effective configuration of this server.", [button("Download log bundle", () => download("api/system/logs.zip"))]));
  const host = h("div");
  root.appendChild(host);
  let level = "warning";
  async function load() {
    host.textContent = "";
    host.appendChild(loading("Loading system information"));
    let s;
    try { s = await api.get(`api/system?level=${level}`); } catch (err) { host.textContent = ""; host.appendChild(errorBlock(err, load)); return; }
    host.textContent = "";
    const a = s.about, st = s.storage;
    s.warnings.forEach((w) => host.appendChild(window.BallastReport.notice("warn", "Configuration warning", w, null)));
    if (s.tls_mode === "off") host.appendChild(window.BallastReport.notice("warn", "HTTPS is off", "Traffic between your browser and this server is not encrypted.", "Set tls.mode to self-signed or files, or keep the listen address on 127.0.0.1 and use an SSH tunnel."));
    const R = window.BallastReport;
    host.appendChild(h("div", { class: "figures", style: null }, [
      R.figure("Health", s.health, "", `pid ${s.pid}`), R.figure("Uptime", F.duration(s.uptime_seconds * 1000), "", ""), R.figure("Runs", F.int(st.runs), "", "stored"),
      R.figure("Data", F.bytes(st.database_bytes), "", "ballast.db"), R.figure("Logs", F.bytes(st.logs_bytes), "", ""), R.figure("Free disk", F.bytes(st.disk_free_bytes), "", `of ${F.bytes(st.disk_total_bytes)}`, st.disk_free_bytes < 1024 * 1024 * 1024 ? "warnish" : ""),
    ]));
    const kv = (pairs) => h("table", { class: "kv" }, h("tbody", null, pairs.map(([k, v]) => h("tr", null, [h("th", { text: k }), h("td", { class: "mono small", text: v === null || v === undefined ? "–" : String(v) })]))));
    host.appendChild(h("div", { class: "grid-2", style: null }, [
      h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Version" })), h("div", { class: "body" }, kv([["Product", `${a.product} ${a.version}`], ["Build commit", a.commit], ["Build date", a.built], ["Driver", a.driver], ["Python", a.python], ["Install directory", s.home], ["Configuration file", s.config_file || "none (defaults)"]]))]),
      h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Listener and certificate" })), h("div", { class: "body" }, kv([
        ["Listening", `${s.scheme}://${s.listen}`], ["TLS mode", s.tls_mode], ["Certificate hosts", s.certificate && s.certificate.hosts ? s.certificate.hosts.join(", ") : null],
        ["SHA-256", s.certificate ? s.certificate.sha256 : null], ["Expires", s.certificate ? s.certificate.not_after : null], ["Access gate", a.access_gate ? "on" : "off"], ["Data directory", st.data_dir],
      ]))]),
    ]));
    const cfgRows = s.config.map((r) => h("tr", null, [h("td", { class: "mono", text: r.key }), h("td", { class: "mono wrap", text: JSON.stringify(r.value) }), h("td", null, h("span", { class: "source-tag " + r.source, text: r.source }))]));
    host.appendChild(h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Effective configuration" }), h("span", { class: "meta", text: "secrets redacted; precedence flag > env > file > default" })]),
      h("div", { class: "body flush scroll-x" }, h("table", { class: "data" }, [h("thead", null, h("tr", null, ["Key", "Value", "Source"].map((x) => h("th", { scope: "col", text: x })))), h("tbody", null, cfgRows)]))]));
    const lv = segmented([{ value: "warning", label: "Warnings and errors" }, { value: "info", label: "Everything" }], level, (v) => { level = v; load(); }, "Log level");
    const logRows = s.logs.length ? h("ul", { class: "event-list" }, s.logs.slice().reverse().map((l) => h("li", null, [h("time", { text: (l.ts || "").slice(11, 23) }), h("span", { class: "ek " + (l.level === "error" || l.level === "critical" ? "fail" : l.level === "warning" ? "warn" : ""), text: `${l.logger || ""}: ${l.msg}` })]))) : h("p", { class: "body muted small", text: "No log lines at this level." });
    host.appendChild(h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Recent log lines" }), lv]), h("div", { class: "body flush" }, logRows)]));
  }
  await load();
  return null;
}
