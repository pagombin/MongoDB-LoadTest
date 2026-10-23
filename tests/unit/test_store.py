import sqlite3
import stat

from ballast.engine.series import OpSecond, SecondRecord
from ballast.store import LATEST_VERSION, Store, intersect_gaps


def test_fresh_store_is_owner_only_and_at_latest_version(tmp_path):
    store = Store(tmp_path / "data" / "ballast.db")
    assert store.schema_version() == LATEST_VERSION
    assert stat.S_IMODE((tmp_path / "data" / "ballast.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700
    assert store.migrated_from == 0
    store.close()


def test_migration_backs_up_existing_store(tmp_path):
    path = tmp_path / "ballast.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    conn.execute("CREATE TABLE legacy_marker (x INTEGER)")
    conn.execute("INSERT INTO legacy_marker VALUES (42)")
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.schema_version() == LATEST_VERSION
    assert store.conn.execute("SELECT x FROM legacy_marker").fetchone()[0] == 42
    store.close()


def test_migration_from_older_version_takes_backup(tmp_path, monkeypatch):
    from ballast import store as store_mod

    path = tmp_path / "ballast.db"
    Store(path).close()
    monkeypatch.setattr(store_mod, "MIGRATIONS", store_mod.MIGRATIONS + [(LATEST_VERSION + 1, "CREATE TABLE extra (id INTEGER)")])
    monkeypatch.setattr(store_mod, "LATEST_VERSION", LATEST_VERSION + 1)
    s2 = store_mod.Store(path)
    assert s2.schema_version() == LATEST_VERSION + 1
    backups = list((tmp_path / "backups").glob("ballast-pre-migration-v*.db"))
    assert len(backups) == 1
    s2.close()
    backup = sqlite3.connect(backups[0])
    assert backup.execute("SELECT version FROM schema_meta").fetchone()[0] == LATEST_VERSION
    backup.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "ballast.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES (999)")
    conn.commit()
    conn.close()
    try:
        Store(path)
    except RuntimeError as exc:
        assert "newer" in str(exc)
    else:
        raise AssertionError("expected refusal")


def test_seconds_round_trip_and_interrupt(tmp_path):
    store = Store(tmp_path / "ballast.db")
    target = store.save_target({"name": "t", "uri": "mongodb://h:1/", "tls": "off"})
    store.create_run("r1", "run", target, "db", {"name": "p"})
    store.update_run("r1", status="running", started_at=1000)
    op = OpSecond(ok=5, err=1)
    op.rt.record(1500, 6)
    store.add_second("r1", SecondRecord(t=100, ops={"find": op}, errors={"NetworkError": 1}))
    back = store.seconds("r1")
    assert back[0].ops["find"].ok == 5 and back[0].errors == {"NetworkError": 1}
    assert store.interrupt_unfinished() == ["r1"]
    run = store.get_run("r1")
    assert run["status"] == "interrupted" and run["ended_at"] == 101_000
    assert store.second_count("r1") == 1
    store.close()


def test_retention_prunes_old_and_excess_runs(tmp_path):
    store = Store(tmp_path / "ballast.db")
    target = store.save_target({"name": "t", "uri": "mongodb://h:1/", "tls": "off"})
    for i in range(4):
        store.create_run(f"r{i}", f"run{i}", target, "db", {})
        store.update_run(f"r{i}", status="completed")
    store.conn.execute("UPDATE runs SET created_at = 1 WHERE id='r0'")
    assert store.prune(max_age_days=30, max_count=0) == ["r0"]
    removed = store.prune(max_age_days=0, max_count=2)
    assert len(removed) == 1 and len(store.list_runs()) == 2
    store.close()


def test_gap_intersection_across_processes():
    gaps = [
        {"process": 0, "kind": "write", "start_ms": 100, "end_ms": 500},
        {"process": 1, "kind": "write", "start_ms": 300, "end_ms": 900},
        {"process": 0, "kind": "write", "start_ms": 1000, "end_ms": 1100},
        {"process": 0, "kind": "read", "start_ms": 0, "end_ms": 50},
    ]
    assert intersect_gaps(gaps, 2) == [{"kind": "write", "start_ms": 300, "end_ms": 500}]
    assert len(intersect_gaps(gaps, 1)) == 4
