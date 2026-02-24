"""CLI entrypoint for MongoDB load tester."""

from __future__ import annotations

import logging
from pathlib import Path

import typer
import yaml

from mongo_load_tester.config import default_config, load_config
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
        reconciled_runs = metadata_store.reconcile_incomplete_runs(reason="application-start")
        if reconciled_runs > 0:
            logger.warning("Reconciled %d stale running metadata run(s) on startup.", reconciled_runs)
        config_record = metadata_store.get_or_create_config(bootstrap_config)
        app_config = config_record.config
        if app_config.logging != bootstrap_config.logging:
            logger, log_buffer = configure_logging(app_config.logging)
    except Exception as exc:
        bootstrap_logger.exception("Failed to initialize run command.")
        typer.echo(f"Failed to start load test: {exc}")
        raise typer.Exit(code=1)

    if log_buffer is None:
        raise RuntimeError("Log buffer was not initialized during startup.")
    logger.info("Loaded config file: %s", config)
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
    if metadata_store is None:
        raise RuntimeError("Metadata store was not initialized during startup.")
    sync_service = ConfigSyncService(
        metadata_store=metadata_store,
        controller=controller,
        interval_seconds=app_config.metadata.sync_interval_seconds,
        logger=logger,
    )
    sync_service.mark_applied_version(config_record.version)
    sync_service.start()

    try:
        if no_dashboard or not app_config.dashboard.enabled:
            logger.info("Running in headless mode.")
            typer.echo("Starting headless load test...")
            run_id = metadata_store.record_run_started(config_version=config_record.version)
            try:
                runner.run()
            finally:
                snapshot = metrics.snapshot()
                run_status = "failed" if runner.last_error else "completed"
                metadata_store.record_run_finished(run_id=run_id, status=run_status, snapshot=snapshot, error=runner.last_error)
            if runner.last_error:
                logger.error("Headless run ended with failure: %s", runner.last_error)
                typer.echo(f"Run failed: {runner.last_error}")
                raise typer.Exit(code=1)
            logger.info(
                "Headless run completed total_ops=%d success=%d errors=%d p95_ms=%.2f",
                snapshot.total_ops,
                snapshot.success_ops,
                snapshot.error_ops,
                snapshot.p95_latency_ms,
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
        logger.info(
            "Dashboard available at http://%s:%d",
            app_config.dashboard.host,
            app_config.dashboard.port,
        )
        dashboard.run(
            host=app_config.dashboard.host,
            port=app_config.dashboard.port,
            debug=False,
            threaded=False,
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
        if sync_service is not None:
            sync_service.stop(timeout=5)
        controller.stop()
        controller.join(timeout=5)
        if runner.last_error:
            logger.error("Runner recorded error: %s", runner.last_error)
        if metadata_store is not None:
            metadata_store.close()


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
