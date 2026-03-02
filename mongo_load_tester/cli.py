"""CLI entrypoint for MongoDB load tester."""

from __future__ import annotations

import logging
import time
import threading
from pathlib import Path

import typer
import yaml

from mongo_load_tester.config import SchemaConfig, default_config, load_config
from mongo_load_tester.dashboard.app import create_dashboard
from mongo_load_tester.engine.control import LoadTestController
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner
from mongo_load_tester.metadata import ConfigSyncService, MetadataStore
from mongo_load_tester.observability import configure_logging

app = typer.Typer(help="MongoDB load/stress test runner with live dashboard.")


@app.command("run")
def run_load_test(
    config: Path = typer.Option(..., "--config", "-c", exists=True, readable=True, help="Path to YAML config."),
    no_dashboard: bool = typer.Option(False, "--no-dashboard", help="Run headless without dashboard."),
    load_test_id: str | None = typer.Option(
        None,
        "--load-test-id",
        help="Optional metadata load test id override (default comes from config.metadata.load_test_id).",
    ),
    worker_id: str | None = typer.Option(
        None,
        "--worker-id",
        help="Unique ID for this follower worker. Enables leader/follower mode: "
        "this worker will wait for the leader (dashboard) to start the test, "
        "then join execution. Use with --no-dashboard.",
    ),
) -> None:
    """Run a load test from YAML config."""

    bootstrap_logger = logging.getLogger("mongo_load_tester.bootstrap")
    metadata_store: MetadataStore | None = None
    sync_service: ConfigSyncService | None = None
    logger = logging.getLogger("mongo_load_tester")
    log_buffer = None
    try:
        bootstrap_config = load_config(config)
        if load_test_id:
            bootstrap_config.metadata.load_test_id = load_test_id
        logger, log_buffer = configure_logging(bootstrap_config.logging)
        metadata_store = MetadataStore(
            mongo_config=bootstrap_config.mongo,
            database_name=bootstrap_config.metadata.database_name,
            load_test_id=bootstrap_config.metadata.load_test_id,
            logger=logger,
        )
    except Exception as exc:
        bootstrap_logger.exception("Failed to initialize run command.")
        typer.echo(f"Failed to start load test: {exc}")
        raise typer.Exit(code=1)

    if log_buffer is None:
        raise RuntimeError("Log buffer was not initialized during startup.")
    if metadata_store is None:
        raise RuntimeError("Metadata store was not initialized during startup.")

    is_follower = bool(worker_id) and (no_dashboard or not bootstrap_config.dashboard.enabled)

    if is_follower:
        _run_follower_worker(
            bootstrap_config=bootstrap_config,
            metadata_store=metadata_store,
            worker_id=worker_id or "worker",
            logger=logger,
        )
    else:
        _run_leader(
            config_path=config,
            bootstrap_config=bootstrap_config,
            metadata_store=metadata_store,
            logger=logger,
            log_buffer=log_buffer,
            no_dashboard=no_dashboard,
            worker_id=worker_id,
        )


