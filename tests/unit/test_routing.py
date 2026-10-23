from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ballast.engine.analysis import AnalysisSettings, analyze_window
from ballast.engine.profile import Profile, tag_strings
from ballast.engine.runs import read_routes
from ballast.engine.series import HostSecond, OpSecond, SecondRecord


def test_profile_read_preference_defaults_to_the_connection_string():
    options = Profile().driver.client_options()
    assert "readPreference" not in options and "readPreferenceTags" not in options
    p = Profile.model_validate({"driver": {"read_preference": "secondary", "read_preference_tags": [{"nodeType": "ANALYTICS"}, {}]}})
    assert p.driver.client_options()["readPreferenceTags"] == ["nodeType:ANALYTICS", ""]
    assert tag_strings([{"nodeType": "READ_ONLY", "region": "US_EAST_1"}]) == ["nodeType:READ_ONLY,region:US_EAST_1"]


@pytest.mark.parametrize(
    "bad",
    [
        {"driver": {"read_preference": "primary", "read_preference_tags": [{"nodeType": "ANALYTICS"}]}},
        {"read_routing": {"analytics": {"mode": "primary", "tags": [{"nodeType": "ANALYTICS"}]}}},
        {"read_routing": {"insert": {"mode": "secondary"}}},
        {"read_routing": {"find": {"mode": "secondary", "tags": [{"node:Type": "x"}]}}},
    ],
)
def test_invalid_routing_is_refused(bad):
    with pytest.raises(ValidationError):
        Profile.model_validate(bad)


def _desc(*members):
    servers = {
        (addr.split(":")[0], int(addr.split(":")[1])): SimpleNamespace(server_type_name=kind, tags=tags) for addr, kind, tags in members
    }
    return SimpleNamespace(server_descriptions=lambda: servers)


ATLAS = _desc(
    ("a1:1", "RSPrimary", {"nodeType": "ELECTABLE"}),
    ("a2:2", "RSSecondary", {"nodeType": "ELECTABLE"}),
    ("a4:4", "RSSecondary", {"nodeType": "ANALYTICS"}),
    ("a5:5", "RSSecondary", {"nodeType": "READ_ONLY"}),
)


def test_routes_show_reachable_members_and_warn_when_none_match():
    p = Profile.model_validate(
        {
            "operations": {"find": 50, "analytics": 50},
            "read_routing": {"analytics": {"mode": "secondary", "tags": [{"nodeType": "ANALYTICS"}]}},
        }
    )
    routes, warnings = read_routes(p, {"mode": "primary"}, ATLAS)
    by_name = {r["name"]: r for r in routes}
    assert by_name["ANALYTICS"]["members"] == ["a4:4"] and by_name["default"]["members"] == ["a1:1"]
    assert warnings == []
    routes, warnings = read_routes(p, {"mode": "primary"}, _desc(("a1:1", "RSPrimary", {}), ("a2:2", "RSSecondary", {})))
    assert len(warnings) == 1 and "analytics reads" in warnings[0] and "nodeType:ANALYTICS" in warnings[0]


def test_connection_string_read_preference_is_the_default_route():
    routes, _ = read_routes(Profile(), {"mode": "secondary", "tags": [{"nodeType": "READ_ONLY"}]}, ATLAS)
    assert routes[0]["members"] == ["a5:5"]
    routes, _ = read_routes(Profile(), {"mode": "secondary", "tags": [{"nodeType": "MISSING"}, {}]}, ATLAS)
    assert routes[0]["members"] == ["a2:2", "a4:4", "a5:5"], "an empty tag set falls back to any secondary"


def _rec(t, hosts, find=(10, 0), analytics=(5, 0)):
    ops = {"find": OpSecond(ok=find[0], err=find[1]), "analytics": OpSecond(ok=analytics[0], err=analytics[1])}
    for op in ops.values():
        op.rt.record(2000, op.ok + op.err)
    return SecondRecord(t=t, ops=ops, hosts={h: HostSecond(reads=r, writes=w) for h, (r, w) in hosts.items()})


def test_host_seconds_round_trip_merge_and_collapse():
    a = _rec(1, {"a1:1": (10, 4), "a4:4": (5, 0)})
    a.hosts["a4:4"].st.record(900)
    b = SecondRecord.from_json(a.to_json())
    assert b.hosts["a4:4"].reads == 5 and b.hosts["a4:4"].st.total == 1
    b.merge(_rec(1, {"a4:4": (3, 0)}))
    assert b.hosts["a4:4"].reads == 8
    c = SecondRecord.from_json(a.to_json(), collapse=True)
    assert c.hosts["a4:4"].reads == 5 and c.hosts["a4:4"].st.total == 0
    assert c.op_counts == {"find": [10, 0], "analytics": [5, 0]}
    assert a.point()["hosts"] == {"a1:1": [14.0, 0.0], "a4:4": [5.0, 0.0]}


def test_window_reports_members_routes_and_operations():
    records = [_rec(t, {"a1:1": (10, 4), "a4:4": (5, 0)}) for t in range(0, 30)]
    records += [_rec(t, {"a1:1": (10, 4)}, analytics=(0, 5)) for t in range(30, 40)]
    records += [_rec(t, {"a1:1": (10, 4), "a4:4": (5, 0)}) for t in range(40, 80)]
    for r in records[30:40]:
        r.errors = {"ServerSelectionTimeout": 5}
        r.first_err_ms, r.last_err_ms = r.t * 1000, r.t * 1000 + 900
    gaps = [{"kind": "read:ANALYTICS", "start_ms": 29_900, "end_ms": 40_100}]
    events = [
        {
            "t_ms": 30_100,
            "kind": "server_state",
            "data": {"address": "a4:4", "from": "RSSecondary", "to": "Unknown", "node_type": "ANALYTICS"},
        }
    ]
    w = analyze_window({"label": "analytics resize", "start_ms": 30_000, "end_ms": 40_000}, records, gaps, events, AnalysisSettings())
    assert w["route_gaps"]["ANALYTICS"]["ms"] == 10_200
    assert w["members"]["a4:4"]["before"]["reads_per_s"] == 5 and "during" not in w["members"]["a4:4"]
    assert w["members"]["a1:1"]["during"]["writes_per_s"] == 4
    assert w["operations"]["analytics"] == {"ok": 0, "err": 50} and w["operations"]["find"]["err"] == 0
    assert w["member_events"][0]["node_type"] == "ANALYTICS"


def test_routes_become_separate_lanes_with_proportional_slots():
    from ballast.engine.generator import build_lanes, split

    mix = {"find": 0.45, "update": 0.25, "insert": 0.10, "analytics": 0.20}
    lanes = {lane.name: lane for lane in build_lanes(mix, {"analytics": "ANALYTICS"}, True)}
    assert set(lanes) == {"default", "ANALYTICS"}
    assert lanes["ANALYTICS"].ops == ["analytics"] and abs(lanes["ANALYTICS"].share - 0.2) < 1e-9
    assert lanes["default"].ops == ["find", "update", "insert"] and abs(lanes["default"].cum[-1] - 1.0) < 1e-9
    assert [lane.name for lane in build_lanes(mix, {"analytics": "ANALYTICS"}, False)] == ["all"]
    assert [lane.name for lane in build_lanes(mix, {}, True)] == ["all"]
    assert split(128, [0.8, 0.2]) == [102, 26] and sum(split(7, [0.5, 0.3, 0.2])) == 7
    assert split(1, [0.9, 0.1]) == [1, 0] and split(3, [0.98, 0.02]) == [2, 1]
