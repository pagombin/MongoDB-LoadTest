/* Formatting helpers shared by the app and the offline report. Classic script: defines window.BallastFormat. */
(function () {
  "use strict";

  function prefs() {
    try {
      return { tz: localStorage.getItem("ballast.tz") || "local" };
    } catch (e) {
      return { tz: "local" };
    }
  }

  function num(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return "–";
    var d = digits === undefined ? (Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2) : digits;
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: d });
  }

  function int(v) {
    if (v === null || v === undefined) return "–";
    return Math.round(v).toLocaleString("en-US");
  }

  /* Latency arrives in microseconds. */
  function lat(us) {
    if (us === null || us === undefined) return "–";
    if (us < 1000) return num(us / 1000, 3) + " ms";
    if (us < 10000) return num(us / 1000, 2) + " ms";
    if (us < 1000000) return num(us / 1000, 1) + " ms";
    return num(us / 1000000, 2) + " s";
  }

  function latParts(us) {
    var text = lat(us);
    var i = text.lastIndexOf(" ");
    return i < 0 ? [text, ""] : [text.slice(0, i), text.slice(i + 1)];
  }

  function pct(v, digits) {
    if (v === null || v === undefined) return "–";
    return num(v * 100, digits === undefined ? 2 : digits) + "%";
  }

  function bytes(n) {
    if (n === null || n === undefined) return "–";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return num(n, i === 0 ? 0 : 1) + " " + units[i];
  }

  function duration(ms) {
    if (ms === null || ms === undefined) return "–";
    var s = Math.max(0, Math.round(ms / 1000));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    function p(x) { return x < 10 ? "0" + x : String(x); }
    return h > 0 ? h + ":" + p(m) + ":" + p(sec) : m + ":" + p(sec);
  }

  function seconds(s) {
    if (s === null || s === undefined) return "–";
    if (s < 60) return num(s, s < 10 ? 1 : 0) + " s";
    return duration(s * 1000);
  }

  function parts(ms, utc) {
    var d = new Date(ms);
    return utc
      ? { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds(), ms: d.getUTCMilliseconds() }
      : { y: d.getFullYear(), mo: d.getMonth(), d: d.getDate(), h: d.getHours(), mi: d.getMinutes(), s: d.getSeconds(), ms: d.getMilliseconds() };
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function two(x) { return x < 10 ? "0" + x : String(x); }

  function isUtc() { return prefs().tz === "utc"; }

  function clock(ms, withMs) {
    if (ms === null || ms === undefined) return "–";
    var p = parts(ms, isUtc());
    var text = two(p.h) + ":" + two(p.mi) + ":" + two(p.s);
    if (withMs) text += "." + String(p.ms).padStart(3, "0");
    return text + (isUtc() ? "Z" : "");
  }

  function dateTime(ms) {
    if (ms === null || ms === undefined) return "–";
    var p = parts(ms, isUtc());
    return MONTHS[p.mo] + " " + p.d + " " + two(p.h) + ":" + two(p.mi) + (isUtc() ? " UTC" : "");
  }

  function full(ms) {
    if (ms === null || ms === undefined) return "–";
    var p = parts(ms, isUtc());
    return p.y + "-" + two(p.mo + 1) + "-" + two(p.d) + " " + two(p.h) + ":" + two(p.mi) + ":" + two(p.s) + (isUtc() ? " UTC" : " local");
  }

  function ago(ms) {
    if (!ms) return "never";
    var s = Math.round((Date.now() - ms) / 1000);
    if (s < 60) return s + " s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }

  var EVENT_TEXT = {
    primary_changed: function (d) { return "Primary changed " + (d.from || "none") + " → " + d.to; },
    primary_lost: function (d) { return "No primary (was " + (d.from || "unknown") + ")"; },
    primary_restored: function (d) { return "Primary restored on " + d.to; },
    server_state: function (d) { return d.address + ": " + d.from + " → " + d.to + (d.error ? " (" + d.error + ")" : ""); },
    topology_changed: function (d) { return "Topology " + d.from + " → " + d.to; },
    heartbeat_failed: function (d) { return "Heartbeat failed for " + d.address; },
    heartbeat_recovered: function (d) { return "Heartbeat recovered for " + d.address; },
    pool_cleared: function (d) { return "Connection pool cleared for " + d.address; },
    error_sample: function (d) { return d["class"] + (d.code !== null && d.code !== undefined ? " (code " + d.code + ")" : "") + ": " + d.message; },
    phase: function (d) { return "Phase: " + d.phase + (d.level ? " at " + num(d.level) : "") + (d.seconds ? " for " + seconds(d.seconds) : ""); },
    load_done: function (d) { return "Data load finished: " + int(d.documents) + " documents"; },
    paused: function () { return "Run paused"; },
    resumed: function () { return "Run resumed"; },
    level_changed: function (d) { return "Load changed to " + num(d.level) + (d.model === "open" ? " ops/s" : " workers"); },
    stop_requested: function (d) { return "Stop requested" + (d.reason && d.reason !== "stopped" ? " (" + d.reason.replace(/_/g, " ") + ")" : ""); },
    sampling_unavailable: function (d) { return "Server sampling unavailable for " + d.command + ": " + d.reason; },
    monitor_unavailable: function (d) { return "Topology monitor unavailable: " + d.error; },
    drain_forced: function (d) { return d.in_flight + " operations still in flight at the drain timeout"; },
    generator_killed: function (d) { return "Generator process " + d.process + " was killed"; }
  };

  function eventText(e) {
    var f = EVENT_TEXT[e.kind];
    try {
      return f ? f(e.data || {}) : e.kind.replace(/_/g, " ");
    } catch (err) {
      return e.kind;
    }
  }

  function eventTone(kind) {
    if (kind === "primary_lost" || kind === "heartbeat_failed" || kind === "error_sample" || kind === "generator_killed") return "fail";
    if (kind === "primary_changed" || kind === "pool_cleared" || kind === "server_state" || kind === "sampling_unavailable" || kind === "drain_forced") return "warn";
    return "";
  }

  var STATUS_TEXT = {
    starting: "Starting", loading: "Loading data", running: "Running", paused: "Paused", stopping: "Stopping",
    completed: "Completed", stopped: "Stopped", failed: "Failed", interrupted: "Interrupted"
  };

  function statusText(s) { return STATUS_TEXT[s] || s; }

  function opName(op) {
    return { find: "Find by key", range: "Range find", insert: "Insert", update: "Update", upsert: "Upsert", "delete": "Delete", aggregate: "Aggregate", find_and_modify: "Find and modify" }[op] || op;
  }

  window.BallastFormat = {
    num: num, int: int, lat: lat, latParts: latParts, pct: pct, bytes: bytes, duration: duration, seconds: seconds,
    clock: clock, dateTime: dateTime, full: full, ago: ago, isUtc: isUtc, eventText: eventText, eventTone: eventTone,
    statusText: statusText, opName: opName
  };
})();
