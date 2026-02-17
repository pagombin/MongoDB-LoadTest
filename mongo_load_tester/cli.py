"""CLI entrypoint for MongoDB load tester."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import typer
import yaml

from mongo_load_tester.config import default_config, load_config
from mongo_load_tester.dashboard.app import create_dashboard
from mongo_load_tester.engine.metrics import MetricsStore
from mongo_load_tester.engine.runner import LoadTestRunner
from mongo_load_tester.observability import configure_logging

app = typer.Typer(help="MongoDB load/stress test runner with live dashboard.")


@app.command("run")
def run_load_test(
    config: Path = typer.Option(..., "--config", "-c", exists=True, readable=True, help="Path to YAML config."),
    no_dashboard: bool = typer.Option(False, "--no-dashboard", help="Run headless without dashboard."),
) -> None:
    """Run a load test from YAML config."""

    bootstrap_logger = logging.getLogger("mongo_load_tester.bootstrap")
    try:
        app_config = load_config(config)
        logger, log_buffer = configure_logging(app_config.logging)
    except Exception as exc:
        bootstrap_logger.exception("Failed to initialize run command.")
        typer.echo(f"Failed to start load test: {exc}")
        raise typer.Exit(code=1)

    logger.info("Loaded config file: %s", config)
    metrics = MetricsStore(history_window_seconds=app_config.dashboard.history_window_seconds)
    runner = LoadTestRunner(app_config, metrics, logger=logger)
    worker_thread: threading.Thread | None = None

    try:
        if no_dashboard or not app_config.dashboard.enabled:
            logger.info("Running in headless mode.")
            typer.echo("Starting headless load test...")
            runner.run()
            snapshot = metrics.snapshot()
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

        worker_thread = threading.Thread(target=runner.run, daemon=True, name="runner-main")
        worker_thread.start()
        dashboard = create_dashboard(app_config, metrics, runner, log_buffer)
        typer.echo(f"Dashboard available at http://{app_config.dashboard.host}:{app_config.dashboard.port}")
        logger.info(
            "Dashboard available at http://%s:%d",
            app_config.dashboard.host,
            app_config.dashboard.port,
        )
        dashboard.run(
            host=app_config.dashboard.host,
            port=app_config.dashboard.port,
            debug=False,
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
        runner.stop()
        if worker_thread is not None:
            worker_thread.join(timeout=5)
        if runner.last_error:
            logger.error("Runner recorded error: %s", runner.last_error)


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
