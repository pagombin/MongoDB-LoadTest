/* App shell: navigation, routing, keyboard shortcuts, about dialog. */
import { api } from "./api.js";
import { connect, subscribe } from "./live.js";
import { h, dialog, F } from "./ui.js";

const ICONS = {
  runs: "M2 3h12M2 8h12M2 13h8",
  newrun: "M8 2v12M2 8h12",
  compare: "M2 12l4-5 3 3 5-7M2 14h12",
  targets: "M8 1.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13M8 5a3 3 0 1 0 0 6a3 3 0 1 0 0-6",
  profiles: "M3 2h10v12H3zM6 5h4M6 8h4M6 11h2",
  settings: "M8 5.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3",
  system: "M2 3h12v8H2zM5 14h6M8 11v3",
};

const NAV = [
  { id: "runs", href: "#/runs", label: "Runs" },
  { id: "newrun", href: "#/new", label: "New run" },
  { id: "compare", href: "#/compare", label: "Compare" },
  { id: "targets", href: "#/targets", label: "Targets" },
  { id: "profiles", href: "#/profiles", label: "Workload profiles" },
  { sep: true },
  { id: "settings", href: "#/settings", label: "Settings" },
  { id: "system", href: "#/system", label: "System" },
];

const ROUTES = [
  { re: /^#\/runs\/([\w]+)\/live$/, view: "live", nav: "runs" },
  { re: /^#\/runs\/([\w]+)$/, view: "report", nav: "runs" },
  { re: /^#\/new$/, view: "newrun", nav: "newrun" },
  { re: /^#\/compare(?:\?(.*))?$/, view: "compare", nav: "compare" },
  { re: /^#\/targets$/, view: "targets", nav: "targets" },
  { re: /^#\/profiles$/, view: "profiles", nav: "profiles" },
  { re: /^#\/settings$/, view: "settings", nav: "settings" },
  { re: /^#\/system$/, view: "system", nav: "system" },
  { re: /^#\/runs$/, view: "runs", nav: "runs" },
];

export const app = { about: null, active: null, cleanup: null, route: null };

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "nav-icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ICONS[name]);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.5");
  path.setAttribute("stroke-linecap", "round");
  svg.appendChild(path);
  return svg;
}

function buildNav() {
  const ul = document.getElementById("nav");
  ul.textContent = "";
  NAV.forEach((item) => {
    if (item.sep) { ul.appendChild(h("li", { class: "nav-sep", "aria-hidden": "true" })); return; }
    const a = h("a", { href: item.href, id: "nav-" + item.id }, [icon(item.id), h("span", { class: "label-text", text: item.label })]);
    a.setAttribute("title", item.label);
    ul.appendChild(h("li", null, a));
  });
}

function markNav(id) {
  document.querySelectorAll("#nav a").forEach((a) => {
    if (a.id === "nav-" + id) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

function updateLiveDot() {
  const runs = document.getElementById("nav-runs");
  if (!runs) return;
  const dot = runs.querySelector(".live-dot");
  const live = app.active && ["starting", "loading", "running", "paused", "stopping"].includes(app.active.status);
  if (live && !dot) runs.appendChild(h("span", { class: "live-dot", title: "A run is active", "aria-label": "A run is active" }));
  if (!live && dot) dot.remove();
}

async function route() {
  const hash = window.location.hash || "#/runs";
  let match = null, def = null;
  for (const r of ROUTES) {
    match = hash.match(r.re);
    if (match) { def = r; break; }
  }
  if (!def) { window.location.hash = "#/runs"; return; }
  if (app.cleanup) { try { app.cleanup(); } catch (e) { console.error(e); } app.cleanup = null; }
  markNav(def.nav);
  const root = document.getElementById("view");
  root.textContent = "";
  app.route = { view: def.view, params: match.slice(1) };
  const mod = await import(`./views/${def.view}.js`);
  const cleanup = await mod.render(root, match.slice(1), app);
  if (app.route && app.route.view === def.view) app.cleanup = cleanup || null;
  if (!document.activeElement || document.activeElement === document.body) root.focus({ preventScroll: true });
}

export function navigate(hash) {
  if (window.location.hash === hash) route();
  else window.location.hash = hash;
}

export function setTitle(text) {
  const product = (app.about && app.about.product) || "Ballast";
  document.title = text ? `${text} – ${product}` : product;
}

function aboutDialog() {
  const a = app.about || {};
  const body = h("table", { class: "kv" }, h("tbody", null, [
    ["Product", a.product], ["Version", a.version], ["Build commit", a.commit], ["Build date", a.built], ["Driver", a.driver], ["Python", a.python],
  ].map(([k, v]) => h("tr", null, [h("th", { text: k }), h("td", { class: "mono", text: v || "–" })]))));
  const wrap = h("div", null, [h("p", { text: a.description || "" }), body, h("p", { class: "xs muted", style: null, text: "Copyright Paul Agombin. All rights reserved. Third-party notices ship in THIRD_PARTY_NOTICES." })]);
  dialog({ title: `About ${a.product || "Ballast"}`, body: wrap, confirm: "Close", cancel: "Copy details", onConfirm: null }).then((ok) => {
    if (!ok && navigator.clipboard) navigator.clipboard.writeText(`${a.product} ${a.version} (${a.commit}, ${a.built}, ${a.driver})`).catch(() => {});
  });
}

function shortcutsDialog() {
  const rows = [["N", "New run"], ["/", "Focus the filter on this page"], ["M", "Add a marker (live run)"], ["P", "Pause or resume (live run)"], ["S", "Stop run (asks first)"], ["?", "Show this list"], ["Esc", "Close a dialog"]];
  const body = h("div", { class: "shortcut-list" }, rows.flatMap(([k, v]) => [h("kbd", { text: k }), h("span", { text: v })]));
  dialog({ title: "Keyboard shortcuts", body, confirm: "Close", cancel: "Dismiss" });
}

function typing(e) {
  const t = e.target;
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
}

function shortcuts() {
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || typing(e) || document.querySelector("dialog[open]")) return;
    if (e.key === "?") { e.preventDefault(); shortcutsDialog(); return; }
    if (e.key === "n" || e.key === "N") { e.preventDefault(); navigate("#/new"); return; }
    if (e.key === "/") {
      const f = /** @type {HTMLElement|null} */ (document.querySelector("[data-filter]"));
      if (f) { e.preventDefault(); f.focus(); }
      return;
    }
    const handler = app.keyHandler;
    if (handler) handler(e);
  });
}

function applyBranding() {
  const a = app.about || {};
  if (a.accent_color) {
    document.documentElement.style.setProperty("--accent", a.accent_color);
  }
  const logo = /** @type {HTMLImageElement} */ (document.getElementById("brand-logo"));
  logo.alt = a.product || "Ballast";
  const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
    (document.documentElement.getAttribute("data-theme") !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  if (!a.has_logo) logo.src = dark ? "img/wordmark-dark.svg" : "img/wordmark.svg";
  if (a.product && a.product !== a.default_product) {
    const brand = document.getElementById("brand");
    if (!brand.querySelector(".brand-name")) brand.appendChild(h("span", { class: "brand-name", text: a.product }));
    if (!a.has_logo) logo.style.display = "none";
  }
}

export function themeChanged() {
  applyBranding();
  route();
}

async function init() {
  buildNav();
  shortcuts();
  document.getElementById("about-btn").addEventListener("click", aboutDialog);
  document.getElementById("shortcuts-btn").addEventListener("click", shortcutsDialog);
  try {
    app.about = await api.get("api/about");
  } catch (e) {
    app.about = { product: "Ballast" };
  }
  applyBranding();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (!document.documentElement.getAttribute("data-theme")) themeChanged();
  });
  try {
    const runs = await api.get("api/runs");
    app.active = runs.active;
  } catch (e) {
    app.active = null;
  }
  updateLiveDot();
  subscribe((kind, data) => {
    if (kind === "hello") { app.active = data.active; updateLiveDot(); }
    if (kind === "state") {
      if (["completed", "stopped", "failed", "interrupted"].includes(data.status)) app.active = null;
      else app.active = data;
      updateLiveDot();
    }
  });
  connect();
  window.addEventListener("hashchange", route);
  route();
  setTitle("");
}

init();
