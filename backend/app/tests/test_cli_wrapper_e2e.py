from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


BACKEND = Path(__file__).resolve().parents[2]
SCENARIO = Path(__file__).with_name("cli_wrapper_scenario.py")


def test_cli_wrapper_async_upload_review_and_approval(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["LYNEWRIGHT_DB_PATH"] = str(tmp_path / "lynewright.sqlite3")
    env["LYNEWRIGHT_MANAGED_MEDIA_DIR"] = str(tmp_path / "managed-media")
    env["LYNEWRIGHT_BOOTSTRAP_EMAIL"] = "cli-admin@example.test"
    env["LYNEWRIGHT_BOOTSTRAP_PASSWORD"] = "test-only-placeholder-password"
    env["LYNEWRIGHT_PLUGINS"] = ""
    env["LYNEWRIGHT_DEMO"] = "0"
    env["LYNEWRIGHT_RUNTIME_WORKER_ENABLED"] = "1"
    env["LYNEWRIGHT_RUNTIME_WORKER_MODE"] = "embedded"
    env["LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED"] = "0"
    env["LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS"] = "1"
    env["LYNEWRIGHT_TELEGRAM_ENABLED"] = "0"
    result = subprocess.run(
        [sys.executable, str(SCENARIO)],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=75,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.strip(), result.stderr
