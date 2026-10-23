"""ballast run end to end: the process exit code reflects the SLO and guardrails, and exports are written."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.conftest import URI

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def run(tmp_path, profile: str, *extra: str) -> subprocess.CompletedProcess:
    (tmp_path / "p.yaml").write_text(profile)
    env = dict(os.environ, BALLAST_HOME=str(tmp_path), PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "ballast", "run", "--uri", URI, "--profile", str(tmp_path / "p.yaml"), "--quiet", *extra],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=tmp_path,
    )


BASE = """
name: headless
load: {model: open, rate: 200, concurrency: 32}
schedule: {warmup_seconds: 2, steady_seconds: 8}
dataset: {database: ballast_headless, load: {documents: 2000}}
"""


def test_slo_pass_and_fail_set_the_exit_code(tmp_path):
    ok = run(tmp_path, BASE + "slo: {p99_ms: 60000, error_percent: 5}\n", "--json", "out.json", "--markdown", "out.md")
    assert ok.returncode == 0, ok.stderr
    data = json.loads((tmp_path / "out.json").read_text())
    assert data["analysis"]["slo"]["passed"] is True
    assert "**SLO: passed**" in (tmp_path / "out.md").read_text()
    bad = run(tmp_path, BASE + "slo: {p99_ms: 0.001}\n")
    assert bad.returncode == 1, bad.stderr
    assert "**SLO: FAILED**" in bad.stdout


def test_guardrail_stops_the_run(tmp_path):
    result = run(
        tmp_path, BASE.replace("steady_seconds: 8", "steady_seconds: 60") + "guardrails: {max_p99_ms: 0.001, sustain_seconds: 2}\n"
    )
    assert result.returncode == 4, result.stderr
    assert "stopped (guardrail)" in result.stdout
