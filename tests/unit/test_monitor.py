"""Server sampling degrades once per command when the user lacks privileges."""

import asyncio

from pymongo import errors as pe

from ballast.engine.monitor import ClusterMonitor


class FakeAdmin:
    def __init__(self, denied):
        self.denied = denied
        self.calls = []

    async def command(self, cmd):
        name = next(iter(cmd))
        self.calls.append(name)
        if name in self.denied:
            raise pe.OperationFailure("not authorized on admin to execute command", code=13)
        if name == "hello":
            return {"isWritablePrimary": True, "setName": "rs0", "primary": "h:1", "me": "h:1", "hosts": ["h:1"]}
        return {"host": "h", "version": "8.0", "connections": {"current": 3}}


class FakeClient:
    def __init__(self, denied):
        self.admin = FakeAdmin(denied)


async def test_unauthorized_commands_are_recorded_once_and_skipped():
    events, samples = [], []
    mon = ClusterMonitor("mongodb://h:1/", {}, lambda t, k, d: events.append((k, d)), lambda t, k, d: samples.append(k), True, 0.01)
    mon.client = FakeClient({"serverStatus", "replSetGetStatus"})
    task = asyncio.create_task(mon._sample_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    unavailable = [d["command"] for k, d in events if k == "sampling_unavailable"]
    assert sorted(unavailable) == ["replSetGetStatus", "serverStatus"]
    assert all("clusterMonitor" in d["fix"] for k, d in events if k == "sampling_unavailable")
    calls = mon.client.admin.calls
    assert calls.count("serverStatus") == 1 and calls.count("replSetGetStatus") == 1
    assert calls.count("hello") > 2, "hello keeps being sampled"
    assert "hello" in samples
