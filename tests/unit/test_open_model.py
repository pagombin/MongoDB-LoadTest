"""Coordinated omission: a stall must show up as latency for every operation scheduled during it."""

import asyncio
import time

from ballast.engine.generator import OpenSchedule
from ballast.engine.histogram import Histogram


def test_schedule_is_fixed_interval_and_rate_change_rebases():
    s = OpenSchedule(100.0, 10.0)
    assert [round(s.take(), 6) for _ in range(3)] == [10.0, 10.01, 10.02]
    s.set_rate(50.0, now=20.0)
    assert round(s.take(), 6) == 10.03
    assert round(s.take(), 6) == 10.05


def test_rate_increase_after_idle_workers_ran_ahead_starts_now():
    s = OpenSchedule(10.0, 0.0)
    for _ in range(64):
        s.take()
    s.set_rate(1000.0, now=0.5)
    assert s.next_intended() == 0.5
    s = OpenSchedule(0.001, 0.0)
    for _ in range(64):
        s.take()
    s.set_rate(500.0, now=1.0)
    assert s.next_intended() == 1.0


def test_rate_change_during_stall_keeps_backlog():
    s = OpenSchedule(100.0, 0.0)
    for _ in range(100):
        s.take()
    assert s.backlog(10.0) == 901
    s.set_rate(200.0, now=10.0)
    assert s.next_intended() == 1.0
    assert s.backlog(10.0) > 900


def test_rate_change_while_paused_resumes_on_schedule():
    s = OpenSchedule(10.0, 0.0)
    for _ in range(80):
        s.take()
    paused_at = 5.0
    s.set_rate(20.0, now=paused_at)
    s.shift(60.0 - paused_at)
    assert s.next_intended() == 60.0


def test_pause_shifts_schedule_and_backlog_counts_missed_starts():
    s = OpenSchedule(10.0, 0.0)
    assert s.backlog(1.0) == 11
    s.take()
    assert s.backlog(1.0) == 10
    s.shift(5.0)
    assert s.backlog(1.0) == 0
    assert s.next_intended() == 5.1


async def _simulate(stall_at: float, stall_for: float, rate: float, duration: float, workers: int):
    """Run the open-model worker logic against a fake service with one stall."""
    start = time.monotonic()
    sched = OpenSchedule(rate, start)
    response, service = Histogram(), Histogram()
    stall_end = start + stall_at + stall_for

    async def service_call():
        now = time.monotonic()
        if start + stall_at <= now < stall_end:
            await asyncio.sleep(stall_end - now)
        await asyncio.sleep(0.001)

    async def worker():
        while True:
            intended = sched.take()
            if intended - start > duration:
                return
            delay = intended - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            sent = time.monotonic()
            await service_call()
            end = time.monotonic()
            response.record((end - intended) * 1e6)
            service.record((end - sent) * 1e6)

    await asyncio.gather(*(worker() for _ in range(workers)))
    return response, service


async def test_stall_is_reported_as_latency_from_intended_start():
    rate, duration, stall = 200.0, 2.0, 0.5
    response, service = await _simulate(stall_at=0.5, stall_for=stall, rate=rate, duration=duration, workers=4)
    expected_ops = int(rate * duration)
    assert response.total >= expected_ops - 2
    affected = stall * rate / (rate * duration)
    assert affected > 0.2
    p90 = response.percentile(90)
    assert p90 > 100_000, f"p90 response {p90} us should include the stall"
    assert response.percentile(50) < stall * 1e6
    assert service.percentile(90) < 50_000, "service time stays low: only the few in-flight ops waited on the wire"
    assert response.max >= stall * 1e6 * 0.9
