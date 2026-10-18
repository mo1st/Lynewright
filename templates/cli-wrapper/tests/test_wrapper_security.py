from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("lynewright_cli_wrapper_template", APP_PATH)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wrapper
SPEC.loader.exec_module(wrapper)


def _command(
    *, argv: list[str] | None = None, output_glob: str = "*.txt",
    max_output: int = 4096, max_jobs: int = 2, timeout: float = 2.0,
) -> dict:
    script = (
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ok'); "
        "print(sys.argv[2] if len(sys.argv)>2 else 'finished')"
    )
    return {
        "argv": argv or [sys.executable, "-c", script, "{output}", "{value}"],
        "params": {
            "value": {
                "type": "string", "min_length": 1, "max_length": 64,
                "pattern": "^[A-Za-z0-9._ -]+$",
            },
        },
        "inputs": {
            "input": {"extensions": [".txt", ".png"], "max_bytes": 4096, "required": False},
        },
        "outputs": {"glob": output_glob},
        "limits": {
            "timeout_seconds": timeout, "max_output_bytes": max_output,
            "max_concurrent_jobs": max_jobs,
        },
    }


def _client(tmp_path: Path, command: dict | None = None, *, allow_private: bool = False) -> TestClient:
    api = wrapper.create_app(
        command or _command(), work_root=tmp_path / "work",
        environ={"WRAPPER_ALLOW_PRIVATE_FETCH": "1" if allow_private else "0"},
    )
    return TestClient(api)


def _wait(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"done", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.mark.parametrize(
    "value",
    ["; rm -rf /", "$(id)", "\u0060id\u0060", "&&", "|", "--output=/etc/passwd", "-y", "-leading"],
)
def test_injection_values_are_rejected_before_exec(tmp_path: Path, value: str) -> None:
    marker = tmp_path / "executed"
    script = f"import pathlib,sys; pathlib.Path({str(marker)!r}).write_text('ran')"
    command = _command(argv=[sys.executable, "-c", script, "{value}"])
    with _client(tmp_path, command) as client:
        response = client.post("/jobs", json={"args": {"value": value}})
        assert response.status_code == 422, response.text
    assert not marker.exists()


def test_nul_is_rejected_before_exec(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    script = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"
    with _client(tmp_path, _command(argv=[sys.executable, "-c", script, "{value}"])) as client:
        response = client.post("/jobs", json={"args": {"value": "left\x00right"}})
        assert response.status_code == 422, response.text
    assert not marker.exists()


@pytest.mark.parametrize("filename", ["../../x.txt", "/etc/passwd", "C:\\Windows\\system.ini", "x/../x.txt"])
def test_upload_path_names_are_rejected(tmp_path: Path, filename: str) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/jobs", data={"args": json.dumps({"value": "safe"})},
            files={"input": (filename, b"data", "text/plain")},
        )
        assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/input.txt",
        "http://169.254.169.254/latest/meta-data/input.txt",
        "http://10.0.0.4/input.txt",
    ],
)
def test_private_url_inputs_are_refused_by_default(tmp_path: Path, url: str) -> None:
    with _client(tmp_path) as client:
        response = client.post("/jobs", json={"args": {"value": "safe"}, "inputs": {"input": url}})
        assert response.status_code == 422
        assert "Private" in response.json()["detail"]


def test_output_glob_is_the_only_served_file(tmp_path: Path) -> None:
    script = (
        "import pathlib,os; pathlib.Path('good.txt').write_text('good'); "
        "pathlib.Path('outside.bin').write_text('no')"
    )
    with _client(tmp_path, _command(argv=[sys.executable, "-c", script], output_glob="*.txt")) as client:
        accepted = client.post("/jobs", json={"args": {"value": "safe"}})
        job = _wait(client, accepted.json()["job_id"])
        assert job["status"] == "done", job
        assert job["outputs"] == ["good.txt"]
        job_id = accepted.json()["job_id"]
        assert client.get(f"/jobs/{job_id}/files/outside.bin").status_code == 404
        good = client.get(f"/jobs/{job_id}/files/good.txt")
        assert good.status_code == 200 and good.content == b"good"
        assert not (tmp_path / "work" / job_id).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink behavior is tested on Linux.")
