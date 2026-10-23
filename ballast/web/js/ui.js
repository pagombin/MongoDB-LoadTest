/* Small DOM helpers and shared components. */

export const h = window.BallastReport.h;
export const F = window.BallastFormat;

export function toast(message, tone) {
  const host = document.getElementById("toasts");
  const node = h("div", { class: "toast " + (tone || "") , text: message });
  host.appendChild(node);
  setTimeout(() => node.remove(), tone === "fail" ? 7000 : 3500);
}

export function announce(message) {
  const n = document.getElementById("announcer");
  n.textContent = "";
  setTimeout(() => { n.textContent = message; }, 30);
}

export function errorBlock(err, retry) {
  const title = err && err.title ? err.title : "Something went wrong";
  return h("div", { class: "notice fail", role: "alert" }, h("div", null, [
    h("div", { class: "notice-title", text: title }),
    err && err.detail ? h("div", { class: "small", text: err.detail }) : null,
    err && err.fix ? h("div", { class: "small fix", text: err.fix }) : null,
    retry ? h("div", { class: "row", style: null }, h("button", { class: "btn small", text: "Try again", onclick: retry })) : null,
  ]));
}

export function empty(title, text, actions) {
  return h("div", { class: "empty" }, [h("h2", { text: title }), h("p", { text }), actions && actions.length ? h("div", { class: "row" }, actions) : null]);
}

export function loading(text) {
  return h("div", { class: "loading", role: "status", text: text || "Loading" });
}

export function chip(status, text) {
  return h("span", { class: "chip " + status, text: text || F.statusText(status) });
}

export function pageHead(title, sub, actions) {
  return h("div", { class: "page-head" }, [
    h("div", null, [h("h1", { text: title }), sub ? h("div", { class: "sub" }, sub) : null]),
    actions ? h("div", { class: "actions" }, actions) : null,
  ]);
}

export function link(href, text, cls) {
  return h("a", { href, class: cls || "", text });
}

export function button(text, onclick, cls, attrs) {
  return h("button", Object.assign({ type: "button", class: "btn " + (cls || ""), text, onclick }, attrs || {}));
}

export function field(labelText, control, help, id) {
  const fid = id || control.id || "f-" + Math.random().toString(36).slice(2, 8);
  control.id = fid;
  if (help) control.setAttribute("aria-describedby", fid + "-help");
  return h("div", { class: "field" }, [h("label", { for: fid, text: labelText }), control, help ? h("div", { class: "help", id: fid + "-help", text: help }) : null]);
}

export function input(attrs) {
  return h("input", Object.assign({ type: "text" }, attrs || {}));
}

export function select(options, value, attrs) {
  const s = h("select", attrs || {});
  options.forEach((o) => {
    const opt = h("option", { value: o.value, text: o.label });
    if (String(o.value) === String(value)) opt.selected = true;
    s.appendChild(opt);
  });
  return s;
}

export function segmented(options, value, onChange, label) {
  const group = h("div", { class: "seg", role: "group", "aria-label": label || null });
  options.forEach((o) => {
    const b = h("button", { type: "button", "aria-pressed": String(o.value) === String(value) ? "true" : "false", text: o.label });
    b.addEventListener("click", () => {
      group.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false"));
      b.setAttribute("aria-pressed", "true");
      onChange(o.value);
    });
    group.appendChild(b);
  });
  return group;
}

/**
 * @param {{ title: string, body: any, confirm: string, cancel?: string, danger?: boolean, typeToConfirm?: string|null, onConfirm?: (() => Promise<any>)|null }} opts
 */
export function dialog({ title, body, confirm, cancel = "Cancel", danger = false, typeToConfirm = null, onConfirm = null }) {
  return new Promise((resolve) => {
    const d = h("dialog", { "aria-labelledby": "dlg-title" });
    const confirmBtn = h("button", { type: "button", class: "btn " + (danger ? "danger solid" : "primary"), text: confirm });
    let typed = null;
    const parts = [typeof body === "string" ? h("p", { text: body }) : body];
    if (typeToConfirm) {
      typed = h("input", { type: "text", id: "dlg-confirm", autocomplete: "off", spellcheck: "false" });
      parts.push(h("div", { class: "field" }, [h("label", { for: "dlg-confirm", text: `Type ${typeToConfirm} to confirm` }), typed]));
      confirmBtn.disabled = true;
      typed.addEventListener("input", () => { confirmBtn.disabled = typed.value !== typeToConfirm; });
    }
    const problem = h("div", { class: "small", style: null });
    parts.push(problem);
    d.appendChild(h("header", null, h("h2", { id: "dlg-title", text: title })));
    d.appendChild(h("div", { class: "dialog-body" }, parts));
    const cancelBtn = h("button", { type: "button", class: "btn", text: cancel });
    d.appendChild(h("footer", null, [cancelBtn, confirmBtn]));
    document.body.appendChild(d);
    const close = (v) => { d.close(); d.remove(); resolve(v); };
    cancelBtn.addEventListener("click", () => close(false));
    d.addEventListener("cancel", (e) => { e.preventDefault(); close(false); });
    confirmBtn.addEventListener("click", async () => {
      if (onConfirm) {
        confirmBtn.disabled = true;
        try {
          await onConfirm();
          close(true);
        } catch (err) {
          confirmBtn.disabled = false;
          problem.textContent = "";
          problem.appendChild(errorBlock(err));
        }
      } else {
        close(true);
      }
    });
    d.showModal();
    (typed || confirmBtn).focus();
  });
}

export function sortableTable(columns, rows, opts) {
  let sortKey = opts.sortKey, dir = opts.dir || "desc";
  const wrap = h("div", { class: "scroll-x" });
  function draw() {
    wrap.textContent = "";
    const sorted = rows.slice().sort((a, b) => {
      const col = columns.find((c) => c.key === sortKey);
      if (!col || !col.value) return 0;
      const va = col.value(a), vb = col.value(b);
      if (va === vb) return 0;
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      return (va < vb ? -1 : 1) * (dir === "asc" ? 1 : -1);
    });
    const head = h("tr", null, columns.map((c) => {
      const th = h("th", { class: c.num ? "num" : "", scope: "col" });
      if (c.value) {
        if (sortKey === c.key) th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
        th.appendChild(h("button", { type: "button", class: "sort", onclick: () => { if (sortKey === c.key) dir = dir === "asc" ? "desc" : "asc"; else { sortKey = c.key; dir = c.num ? "desc" : "asc"; } draw(); } },
          [c.label, sortKey === c.key ? (dir === "asc" ? " ↑" : " ↓") : ""]));
      } else {
        th.textContent = c.label;
      }
      return th;
    }));
    const body = h("tbody", null, sorted.map((r) => {
      const tr = h("tr", { class: opts.rowClass ? opts.rowClass(r) : "" }, columns.map((c) => {
        const cell = c.render(r);
        return h("td", { class: (c.num ? "num " : "") + (c.cls || "") }, cell);
      }));
      return tr;
    }));
    wrap.appendChild(h("table", { class: "data" }, [h("thead", null, head), body]));
  }
  draw();
  return { node: wrap, setRows: (r) => { rows = r; draw(); } };
}

export function download(href) {
  const a = h("a", { href, download: "" });
  document.body.appendChild(a);
  a.click();
  a.remove();
}
