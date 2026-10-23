import { api } from "../api.js";
import { app, setTitle, themeChanged } from "../app.js";
import { h, pageHead, segmented, toast } from "../ui.js";

function get(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; } }
function set(key, value) { try { localStorage.setItem(key, value); } catch (e) { toast("This browser does not allow saving settings", "fail"); } }

export async function render(root) {
  setTitle("Settings");
  root.appendChild(pageHead("Settings", "Theme, density and time display are saved in this browser. Branding comes from the server configuration."));
  const theme = segmented([{ value: "system", label: "System" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" }], get("ballast.theme", "system"), (v) => {
    set("ballast.theme", v);
    if (v === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", v);
    toast(`Theme set to ${v}`);
    themeChanged();
  }, "Theme");
  const density = segmented([{ value: "comfortable", label: "Comfortable" }, { value: "compact", label: "Compact" }], get("ballast.density", "comfortable"), (v) => {
    set("ballast.density", v);
    if (v === "compact") document.documentElement.setAttribute("data-density", "compact");
    else document.documentElement.removeAttribute("data-density");
    toast(`Density set to ${v}`);
  }, "Density");
  const tz = segmented([{ value: "local", label: "Local time" }, { value: "utc", label: "UTC" }], get("ballast.tz", "local"), (v) => {
    set("ballast.tz", v);
    toast(v === "utc" ? "Times shown in UTC" : "Times shown in local time");
    themeChanged();
  }, "Time display");
  const a = app.about || {};
  const row = (label, control, help) => h("div", { class: "field" }, [h("div", { class: "label", text: label }), control, help ? h("div", { class: "help", text: help }) : null]);
  root.appendChild(h("section", { class: "panel" }, [h("header", null, h("h2", { text: "Display" })), h("div", { class: "body" }, [
    row("Theme", theme, "System follows your operating system's light or dark setting."),
    row("Density", density, "Compact fits more rows on screen; type size does not change."),
    row("Times", tz, "Stored times are always UTC. This only changes how they are shown."),
  ])]));
  root.appendChild(h("section", { class: "panel" }, [h("header", null, [h("h2", { text: "Branding" }), h("span", { class: "meta", text: "read only here" })]), h("div", { class: "body" }, [
    h("table", { class: "kv" }, h("tbody", null, [
      ["Product name", a.product, "branding.product_name"], ["Logo file", a.has_logo ? "custom" : "default wordmark", "branding.logo_file"], ["Accent color", a.accent_color || "default", "branding.accent_color"],
    ].map(([k, v, key]) => h("tr", null, [h("th", { text: k }), h("td", null, [h("span", { text: v || "" }), " ", h("code", { class: "xs muted", text: key })])])))),
    h("p", { class: "small muted", style: null, text: "Change these in config/ballast.yaml (or BALLAST_BRANDING_* environment variables) and restart with ballastctl restart." }),
  ])]));
  return null;
}