def test_symlink_output_is_excluded_from_matching_list(tmp_path: Path) -> None:
    script = "import os; os.symlink('/etc/passwd','link.txt')"
    with _client(tmp_path, _command(argv=[sys.executable, "-c", script])) as client:
        accepted = client.post("/jobs", json={"args": {"value": "safe"}})
        job = _wait(client, accepted.json()["job_id"])
        assert job["status"] == "done" and job["outputs"] == []
        assert client.get(f"/jobs/{accepted.json()['job_id']}/files/link.txt").status_code == 404


@pytest.mark.skipif(os.name != "posix", reason="Windows process groups do not match POSIX tree-kill semantics.")
def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-finished"
    child = f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('late')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)"
    with _client(tmp_path, _command(argv=[sys.executable, "-c", parent], timeout=0.25)) as client:
        accepted = client.post("/jobs", json={"args": {"value": "safe"}})
        job = _wait(client, accepted.json()["job_id"])
        assert job["status"] == "failed" and "timed out" in (job["error"] or "").lower()
        time.sleep(1.1)
        assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="Windows process groups do not match POSIX tree-kill semantics.")
def test_cancel_kills_the_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "cancelled-grandchild-finished"
    child = f"import pathlib,time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('late')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)"
    with _client(tmp_path, _command(argv=[sys.executable, "-c", parent], timeout=4)) as client:
        accepted = client.post("/jobs", json={"args": {"value": "safe"}})
        job_id = accepted.json()["job_id"]
        manager = client.app.state.job_manager
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and manager.get(job_id).status != "running":
            time.sleep(0.01)
        manager.cancel(job_id)
        job = _wait(client, job_id)
        assert job["status"] == "failed"
        time.sleep(1.1)
        assert not marker.exists()


def test_exceeding_output_limit_fails_and_cleans_workdir(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.write('x'*5000)"
    with _client(tmp_path, _command(argv=[sys.executable, "-c", script], max_output=1024)) as client:
        accepted = client.post("/jobs", json={"args": {"value": "safe"}})
        job_id = accepted.json()["job_id"]
        job = _wait(client, job_id)
        assert job["status"] == "failed" and "max_output_bytes" in (job["error"] or "")
        assert not (tmp_path / "work" / job_id).exists()


def test_exceeding_artifact_output_limit_fails_and_cleans_workdir(tmp_path: Path) -> None:
    script = "import pathlib; pathlib.Path('large.txt').write_bytes(b'x'*5000)"
    with _client(tmp_path, _command(argv=[sys.executable, "-c", script], max_output=1024)) as client:
        accepted = client.post("/jobs", json={"args": {"value": "safe"}})
        job_id = accepted.json()["job_id"]
        job = _wait(client, job_id)
        assert job["status"] == "failed" and "max_output_bytes" in (job["error"] or "")
        assert not (tmp_path / "work" / job_id).exists()


def test_concurrency_limit_returns_429(tmp_path: Path) -> None:
    script = "import time; time.sleep(.5)"
    with _client(tmp_path, _command(argv=[sys.executable, "-c", script], max_jobs=1)) as client:
        first = client.post("/jobs", json={"args": {"value": "safe"}})
        second = client.post("/jobs", json={"args": {"value": "safe"}})
        assert first.status_code == 202
        assert second.status_code == 429


def test_openapi_describes_exact_routes_upload_and_binary_file_response(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/health", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/files/{name}",
    }
    upload = schema["paths"]["/jobs"]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in upload and "application/json" in upload
    binary = schema["paths"]["/jobs/{job_id}/files/{name}"]["get"]["responses"]["200"]["content"]
    assert binary["application/octet-stream"]["schema"]["format"] == "binary"