def _run_leader(
    config_path: Path,
    bootstrap_config,
    metadata_store: MetadataStore,
    logger: logging.Logger,
    log_buffer,
    no_dashboard: bool,
    worker_id: str | None,
) -> None:
    """Run as the leader worker (dashboard or headless without --worker-id)."""
    sync_service: ConfigSyncService | None = None
    try:
        reconciled_runs = metadata_store.reconcile_incomplete_runs(reason="application-start")
        if reconciled_runs > 0:
            logger.warning("Reconciled %d stale running metadata run(s) on startup.", reconciled_runs)
        config_record = metadata_store.get_or_create_config(bootstrap_config)
        app_config = config_record.config
        app_config.mongo = bootstrap_config.mongo
        if app_config.logging != bootstrap_config.logging:
            logger, log_buffer = configure_logging(app_config.logging)

        logger.info("Loaded config file: %s", config_path)
        logger.info(
            "Metadata configuration db=%s load_test_id=%s sync_interval=%ds",
            app_config.metadata.database_name,
            app_config.metadata.load_test_id,
            app_config.metadata.sync_interval_seconds,
        )
        metrics = MetricsStore(history_window_seconds=app_config.dashboard.history_window_seconds)
        runner = LoadTestRunner(app_config, metrics, logger=logger)
        controller = LoadTestController(runner=runner, metrics_store=metrics, logger=logger)
        controller.apply_config(app_config, source="startup")
        _load_collection_schemas(metadata_store, runner, logger)
        sync_service = ConfigSyncService(
            metadata_store=metadata_store,
            controller=controller,
            interval_seconds=app_config.metadata.sync_interval_seconds,
            logger=logger,
        )
        sync_service.mark_applied_version(config_record.version)
        sync_service.start()

        leader_id = worker_id or "leader"
        metadata_store.register_worker(leader_id, "dashboard", "idle")
        metadata_store.set_workload_state("IDLE")

        try:
            if no_dashboard or not app_config.dashboard.enabled:
                logger.info("Running in headless leader mode.")
                typer.echo("Starting headless load test...")
                metadata_store.set_workload_state("STARTED")
                metadata_store.heartbeat_worker(leader_id, "running")
                run_id = metadata_store.record_run_started(config_version=config_record.version)
                try:
                    runner.run()
                finally:
                    snapshot = metrics.snapshot()
                    run_status = "failed" if runner.last_error else "completed"
                    metadata_store.record_run_finished(run_id=run_id, status=run_status, snapshot=snapshot, error=runner.last_error)
                    metadata_store.set_workload_state("COMPLETED" if not runner.last_error else "STOPPED", run_id=run_id)
                    metadata_store.heartbeat_worker(leader_id, "idle")
                if runner.last_error:
                    logger.error("Headless run ended with failure: %s", runner.last_error)
                    typer.echo(f"Run failed: {runner.last_error}")
                    raise typer.Exit(code=1)
                logger.info(
                    "Headless run completed total_ops=%d success=%d errors=%d p95_ms=%.2f",
                    snapshot.total_ops, snapshot.success_ops, snapshot.error_ops, snapshot.p95_latency_ms,
                )
                typer.echo(f"Completed. Total ops={snapshot.total_ops} p95={snapshot.p95_latency_ms:.2f}ms")
                return

            dashboard = create_dashboard(
                config=app_config,
                metrics_store=metrics,
                runner=runner,
                log_buffer=log_buffer,
                controller=controller,
                metadata_store=metadata_store,
                config_sync_service=sync_service,
                initial_config_version=config_record.version,
            )
            typer.echo(f"Dashboard available at http://{app_config.dashboard.host}:{app_config.dashboard.port}")
            typer.echo("Load test is READY and not started yet. Configure values in UI and click Start.")
            dashboard.run(
                host=app_config.dashboard.host,
                port=app_config.dashboard.port,
                debug=False,
                threaded=True,
            )
        except KeyboardInterrupt:
            logger.warning("Interrupted by user.")
            typer.echo("Interrupted by user. Stopping load test...")
            raise typer.Exit(code=130)
        except typer.Exit:
            raise
        except Exception as exc:
            logger.exception("Run command failed unexpectedly.")
            typer.echo(f"Run failed: {exc}")
            raise typer.Exit(code=1)
        finally:
            metadata_store.set_workload_state("STOPPED")
            metadata_store.heartbeat_worker(leader_id, "stopped")
            if sync_service is not None:
                sync_service.stop(timeout=5)
            controller.stop()
            controller.join(timeout=5)
            if runner.last_error:
                logger.error("Runner recorded error: %s", runner.last_error)
            metadata_store.close()
    except typer.Exit:
        raise
    except Exception as exc:
        logger.exception("Leader startup failed.")
        typer.echo(f"Leader failed: {exc}")
        raise typer.Exit(code=1)


