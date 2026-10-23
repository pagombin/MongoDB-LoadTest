import asyncio
import random

from ballast.engine.analysis import AnalysisSettings
from ballast.engine.keys import KeyChooser
from ballast.engine.runs import ActiveRun, Hub, RunManager
from ballast.engine.series import OpSecond, SecondRecord
from ballast.store import Store


def test_upsert_keys_never_collide_with_insert_keys():
    for count in (1, 3):
        choosers = [KeyChooser("uniform", 1000, random.Random(i), process_index=i, process_count=count) for i in range(count)]
        upserts = {c.upsert_key() for c in choosers for _ in range(20_000)}
        inserts = {c.new_key() for c in choosers for _ in range(2_000)}
        assert not upserts & inserts
        assert any(k < 0 for k in upserts)


def _part(t, ok, err=0):
    op = OpSecond(ok=ok, err=err)
    op.rt.record(5000, ok + err)
    rec = SecondRecord(t=t, ops={"find": op}, processes=1)
    if err:
        rec.errors = {"NetworkTimeout": err}
    return rec


def test_late_process_second_is_merged_into_stored_second(tmp_path):
    store = Store(tmp_path / "ballast.db")
    target = store.save_target({"name": "t", "uri": "mongodb://h:1/", "tls": "off"})
    store.create_run("r1", "run", target, "db", {"name": "p"})
    manager = RunManager(store, Hub(), tmp_path)
    run = ActiveRun(id="r1", name="run", profile=None, target=target, database="db", collections=["c"], settings=AnalysisSettings())  # type: ignore[arg-type]
    run.processes = [object(), object()]  # type: ignore[list-item]

    async def scenario():
        for t in range(100, 105):
            run.pending.setdefault(t, {})[0] = _part(t, 10)
            manager._flush_pending(run)
        assert run.last_written == 101
        run.pending.setdefault(100, {})[1] = _part(100, 7, err=2)
        manager._flush_pending(run)

    asyncio.run(scenario())
    stored = {r.t: r for r in store.seconds("r1")}
    assert stored[100].ops["find"].ok == 17
    assert stored[100].ops["find"].err == 2
    assert stored[100].processes == 2
    assert run.errors_live == {"NetworkTimeout": 2}
    store.close()
