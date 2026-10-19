from __future__ import annotations

import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "backend" / "entrypoint.py"


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("lynewright_container_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rootful_entrypoint_chowns_mounted_data_before_dropping_privileges(
    tmp_path: Path, monkeypatch,
) -> None:
    entrypoint = _load_entrypoint()
    package_dir = tmp_path / "packages"
    package_dir.mkdir()
    package_file = package_dir / "wheel.whl"
    package_file.write_bytes(b"wheel")
    database = tmp_path / "lynewright.sqlite3"
    database.write_bytes(b"sqlite")
    entrypoint.DATA_ROOT = tmp_path
    monkeypatch.setenv("LYNEWRIGHT_UID", "1201")
    monkeypatch.setenv("LYNEWRIGHT_GID", "1302")
    monkeypatch.setattr(entrypoint, "_container_root_maps_to_host_root", lambda: True)
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0, raising=False)
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        entrypoint.os,
        "chown",
        lambda path, uid, gid, **kwargs: events.append(("chown", Path(path), uid, gid)),
        raising=False,
    )
    monkeypatch.setattr(entrypoint.os, "setgroups", lambda groups: events.append(("setgroups", groups)), raising=False)
    monkeypatch.setattr(entrypoint.os, "setgid", lambda gid: events.append(("setgid", gid)), raising=False)
    monkeypatch.setattr(entrypoint.os, "setuid", lambda uid: events.append(("setuid", uid)), raising=False)

    entrypoint.drop_to_host_user()

    chowned_paths = {event[1] for event in events if event[0] == "chown"}
    assert {tmp_path, package_dir, package_file, database} <= chowned_paths
    privilege_events = [event for event in events if event[0] != "chown"]
    assert privilege_events == [("setgroups", []), ("setgid", 1302), ("setuid", 1201)]
    assert max(index for index, event in enumerate(events) if event[0] == "chown") < min(
        index for index, event in enumerate(events) if event[0] == "setgroups"
    )


def test_rootless_entrypoint_keeps_container_user_namespace_mapping(tmp_path: Path, monkeypatch) -> None:
    entrypoint = _load_entrypoint()
    entrypoint.DATA_ROOT = tmp_path
    monkeypatch.setattr(entrypoint, "_container_root_maps_to_host_root", lambda: False)
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(entrypoint.os, "chown", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected chown")), raising=False)
    monkeypatch.setattr(entrypoint.os, "setgid", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected setgid")), raising=False)
    monkeypatch.setattr(entrypoint.os, "setuid", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected setuid")), raising=False)

    entrypoint.drop_to_host_user()


def test_mcp_rootful_entrypoint_skips_repeat_chown_but_drops_to_api_uid(tmp_path: Path, monkeypatch) -> None:
    entrypoint = _load_entrypoint()
    entrypoint.DATA_ROOT = tmp_path
    monkeypatch.setenv("LYNEWRIGHT_UID", "1201")
    monkeypatch.setenv("LYNEWRIGHT_GID", "1302")
    monkeypatch.setenv("LYNEWRIGHT_SKIP_DATA_CHOWN", "1")
    monkeypatch.setattr(entrypoint, "_container_root_maps_to_host_root", lambda: True)
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        entrypoint.os,
        "chown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("MCP must not repeat API's chown")),
        raising=False,
    )
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(entrypoint.os, "setgroups", lambda groups: events.append(("setgroups", groups)), raising=False)
    monkeypatch.setattr(entrypoint.os, "setgid", lambda gid: events.append(("setgid", gid)), raising=False)
    monkeypatch.setattr(entrypoint.os, "setuid", lambda uid: events.append(("setuid", uid)), raising=False)

    entrypoint.drop_to_host_user()

    assert events == [("setgroups", []), ("setgid", 1302), ("setuid", 1201)]
