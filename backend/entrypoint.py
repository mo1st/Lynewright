"""Prepare mounted data ownership, then run the API without root privileges."""
from __future__ import annotations

import os
from pathlib import Path
import sys

DATA_ROOT = Path("/data")


def _container_root_maps_to_host_root() -> bool:
    """Return true only for the ordinary rootful Docker/Podman uid/gid maps."""
    for map_name in ("uid_map", "gid_map"):
        try:
            first = (Path("/proc/self") / map_name).read_text(encoding="ascii").splitlines()[0]
            inside, outside, size = (int(value) for value in first.split())
        except (OSError, IndexError, ValueError):
            return False
        if inside != 0 or outside != 0 or size < 2:
            return False
    return True


def _chown_data_tree(uid: int, gid: int) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    for directory, subdirectories, filenames in os.walk(DATA_ROOT, followlinks=False):
        os.chown(directory, uid, gid, follow_symlinks=False)
        for name in (*subdirectories, *filenames):
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)


def drop_to_host_user() -> None:
    """Chown mounted data on rootful installs, then drop to the installer account."""
    geteuid = getattr(os, "geteuid", lambda: 1)
    if geteuid() != 0 or not _container_root_maps_to_host_root():
        # Rootless container root already maps to the invoking host user.
        return

    try:
        uid = int(os.environ.get("LYNEWRIGHT_UID", "1000"))
        gid = int(os.environ.get("LYNEWRIGHT_GID", "1000"))
    except ValueError as error:
        raise RuntimeError("LYNEWRIGHT_UID and LYNEWRIGHT_GID must be numeric.") from error
    if uid < 0 or gid < 0:
        raise RuntimeError("LYNEWRIGHT_UID and LYNEWRIGHT_GID must be non-negative.")

    # The MCP service starts only after API health succeeds, so API has already
    # applied ownership to the shared volume. MCP still drops to the same host
    # uid/gid but avoids a second recursive chown under rootful Docker.
    if os.environ.get("LYNEWRIGHT_SKIP_DATA_CHOWN") != "1":
        _chown_data_tree(uid, gid)
    setgroups = getattr(os, "setgroups", None)
    if setgroups is not None:
        setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    command = sys.argv[1:]
    if not command:
        raise SystemExit("container entrypoint requires a command")
    drop_to_host_user()
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