def _run_follower_worker(
    bootstrap_config,
    metadata_store: MetadataStore,
    worker_id: str,
    logger: logging.Logger,
) -> None:
    """Run as a follower worker: poll metadata, wait for leader, then execute."""
    logger.info("Starting as follower worker (worker_id=%s). Waiting for leader to start workload...", worker_id)
    typer.echo(f"Follower worker {worker_id} waiting for leader to start the load test...")

    metadata_store.register_worker(worker_id, "headless", "idle")
    heartbeat_stop = threading.Event()

    def _heartbeat_loop():
        while not heartbeat_stop.is_set():
            try:
                state = metadata_store.get_workload_state()
                status = "idle"
                if state:
                    ws = state.get("state", "IDLE")
                    if ws == "STARTED":
                        status = "running"
                    elif ws == "PAUSED":
                        status = "paused"
                metadata_store.heartbeat_worker(worker_id, status)
            except Exception:
                pass
            heartbeat_stop.wait(2.0)

    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="worker-heartbeat")
    hb_thread.start()

    try:
        _wait_for_workload_start(metadata_store, worker_id, logger)
        _execute_follower_workload(bootstrap_config, metadata_store, worker_id, logger)
    except KeyboardInterrupt:
        logger.warning("Follower %s interrupted by user.", worker_id)
        typer.echo("Interrupted by user.")
        raise typer.Exit(code=130)
    except typer.Exit:
        raise
    except Exception as exc:
        logger.exception("Follower worker %s failed.", worker_id)
        typer.echo(f"Follower failed: {exc}")
        raise typer.Exit(code=1)
    finally:
        heartbeat_stop.set()
        hb_thread.join(timeout=3)
        metadata_store.heartbeat_worker(worker_id, "stopped")
        metadata_store.close()


def _wait_for_workload_start(
    metadata_store: MetadataStore,
    worker_id: str,
    logger: logging.Logger,
) -> None:
    """Poll metadata until the workload state is STARTED."""
    while True:
        try:
            state = metadata_store.get_workload_state()
            if state and state.get("state") == "STARTED":
                logger.info("Follower %s detected workload STARTED. Joining execution.", worker_id)
                typer.echo(f"Workload started by leader. Follower {worker_id} joining...")
                return
            if state and state.get("state") in ("STOPPED", "COMPLETED"):
                logger.info("Follower %s detected workload %s. Exiting.", worker_id, state.get("state"))
                typer.echo(f"Workload is {state.get('state')}. Nothing to join.")
                raise typer.Exit(code=0)
        except typer.Exit:
            raise
        except Exception:
            logger.warning("Follower %s: failed to poll workload state. Retrying...", worker_id)
        time.sleep(2.0)


