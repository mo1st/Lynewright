from __future__ import annotations

import importlib.util
import shutil
import struct
import sys
import zlib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
APP_PATH = ROOT / "app.py"
SPEC = importlib.util.spec_from_file_location("lynewright_cli_wrapper_thumbnail_test", APP_PATH)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wrapper
SPEC.loader.exec_module(wrapper)


def _png_1px() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b"")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is installed in the example container, not required on the test host.")
def test_ffmpeg_thumbnail_command_accepts_a_generated_small_image(tmp_path: Path) -> None:
    fixture = _png_1px()
    assert len(fixture) < 200_000
    command = yaml.safe_load((Path(__file__).with_name("command.yaml")).read_text(encoding="utf-8"))
    api = wrapper.create_app(command, work_root=tmp_path / "work")
    with TestClient(api) as client:
        response = client.post(
            "/jobs", data={"args": '{"width":16,"at_seconds":0}'},
            files={"input": ("tiny.png", fixture, "image/png")},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        job = None
        for _ in range(500):
            current = client.get(f"/jobs/{job_id}").json()
            if current["status"] in {"done", "failed"}:
                job = current
                break
            import time
            time.sleep(0.02)
        assert job and job["status"] == "done", job
        assert job["outputs"] == ["output.png"]
        output = client.get(f"/jobs/{job_id}/files/output.png")
        assert output.status_code == 200
        assert output.content.startswith(b"\x89PNG\r\n\x1a\n")
