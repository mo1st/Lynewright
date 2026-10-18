"""Key-free HTTP end-to-end coverage for the optional demo deployment."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


BACKEND = Path(__file__).resolve().parents[2]
SCENARIO = Path(__file__).with_name("demo_api_scenario.py")


def _run_scenario(tmp_path: Path, scenario: str, *, enabled: bool) -> None:
    env = os.environ.copy()
    env["LYNEWRIGHT_DB_PATH"] = str(tmp_path / "demo.sqlite3")
    env["LYNEWRIGHT_MANAGED_MEDIA_DIR"] = str(tmp_path / "managed-media")
    env["LYNEWRIGHT_BOOTSTRAP_EMAIL"] = "demo-admin@example.test"
    env["LYNEWRIGHT_BOOTSTRAP_PASSWORD"] = "test-only-placeholder-password"
    env["LYNEWRIGHT_PLUGINS"] = "app.integrations.demo.bootstrap"
    env["LYNEWRIGHT_RUNTIME_WORKER_ENABLED"] = "1"
    env["LYNEWRIGHT_RUNTIME_WORKER_MODE"] = "embedded"
    env["LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED"] = "0"
    env["LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS"] = "1"
    if enabled:
        env["LYNEWRIGHT_DEMO"] = "1"
    else:
        env.pop("LYNEWRIGHT_DEMO", None)
    result = subprocess.run(
        [sys.executable, str(SCENARIO), scenario],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.strip(), result.stderr


def test_demo_run_waits_for_review_then_formats_and_logs(tmp_path: Path) -> None:
    _run_scenario(tmp_path, "approved", enabled=True)


def test_rejecting_demo_review_ends_run_without_formatting(tmp_path: Path) -> None:
    _run_scenario(tmp_path, "rejected", enabled=True)


def test_demo_is_absent_when_flag_is_unset(tmp_path: Path) -> None:
    _run_scenario(tmp_path, "disabled", enabled=False)


def test_demo_passes_media_through_review_link_and_upload(tmp_path: Path) -> None:
    _run_scenario(tmp_path, "media", enabled=True)