def _execute_follower_workload(
    bootstrap_config,
    metadata_store: MetadataStore,
    worker_id: str,
    logger: logging.Logger,
) -> None:
    """Execute the load test as a follower, reading config from metadata."""
    config_record = metadata_store.fetch_config()
    if config_record is None:
        logger.error("Follower %s: no config found in metadata. Cannot proceed.", worker_id)
        raise typer.Exit(code=1)

    app_config = config_record.config
    app_config.mongo = bootstrap_config.mongo

    logger.info(
        "Follower %s loaded config from metadata (version %d).",
        worker_id, config_record.version,
    )

    metrics = MetricsStore(history_window_seconds=app_config.dashboard.history_window_seconds)
    runner = LoadTestRunner(app_config, metrics, logger=logger)
    _load_collection_schemas(metadata_store, runner, logger)

    from mongo_load_tester.engine.distributed import MetricsPublisher, DISTRIBUTED_METRICS_COLLECTION
    from mongo_load_tester.engine.client import build_client as _build_client
    _pub_client = _build_client(app_config.mongo)
    _pub_collection = _pub_client[app_config.metadata.database_name][DISTRIBUTED_METRICS_COLLECTION]
    metrics_publisher = MetricsPublisher(
        metrics_store=metrics,
        collection=_pub_collection,
        load_test_id=app_config.metadata.load_test_id,
        worker_id=worker_id,
        logger=logger,
    )
    metrics_publisher.start()

    metadata_store.heartbeat_worker(worker_id, "running")
    state_stop = threading.Event()

    last_synced_version = config_record.version

    def _state_sync_loop():
        """Poll workload state AND config version, sync both to the runner."""
        nonlocal last_synced_version
        while not state_stop.is_set():
            try:
                ws = metadata_store.get_workload_state()
                if ws:
                    current = ws.get("state", "IDLE")
                    if current in ("STOPPED", "COMPLETED", "IDLE"):
                        runner.stop()
                        return
                    elif current == "PAUSED":
                        if not runner.is_paused and runner.is_running:
                            runner.pause()
                    elif current == "STARTED":
                        if runner.is_paused:
                            runner.resume()
                rec = metadata_store.fetch_config()
                if rec and rec.version > last_synced_version:
                    new_cfg = rec.config
                    new_cfg.mongo = bootstrap_config.mongo
                    runner.apply_config(new_cfg, record_change=True)
                    last_synced_version = rec.version
                    logger.info("Follower %s synced config version %d.", worker_id, rec.version)
            except Exception:
                pass
            state_stop.wait(2.0)

    sync_thread = threading.Thread(target=_state_sync_loop, daemon=True, name="follower-state-sync")
    sync_thread.start()

    try:
        runner.run()
    finally:
        state_stop.set()
        sync_thread.join(timeout=3)
        metrics_publisher.stop()
        _pub_client.close()
        metadata_store.heartbeat_worker(worker_id, "idle")
        snapshot = metrics.snapshot()
        logger.info(
            "Follower %s finished total_ops=%d p95=%.2fms",
            worker_id, snapshot.total_ops, snapshot.p95_latency_ms,
        )
        typer.echo(f"Follower {worker_id} completed. Total ops={snapshot.total_ops}")


def _load_collection_schemas(
    metadata_store: MetadataStore,
    runner: LoadTestRunner,
    logger: logging.Logger,
) -> None:
    """Load per-collection schema overrides from metadata into the runner's registry."""
    try:
        docs = metadata_store.fetch_collection_schemas()
        for doc in docs:
            db = doc.get("database", "")
            coll = doc.get("collection", "")
            raw_schema = doc.get("schema")
            if not db or not coll or not raw_schema:
                continue
            try:
                schema = SchemaConfig.model_validate(raw_schema)
                runner.register_collection_schema(db, coll, schema)
            except Exception as exc:
                logger.warning(
                    "Skipping invalid per-collection schema for %s.%s: %s",
                    db, coll, exc,
                )
        if docs:
            logger.info("Loaded %d per-collection schema override(s) from metadata.", len(docs))
    except Exception:
        logger.warning("Failed to load per-collection schemas from metadata.", exc_info=True)


@app.command("validate")
def validate_config(
    config: Path = typer.Option(..., "--config", "-c", exists=True, readable=True, help="Path to YAML config."),
) -> None:
    """Validate config file without running a test."""
    try:
        load_config(config)
    except Exception as exc:
        typer.echo(f"Config is invalid: {exc}")
        raise typer.Exit(code=1)
    typer.echo("Config is valid.")


@app.command("init-config")
def init_config(
    output: Path = typer.Option(
        Path("config/loadtest.yaml"),
        "--output",
        "-o",
        help="Where to write a starter config file.",
    ),
) -> None:
    """Generate a starter YAML config."""
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        config_obj = default_config()
        with output.open("w", encoding="utf-8") as file_obj:
            yaml.safe_dump(config_obj.model_dump(mode="json", by_alias=True), file_obj, sort_keys=False)
    except Exception as exc:
        typer.echo(f"Failed to write starter config: {exc}")
        raise typer.Exit(code=1)
    typer.echo(f"Wrote starter config: {output}")


def main() -> None:
    """Console script shim."""
    try:
        app()
    except KeyboardInterrupt:
        raise typer.Exit(code=130)
    except typer.Exit:
        raise
    except Exception as exc:  # pragma: no cover - terminal safety
        logging.getLogger("mongo_load_tester").exception("Unhandled CLI exception.")
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    main()
