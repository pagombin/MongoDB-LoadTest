"""ballast run: one run from the command line, without the web server. For CI and scripted tests.

Exit codes: 0 completed (and SLO passed or not set), 1 SLO failed, 2 could not start, 3 run failed,
4 stopped by a guardrail, 130 interrupted.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from ballast import notify, report
from ballast.config import YAML_ERRORS, Loaded, yaml_load
from ballast.engine.profile import Profile
from ballast.engine.runs import Hub, RunError, RunManager
from ballast.store import Store
from ballast.targets import parse

EXIT_SLO, EXIT_START, EXIT_FAILED, EXIT_GUARDRAIL, EXIT_INTERRUPTED = 1, 2, 3, 4, 130


def load_profile(store: Store, args) -> Profile:
    data: dict = {}
    if args.profile:
        path = Path(args.profile)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            try:
                data = json.loads(text) if path.suffix == ".json" else yaml_load(text)
            except (ValueError, *YAML_ERRORS) as exc:  # type: ignore[misc]
                raise SystemExit(f"error: {path} is not valid JSON or YAML: {exc}") from None
            data = data.get("body", data) if isinstance(data, dict) else {}
        else:
            row = store.get_profile_by_name(args.profile)
            if row is None:
                raise SystemExit(f"error: no profile file or saved profile named '{args.profile}'")
            data = row["body"]
    profile = Profile.model_validate(data or {})
    changes: dict = {}
    if args.rate is not None:
        changes.setdefault("load", {})["rate"] = args.rate
    if args.concurrency is not None:
        changes.setdefault("load", {})["concurrency"] = args.concurrency
    if args.processes is not None:
        changes.setdefault("load", {})["processes"] = args.processes
    if args.duration is not None:
        changes.setdefault("schedule", {})["steady_seconds"] = args.duration
    if args.warmup is not None:
        changes.setdefault("schedule", {})["warmup_seconds"] = args.warmup
    if changes:
        merged = profile.model_dump(mode="json")
        for section, values in changes.items():
            merged[section].update(values)
        profile = Profile.model_validate(merged)
    if profile.schedule.steady_seconds is None:
        print("note: this profile runs until stopped; press Ctrl-C to stop it, or pass --duration SECONDS", file=sys.stderr)
    return profile


def resolve_target(store: Store, loaded: Loaded, args) -> dict:
    if args.uri:
        hosts = parse(args.uri)["hosts"]
        name = args.target or ("cli " + (hosts[0] if hosts else "cluster"))[:80]
        existing = store.get_target_by_name(name)
        return store.save_target({"name": name, "uri": args.uri, "tls": "auto"}, existing["id"] if existing else None)
    if not args.target:
        raise SystemExit("error: pass --uri CONNECTION_STRING or --target NAME")
    for preset in loaded.target_presets():
        if preset["name"] == args.target:
            existing = store.get_target_by_name(preset["name"])
            return store.save_target(preset, existing["id"] if existing else None)
    target = store.get_target_by_name(args.target)
    if target is None:
        raise SystemExit(f"error: no saved target named '{args.target}'")
    return target


def _line(state: dict) -> str:
    last = state.get("last") or {}
    progress = state.get("progress") or {}
    parts = [f"{state['status']:<8}", f"{state['phase']:<8}"]
    if state["status"] == "loading" and state.get("load"):
        parts.append(f"loaded {state['load']['done']:,} of {state['load']['target']:,} documents")
    elif last:
        parts.append(f"{last.get('ok', 0):>9,.0f} ops/s")
        parts.append(f"p99 {last.get('p99', 0) / 1000:>8,.1f} ms")
        parts.append(f"failed {last.get('err', 0):>6,.0f}/s")
    if progress.get("total_remaining_s") is not None:
        parts.append(f"{progress['total_remaining_s']:,.0f} s left")
    return "  ".join(parts)


async def _run(loaded: Loaded, args) -> int:
    store = Store(loaded.data_dir / "ballast.db")
    runs = RunManager(store, Hub(), loaded.data_dir, notifier=notify.from_config(loaded.config))
    try:
        profile = load_profile(store, args)
        target = resolve_target(store, loaded, args)
        name = args.name or f"{profile.name} {time.strftime('%Y-%m-%d %H:%M', time.localtime())}"
        try:
            row = await runs.start(name=name, target=target, profile=profile, acknowledge_foreign=args.yes)
        except RunError as exc:
            err = exc.as_dict()
            print(f"error: {err.get('title')}: {err.get('detail')}", file=sys.stderr)
            if err.get("fix"):
                print(f"       {err['fix']}" + (" Pass --yes to confirm." if exc.status == 428 else ""), file=sys.stderr)
            return EXIT_START
        run_id = row["id"]
        print(f"run {run_id} started: {name}", file=sys.stderr)
        interrupted = 0
        loop = asyncio.get_running_loop()

        def on_signal() -> None:
            nonlocal interrupted
            interrupted += 1
            if interrupted == 1:
                print("stopping (press Ctrl-C again to stop without draining)", file=sys.stderr)
                loop.create_task(runs.stop(run_id, reason="interrupted"))
            elif runs.active is not None and runs.active.task is not None:
                runs.active.task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, on_signal)
            except (NotImplementedError, RuntimeError):
                pass
        last_print = 0.0
        while runs.active is not None and runs.active.task is not None and not runs.active.task.done():
            await asyncio.sleep(0.5)
            if not args.quiet and time.monotonic() - last_print >= args.interval:
                state = runs.status()
                if state:
                    print(_line(state), file=sys.stderr, flush=True)
                last_print = time.monotonic()
        if runs.active is not None and runs.active.task is not None:
            try:
                await runs.active.task
            except asyncio.CancelledError:
                pass
        final = store.get_run(run_id) or {}
        analysis = final.get("analysis")
        if args.json:
            Path(args.json).write_text(report.json_export(store, final, analysis), encoding="utf-8")
        markdown = report.markdown_export(final, analysis)
        if args.markdown:
            Path(args.markdown).write_text(markdown, encoding="utf-8")
        try:
            print(markdown, flush=True)
        except BrokenPipeError:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        if interrupted:
            return EXIT_INTERRUPTED
        if final.get("status") == "failed":
            return EXIT_FAILED
        if final.get("stop_reason") == "guardrail":
            return EXIT_GUARDRAIL
        if final.get("slo_passed") is False:
            return EXIT_SLO
        return 0
    finally:
        store.close()


def main(loaded: Loaded, args) -> int:
    return asyncio.run(_run(loaded, args))
