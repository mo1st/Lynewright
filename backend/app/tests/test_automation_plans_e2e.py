"""Real HTTP transport coverage with a disposable database and demo integration."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def test_automation_preview_through_api_and_mcp_servers():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, str(root / "tools/verification/automation_plans_smoke.py")],
        cwd=root / "backend", capture_output=True, text=True, timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["status"] == "passed"
    assert summary["checks"] >= 40
    assert summary["preview_lines_created"] == 2
