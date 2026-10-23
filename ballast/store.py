"""Local storage in SQLite under data/. Schema migrations back up the file first."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, overload

from ballast.engine.series import SecondRecord

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
    CREATE TABLE targets (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        uri TEXT NOT NULL,
        options_json TEXT NOT NULL DEFAULT '{}',
        last_test_json TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE profiles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        body_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        target_id TEXT,
        target_name TEXT,
        database_name TEXT,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        ended_at INTEGER,
        profile_json TEXT NOT NULL,
        snapshot_json TEXT NOT NULL DEFAULT '{}',
        summary_json TEXT,
        analysis_json TEXT,
        error_json TEXT
    );
    CREATE INDEX runs_created ON runs(created_at DESC);
    CREATE TABLE seconds (
        run_id TEXT NOT NULL,
        t INTEGER NOT NULL,
        data BLOB NOT NULL,
        PRIMARY KEY (run_id, t)
    ) WITHOUT ROWID;
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        t_ms INTEGER NOT NULL,
        kind TEXT NOT NULL,
        data_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX events_run ON events(run_id, t_ms);
    CREATE TABLE markers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        t_ms INTEGER NOT NULL,
        label TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        phase TEXT,
        source TEXT NOT NULL DEFAULT 'ui'
    );
    CREATE INDEX markers_run ON markers(run_id, t_ms);
    CREATE TABLE gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        process INTEGER NOT NULL,
        kind TEXT NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL
    );
    CREATE INDEX gaps_run ON gaps(run_id, kind, start_ms);
    CREATE TABLE samples (
        run_id TEXT NOT NULL,
        t_ms INTEGER NOT NULL,
        kind TEXT NOT NULL,
        data_json TEXT NOT NULL
    );
    CREATE INDEX samples_run ON samples(run_id, t_ms);
    """,
    ),
    (
        2,
        """
    ALTER TABLE runs ADD COLUMN notes TEXT NOT NULL DEFAULT '';
    ALTER TABLE runs ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE runs ADD COLUMN stop_reason TEXT;
    ALTER TABLE runs ADD COLUMN slo_passed INTEGER;
    """,
    ),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Store:
    def __init__(self, path: Path, backup_dir: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.backup_dir = backup_dir or self.path.parent / "backups"
        self._lock = threading.RLock()
        existed = self.path.exists()
        old_umask = os.umask(0o077)
        try:
            self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        finally:
            os.umask(old_umask)
        _secure_file(self.path)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.migrated_from = self._migrate(existed)
        except BaseException:
            self.conn.close()
            raise
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.path) + suffix)
            if side.exists():
                _secure_file(side)

    # ---- migrations ----

    def schema_version(self) -> int:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
        row = self.conn.execute("SELECT version FROM schema_meta").fetchone()
        return int(row[0]) if row else 0

    def _migrate(self, existed: bool) -> int | None:
        with self._lock:
            current = self.schema_version()
            if current > LATEST_VERSION:
                raise RuntimeError(
                    f"data store schema version {current} is newer than this build supports ({LATEST_VERSION}); use a newer release"
                )
            if current == LATEST_VERSION:
                return None
            if existed and current > 0:
                self.backup(f"pre-migration-v{current}")
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                self.conn.execute("BEGIN")
                try:
                    for statement in _statements(sql):
                        self.conn.execute(statement)
                    self.conn.execute("DELETE FROM schema_meta")
                    self.conn.execute("INSERT INTO schema_meta(version) VALUES (?)", (version,))
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            return current

    def backup(self, label: str) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"ballast-{label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        _secure_file(dest)
        return dest

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ---- targets ----

    @overload
    def _target_row(self, row: dict) -> dict: ...
    @overload
    def _target_row(self, row: dict | None) -> dict | None: ...
    def _target_row(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        options = json.loads(row.pop("options_json") or "{}")
        last = row.pop("last_test_json")
        row.update(options)
        row["last_test"] = json.loads(last) if last else None
        return row

    def list_targets(self) -> list[dict]:
        return [self._target_row(r) for r in self._all("SELECT * FROM targets ORDER BY name")]

    def get_target(self, target_id: str) -> dict | None:
        return self._target_row(self._one("SELECT * FROM targets WHERE id=?", (target_id,)))

    def get_target_by_name(self, name: str) -> dict | None:
        return self._target_row(self._one("SELECT * FROM targets WHERE name=?", (name,)))

    def save_target(self, data: dict, target_id: str | None = None) -> dict:
        options = {k: data.get(k) for k in ("tls", "tls_ca_file", "tls_certificate_key_file", "tls_allow_invalid_hostnames", "notes")}
        ts = now_ms()
        if target_id is None:
            target_id = new_id()
            self._exec(
                "INSERT INTO targets(id,name,uri,options_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (target_id, data["name"], data["uri"], json.dumps(options), ts, ts),
            )
        else:
            self._exec(
                "UPDATE targets SET name=?, uri=?, options_json=?, updated_at=? WHERE id=?",
                (data["name"], data["uri"], json.dumps(options), ts, target_id),
            )
        target = self.get_target(target_id)
        assert target is not None
        return target

    def set_target_test(self, target_id: str, result: dict) -> None:
        self._exec("UPDATE targets SET last_test_json=? WHERE id=?", (json.dumps(result), target_id))

    def delete_target(self, target_id: str) -> bool:
        return self._exec("DELETE FROM targets WHERE id=?", (target_id,)).rowcount > 0

    # ---- profiles ----

    @overload
    def _profile_row(self, row: dict) -> dict: ...
    @overload
    def _profile_row(self, row: dict | None) -> dict | None: ...
    def _profile_row(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        row["body"] = json.loads(row.pop("body_json"))
        return row

    def list_profiles(self) -> list[dict]:
        return [self._profile_row(r) for r in self._all("SELECT * FROM profiles ORDER BY name")]

    def get_profile(self, profile_id: str) -> dict | None:
        return self._profile_row(self._one("SELECT * FROM profiles WHERE id=?", (profile_id,)))

    def get_profile_by_name(self, name: str) -> dict | None:
        return self._profile_row(self._one("SELECT * FROM profiles WHERE name=?", (name,)))

    def save_profile(self, body: dict, profile_id: str | None = None) -> dict:
        ts = now_ms()
        if profile_id is None:
            profile_id = new_id()
            self._exec(
                "INSERT INTO profiles(id,name,body_json,created_at,updated_at) VALUES (?,?,?,?,?)",
                (profile_id, body["name"], json.dumps(body), ts, ts),
            )
        else:
            self._exec("UPDATE profiles SET name=?, body_json=?, updated_at=? WHERE id=?", (body["name"], json.dumps(body), ts, profile_id))
        profile = self.get_profile(profile_id)
        assert profile is not None
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        return self._exec("DELETE FROM profiles WHERE id=?", (profile_id,)).rowcount > 0

    # ---- runs ----

    @overload
    def _run_row(self, row: dict) -> dict: ...
    @overload
    def _run_row(self, row: dict | None) -> dict | None: ...
    def _run_row(self, row: dict | None) -> dict | None:
        if row is None:
            return None
        for key in ("profile_json", "snapshot_json", "summary_json", "analysis_json", "error_json"):
            value = row.pop(key)
            row[key[:-5]] = json.loads(value) if value else None
        row["tags"] = json.loads(row.pop("tags_json", None) or "[]")
        if row.get("slo_passed") is not None:
            row["slo_passed"] = bool(row["slo_passed"])
        return row

    def create_run(self, run_id: str, name: str, target: dict, database: str, profile: dict) -> dict:
        self._exec(
            "INSERT INTO runs(id,name,status,target_id,target_name,database_name,created_at,profile_json) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, name, "starting", target["id"], target["name"], database, now_ms(), json.dumps(profile)),
        )
        run = self.get_run(run_id)
        assert run is not None
        return run

    def update_run(self, run_id: str, **fields) -> None:
        cols: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key in ("snapshot", "summary", "analysis", "error", "profile"):
                cols.append(f"{key}_json=?")
                vals.append(json.dumps(value) if value is not None else None)
            elif key in ("status", "started_at", "ended_at", "name", "notes", "stop_reason"):
                cols.append(f"{key}=?")
                vals.append(value)
            elif key == "tags":
                cols.append("tags_json=?")
                vals.append(json.dumps(value))
            elif key == "slo_passed":
                cols.append("slo_passed=?")
                vals.append(None if value is None else int(bool(value)))
            else:
                raise KeyError(key)
        vals.append(run_id)
        self._exec(f"UPDATE runs SET {', '.join(cols)} WHERE id=?", tuple(vals))  # noqa: S608

    def get_run(self, run_id: str) -> dict | None:
        return self._run_row(self._one("SELECT * FROM runs WHERE id=?", (run_id,)))

    def list_runs(self, limit: int = 500) -> list[dict]:
        rows = self._all(
            "SELECT id,name,status,target_id,target_name,database_name,created_at,started_at,ended_at,summary_json,analysis_json,"
            "notes,tags_json,stop_reason,slo_passed FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        out = []
        for row in rows:
            summary = json.loads(row.pop("summary_json")) if row.get("summary_json") else None
            analysis = json.loads(row.pop("analysis_json")) if row.get("analysis_json") else None
            row["summary"] = summary
            row["client_bound"] = bool(analysis and analysis.get("client_bound", {}).get("client_bound"))
            row["impact_count"] = len(analysis.get("impacts", [])) if analysis else 0
            row["tags"] = json.loads(row.pop("tags_json") or "[]")
            row["slo_passed"] = None if row["slo_passed"] is None else bool(row["slo_passed"])
            out.append(row)
        return out

    def run_counts(self) -> dict[str, int]:
        return {row["status"]: row["n"] for row in self._all("SELECT status, COUNT(*) AS n FROM runs GROUP BY status", ())}

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                for table in ("seconds", "events", "markers", "gaps", "samples"):
                    self.conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))  # noqa: S608
                deleted = self.conn.execute("DELETE FROM runs WHERE id=?", (run_id,)).rowcount
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return deleted > 0

    def interrupt_unfinished(self) -> list[str]:
        rows = self._all("SELECT id FROM runs WHERE status IN ('starting','loading','running','paused','stopping')")
        ids = [r["id"] for r in rows]
        for run_id in ids:
            last = self._one("SELECT MAX(t) AS t FROM seconds WHERE run_id=?", (run_id,))
            ended = (last["t"] + 1) * 1000 if last and last["t"] is not None else now_ms()
            self.update_run(
                run_id,
                status="interrupted",
                ended_at=ended,
                error={
                    "title": "Interrupted",
                    "detail": "The application stopped while this run was active. Data recorded up to that point is kept.",
                },
            )
        return ids

    def prune(self, max_age_days: int, max_count: int) -> list[str]:
        removed: list[str] = []
        active = ("starting", "loading", "running", "paused", "stopping")
        if max_age_days > 0:
            cutoff = now_ms() - max_age_days * 86_400_000
            for row in self._all("SELECT id, status FROM runs WHERE created_at < ?", (cutoff,)):
                if row["status"] not in active and self.delete_run(row["id"]):
                    removed.append(row["id"])
        if max_count > 0:
            rows = self._all("SELECT id, status FROM runs ORDER BY created_at DESC")
            for row in rows[max_count:]:
                if row["status"] not in active and self.delete_run(row["id"]):
                    removed.append(row["id"])
        return removed

    # ---- time series ----

    def add_second(self, run_id: str, record: SecondRecord) -> None:
        blob = zlib.compress(json.dumps(record.to_json(), separators=(",", ":")).encode(), 6)
        self._exec("INSERT OR REPLACE INTO seconds(run_id,t,data) VALUES (?,?,?)", (run_id, record.t, blob))

    def seconds(self, run_id: str, start: int | None = None, end: int | None = None) -> list[SecondRecord]:
        sql = "SELECT data FROM seconds WHERE run_id=?"
        params: list = [run_id]
        if start is not None:
            sql += " AND t>=?"
            params.append(start)
        if end is not None:
            sql += " AND t<?"
            params.append(end)
        sql += " ORDER BY t"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [SecondRecord.from_json(json.loads(zlib.decompress(r[0]))) for r in rows]

    def second_count(self, run_id: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM seconds WHERE run_id=?", (run_id,))
        return int(row["n"]) if row else 0

    # ---- events, markers, gaps, samples ----

    def add_event(self, run_id: str, t_ms: int, kind: str, data: dict) -> dict:
        cur = self._exec("INSERT INTO events(run_id,t_ms,kind,data_json) VALUES (?,?,?,?)", (run_id, t_ms, kind, json.dumps(data)))
        return {"id": cur.lastrowid, "t_ms": t_ms, "kind": kind, "data": data}

    def events(self, run_id: str) -> list[dict]:
        rows = self._all("SELECT id,t_ms,kind,data_json FROM events WHERE run_id=? ORDER BY t_ms, id", (run_id,))
        return [{"id": r["id"], "t_ms": r["t_ms"], "kind": r["kind"], "data": json.loads(r["data_json"])} for r in rows]

    def add_marker(self, run_id: str, t_ms: int, label: str, note: str, phase: str | None, source: str) -> dict:
        cur = self._exec(
            "INSERT INTO markers(run_id,t_ms,label,note,phase,source) VALUES (?,?,?,?,?,?)", (run_id, t_ms, label, note, phase, source)
        )
        return {"id": cur.lastrowid, "run_id": run_id, "t_ms": t_ms, "label": label, "note": note, "phase": phase, "source": source}

    def markers(self, run_id: str) -> list[dict]:
        return self._all("SELECT id,run_id,t_ms,label,note,phase,source FROM markers WHERE run_id=? ORDER BY t_ms, id", (run_id,))

    def add_gap(self, run_id: str, process: int, kind: str, start_ms: int, end_ms: int) -> None:
        self._exec("INSERT INTO gaps(run_id,process,kind,start_ms,end_ms) VALUES (?,?,?,?,?)", (run_id, process, kind, start_ms, end_ms))

    def gaps(self, run_id: str) -> list[dict]:
        return self._all("SELECT process,kind,start_ms,end_ms FROM gaps WHERE run_id=? ORDER BY start_ms", (run_id,))

    def add_sample(self, run_id: str, t_ms: int, kind: str, data: dict) -> None:
        self._exec("INSERT INTO samples(run_id,t_ms,kind,data_json) VALUES (?,?,?,?)", (run_id, t_ms, kind, json.dumps(data, default=str)))

    def samples(self, run_id: str, kind: str | None = None) -> list[dict]:
        sql = "SELECT t_ms,kind,data_json FROM samples WHERE run_id=?"
        params: tuple = (run_id,)
        if kind:
            sql += " AND kind=?"
            params = (run_id, kind)
        rows = self._all(sql + " ORDER BY t_ms", params)
        return [{"t_ms": r["t_ms"], "kind": r["kind"], "data": json.loads(r["data_json"])} for r in rows]

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def intersect_gaps(gaps: list[dict], processes: int) -> list[dict]:
    """Global gaps: intervals where no generator process had a successful operation of that kind."""
    if processes <= 1:
        return [{"kind": g["kind"], "start_ms": g["start_ms"], "end_ms": g["end_ms"]} for g in gaps]
    out: list[dict] = []
    for kind in ("write", "read"):
        per: dict[int, list[tuple[int, int]]] = {}
        for g in gaps:
            if g["kind"] == kind:
                per.setdefault(g["process"], []).append((g["start_ms"], g["end_ms"]))
        if len(per) < processes:
            continue
        current = sorted(per.pop(next(iter(per))))
        for intervals in per.values():
            intervals.sort()
            merged: list[tuple[int, int]] = []
            i = j = 0
            while i < len(current) and j < len(intervals):
                lo = max(current[i][0], intervals[j][0])
                hi = min(current[i][1], intervals[j][1])
                if lo < hi:
                    merged.append((lo, hi))
                if current[i][1] < intervals[j][1]:
                    i += 1
                else:
                    j += 1
            current = merged
        out.extend({"kind": kind, "start_ms": lo, "end_ms": hi} for lo, hi in current)
    return sorted(out, key=lambda g: g["start_ms"])
