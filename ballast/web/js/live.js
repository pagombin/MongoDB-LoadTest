/* One shared server-sent event stream with automatic reconnect. */

const listeners = new Set();
let source = null;
let retry = 1000;
let connected = false;

function dispatch(kind, data) {
  listeners.forEach((fn) => {
    try { fn(kind, data); } catch (e) { console.error(e); }
  });
}

export function connect() {
  if (source) return;
  source = new EventSource("api/live");
  ["hello", "state", "point", "event", "marker", "gap", "load", "sample"].forEach((kind) => {
    source.addEventListener(kind, (e) => {
      let data = null;
      try { data = JSON.parse(e.data); } catch (err) { return; }
      dispatch(kind, data);
    });
  });
  source.onopen = () => { connected = true; retry = 1000; dispatch("connection", { connected: true }); };
  source.onerror = () => {
    connected = false;
    dispatch("connection", { connected: false });
    source.close();
    source = null;
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 15000);
  };
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function isConnected() { return connected; }
