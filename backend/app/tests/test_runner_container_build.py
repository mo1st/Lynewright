from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = Path(os.getenv("LYNEWRIGHT_TEMPLATE_REPO", ROOT / "templates/starters"))


def _run(command: list[str], *, cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def test_template_native_wheel_builds_as_a_root_owned_runner_layer(tmp_path: Path) -> None:
    builder = shutil.which("podman") or shutil.which("docker")
    if builder is None:
        pytest.skip("No Docker or Podman container image builder is installed in this environment.")
    native = TEMPLATE / "native"
    if not (native / "pyproject.toml").is_file():
        pytest.fail(f"Template native starter was not found at {native}; set LYNEWRIGHT_TEMPLATE_REPO for CI.")

    slug = f"lynewright-k53b-{uuid.uuid4().hex[:10]}"
    base_image, integration_image = f"{slug}:base", f"{slug}:integration"
    wheel_dir = tmp_path / "wheelhouse"
    wheel_dir.mkdir()
    build = _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(native)],
        cwd=ROOT,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    wheel = wheels[0]
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()

    base = _run([builder, "build", "-f", "runner/Containerfile", "-t", base_image, "."], cwd=ROOT)
    assert base.returncode == 0, base.stdout + base.stderr
    context = tmp_path / "runner-context"
    (context / "wheels").mkdir(parents=True)
    shutil.copy2(wheel, context / "wheels" / wheel.name)
    integration = _run([
        builder, "build", "-f", str(ROOT / "runner" / "Containerfile.integration"),
        "-t", integration_image,
        "--build-arg", f"RUNNER_BASE_IMAGE={base_image}",
        "--build-arg", f"INTEGRATION_WHEEL=wheels/{wheel.name}",
        "--build-arg", f"INTEGRATION_WHEEL_FILENAME={wheel.name}",
        "--build-arg", f"INTEGRATION_WHEEL_SHA256={wheel_hash}",
        str(context),
    ], cwd=ROOT)
    assert integration.returncode == 0, integration.stdout + integration.stderr

    probe = """import importlib.metadata, pathlib, stat
d=importlib.metadata.distribution('lynewright-integration-example')
files=[d.locate_file(f) for f in (d.files or ()) if str(f).startswith('example_native/')]
assert files, 'wheel package files were not installed'
for p in files:
    s=p.stat()
    assert s.st_uid == 0, f'{p} owner was {s.st_uid}, expected root'
    assert not (s.st_mode & 0o022), f'{p} is writable by group or other'
print(f'uid={__import__(\"os\").geteuid()} files={len(files)} root_owned=true non_writable=true')
"""
    verified = _run([
        builder, "run", "--rm", "--network", "none", "--entrypoint", "python", integration_image, "-c", probe,
    ], cwd=ROOT, timeout=60)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "uid=10001" in verified.stdout and "root_owned=true non_writable=true" in verified.stdout
