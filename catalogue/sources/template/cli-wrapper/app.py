"""Single-command HTTP wrapper for a trusted CLI integration.

Only command.yaml is trusted configuration. Request values are schema-checked
before each replacement occupies one complete argv token. No shell is involved.
"""
from __future__ import annotations

import concurrent.futures
from contextlib import asynccontextmanager
import email
import glob
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote, urlsplit

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response

MAX_REQUEST_HARD_CAP = 250_000_000
MAX_ARTIFACT_BYTES = 10_000_000
MAX_PROGRESS_LINES = 50
MAX_PROGRESS_LINE_CHARS = 1000
_PLACEHOLDER = re.compile(r"^\{([A-Za-z][A-Za-z0-9_]*)\}$")
_PLACEHOLDER_ANYWHERE = re.compile(r"\{[^{}]+\}")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class WrapperInputError(ValueError):
    pass


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WrapperInputError(f"{label} must be an integer between {minimum} and {maximum}.")
    return value


def validate_command(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WrapperInputError("Command configuration must be an object.")
    for key in ("argv", "params", "inputs", "outputs", "limits"):
        if key not in value:
            raise WrapperInputError(f"Command configuration is missing {key}.")
    argv, params, inputs, outputs, limits = (
        value["argv"], value["params"], value["inputs"], value["outputs"], value["limits"]
    )
    if not isinstance(argv, list) or not argv or len(argv) > 128 or any(not isinstance(item, str) or not item for item in argv):
        raise WrapperInputError("argv must be a non-empty list of at most 128 non-empty strings.")
    if not isinstance(params, dict) or not isinstance(inputs, dict) or not isinstance(limits, dict):
        raise WrapperInputError("params, inputs, and limits must be objects.")
    if isinstance(outputs, str):
        outputs = {"glob": outputs}
    if not isinstance(outputs, dict) or not isinstance(outputs.get("glob"), str):
        raise WrapperInputError("outputs.glob must be a relative glob.")
    pattern = outputs["glob"]
    glob_path = PurePosixPath(pattern)
    if not pattern or glob_path.is_absolute() or "\\" in pattern or any(part in {".", ".."} for part in glob_path.parts):
        raise WrapperInputError("outputs.glob must stay inside the job workdir.")
    if any(not _NAME.fullmatch(str(name)) for name in params):
        raise WrapperInputError("Parameter names must be simple identifiers.")
    if any(not _NAME.fullmatch(str(name)) for name in inputs):
        raise WrapperInputError("Input names must be simple identifiers.")
    supported = {"integer", "number", "string", "boolean", "enum"}
    for name, spec in params.items():
        if not isinstance(spec, dict) or spec.get("type") not in supported:
            raise WrapperInputError(f"params.{name} needs a supported JSON-schema-like type.")
        if "derive" in spec:
            derived = spec["derive"]
            if not isinstance(derived, dict) or derived.get("from") not in params or not isinstance(derived.get("format"), str):
                raise WrapperInputError(f"params.{name}.derive must name another parameter and a format.")
            if _PLACEHOLDER_ANYWHERE.findall(derived["format"]) != ["{value}"]:
                raise WrapperInputError(f"params.{name}.derive.format must contain one {{value}} placeholder.")
        elif "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
            raise WrapperInputError(f"params.{name}.enum must be a non-empty list.")
        if spec.get("pattern") is not None:
            try:
                re.compile(str(spec["pattern"]))
            except re.error as exc:
                raise WrapperInputError(f"params.{name}.pattern is invalid.") from exc
    for name, spec in inputs.items():
        if not isinstance(spec, dict):
            raise WrapperInputError(f"inputs.{name} must be an object.")
        extensions = spec.get("extensions")
        if not isinstance(extensions, list) or not extensions or any(
            not isinstance(ext, str) or not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", ext) for ext in extensions
        ):
            raise WrapperInputError(f"inputs.{name}.extensions must be a list of file extensions.")
        _integer(spec.get("max_bytes"), f"inputs.{name}.max_bytes", 1, MAX_REQUEST_HARD_CAP)
    try:
        limits["timeout_seconds"] = float(limits.get("timeout_seconds", 0))
    except (TypeError, ValueError) as exc:
        raise WrapperInputError("limits.timeout_seconds must be numeric.") from exc
    if not 0.1 <= limits["timeout_seconds"] <= 3600:
        raise WrapperInputError("limits.timeout_seconds must be between 0.1 and 3600.")
    _integer(limits.get("max_output_bytes"), "limits.max_output_bytes", 1, MAX_ARTIFACT_BYTES)
    _integer(limits.get("max_concurrent_jobs"), "limits.max_concurrent_jobs", 1, 128)
    allowed = set(params) | set(inputs) | {"output"}
    for token in argv:
        found = _PLACEHOLDER_ANYWHERE.findall(token)
        match = _PLACEHOLDER.fullmatch(token)
        if found and not match:
            raise WrapperInputError("Every command placeholder must occupy a whole argv element.")
        if match and match.group(1) not in allowed:
            raise WrapperInputError(f"argv contains undeclared placeholder {match.group(1)}.")
        if ("{" in token or "}" in token) and not match:
            raise WrapperInputError("Literal argv elements may not contain braces.")
        if "\x00" in token:
            raise WrapperInputError("argv may not contain NUL.")
    for name, spec in params.items():
        if "derive" in spec and "derive" in params[spec["derive"]["from"]]:
            raise WrapperInputError("Derived parameters may not be chained.")
    return {**value, "outputs": outputs}


def _load_command(path: str | Path | None) -> dict[str, Any]:
    selected = Path(path or os.getenv("WRAPPER_COMMAND_FILE", Path(__file__).with_name("command.yaml")))
    try:
        return validate_command(yaml.safe_load(selected.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"Could not load command configuration: {exc}") from exc


def _validate_parameters(command: dict[str, Any], raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise WrapperInputError("args must be a JSON object.")
    specs = command["params"]
    public_names = {name for name, spec in specs.items() if "derive" not in spec}
    unknown = set(raw) - public_names
    if unknown:
        raise WrapperInputError(f"Unknown args: {', '.join(sorted(map(str, unknown)))}.")
    values: dict[str, str] = {}
    for name in public_names:
        spec = specs[name]
        if name not in raw:
            if "default" not in spec:
                raise WrapperInputError(f"args.{name} is required.")
            value = spec["default"]
        else:
            value = raw[name]
        kind = spec["type"]
        if kind == "integer":
            value = _integer(value, f"args.{name}", int(spec.get("min", -2**31)), int(spec.get("max", 2**31 - 1)))
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise WrapperInputError(f"args.{name} must be a number.")
            value = float(value)
            if not float(spec.get("min", -1e308)) <= value <= float(spec.get("max", 1e308)):
                raise WrapperInputError(f"args.{name} is outside its allowed range.")
        elif kind == "boolean" and not isinstance(value, bool):
            raise WrapperInputError(f"args.{name} must be a boolean.")
        elif kind in {"string", "enum"}:
            if not isinstance(value, str):
                raise WrapperInputError(f"args.{name} must be a string.")
            if "enum" in spec and value not in spec["enum"]:
                raise WrapperInputError(f"args.{name} is not an allowed value.")
            if len(value) < int(spec.get("min_length", 0)) or len(value) > int(spec.get("max_length", 1_000_000)):
                raise WrapperInputError(f"args.{name} has an invalid length.")
            if "pattern" in spec and re.fullmatch(str(spec["pattern"]), value) is None:
                raise WrapperInputError(f"args.{name} does not match its required pattern.")
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if "\x00" in text:
            raise WrapperInputError(f"args.{name} may not contain NUL.")
        if text.startswith("-") and not bool(spec.get("allow_leading_dash")):
            raise WrapperInputError(f"args.{name} may not start with a dash.")
        values[name] = text
    for name, spec in specs.items():
        if "derive" in spec:
            source = str(spec["derive"]["from"])
            text = str(spec["derive"]["format"]).replace("{value}", values[source])
            if "\x00" in text or text.startswith("-"):
                raise WrapperInputError(f"Derived args.{name} is invalid.")
            values[name] = text
    return values


def _safe_filename(filename: str) -> str:
    if not filename or "\x00" in filename or "/" in filename or "\\" in filename:
        raise WrapperInputError("Uploaded file name must be a plain file name.")
    if filename in {".", ".."} or PurePosixPath(filename).is_absolute() or PureWindowsPath(filename).is_absolute():
        raise WrapperInputError("Uploaded file name may not be a path.")
    if PureWindowsPath(filename).drive or ":" in filename or filename.startswith("."):
        raise WrapperInputError("Uploaded file name is not allowed.")
    return filename


def _fetch_url(url: str, *, max_bytes: int, allow_private: bool) -> tuple[bytes, str]:
    connection: http.client.HTTPConnection | None = None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise WrapperInputError("Input URLs must be plain http or https URLs without credentials or fragments.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(str(item[4][0]) for item in addresses))
        if not ips:
            raise WrapperInputError("Input URL host did not resolve.")
        if not allow_private and any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise WrapperInputError("Private, loopback, and link-local input URLs are refused.")
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=15)
        sock = socket.create_connection((ips[0], port), timeout=15)
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        connection.sock = sock
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"Host": parsed.netloc, "Accept": "application/octet-stream"})
        response = connection.getresponse()
        if response.status != 200:
            raise WrapperInputError("Input URL must return HTTP 200; redirects are not followed.")
        raw_length = response.getheader("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > max_bytes:
                    raise WrapperInputError("Input URL exceeds the configured size limit.")
            except ValueError:
                pass
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes + 1 - received))
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise WrapperInputError("Input URL exceeds the configured size limit.")
            chunks.append(chunk)
        return b"".join(chunks), str(response.getheader("Content-Type") or "application/octet-stream").split(";", 1)[0]
    except WrapperInputError:
        raise
    except (OSError, ValueError, http.client.HTTPException, ssl.SSLError) as exc:
        raise WrapperInputError("Input URL could not be fetched safely.") from exc
    finally:
        if connection is not None:
            connection.close()


@dataclass
class Job:
    id: str
    workdir: Path
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_PROGRESS_LINES))
    outputs: list[str] = field(default_factory=list)
    served: set[str] = field(default_factory=set)
    error: str | None = None
    process: subprocess.Popen[bytes] | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)


class JobManager:
    def __init__(
        self, command: dict[str, Any], *, work_root: str | Path,
        allow_private_fetch: bool, retention_seconds: int = 900,
    ) -> None:
        self.command = command
        self.work_root = Path(work_root).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.allow_private_fetch = allow_private_fetch
        self.retention_seconds = retention_seconds
        self.limit = int(command["limits"]["max_concurrent_jobs"])
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.limit, thread_name_prefix="cli-wrapper",
        )
        self.stop_sweeper = threading.Event()
        self.sweeper = threading.Thread(target=self._sweep_loop, daemon=True, name="cli-wrapper-ttl")
        self.sweeper.start()

    def submit(self, args: Any, sources: dict[str, tuple[str, bytes, str] | str]) -> Job:
        values = _validate_parameters(self.command, args)
        definitions = self.command["inputs"]
        unknown = set(sources) - set(definitions)
        if unknown:
            raise WrapperInputError(f"Unknown inputs: {', '.join(sorted(unknown))}.")
        for name, spec in definitions.items():
            if spec.get("required", True) and name not in sources:
                raise WrapperInputError(f"Input {name} is required.")
        with self.lock:
            self._sweep_expired_locked()
            active = sum(job.status in {"queued", "running"} for job in self.jobs.values())
            if active >= self.limit:
                raise OverflowError("Maximum concurrent jobs reached.")
            job_id = uuid.uuid4().hex
            workdir = (self.work_root / job_id).resolve()
            if workdir.parent != self.work_root:
                raise WrapperInputError("Workdir escaped its root.")
            workdir.mkdir(mode=0o700)
            job = Job(id=job_id, workdir=workdir)
            self.jobs[job_id] = job
        try:
            input_paths: dict[str, str] = {}
            input_root = job.workdir / "inputs"
            input_root.mkdir(mode=0o700)
            for name, source in sources.items():
                definition = definitions[name]
                if isinstance(source, str):
                    filename = Path(urlsplit(source).path).name
                    _safe_filename(filename)
                    extension = Path(filename).suffix.lower()
                    body, _content_type = _fetch_url(
                        source, max_bytes=int(definition["max_bytes"]),
                        allow_private=self.allow_private_fetch,
                    )
                else:
                    filename, body, _content_type = source
                    _safe_filename(filename)
                    extension = Path(filename).suffix.lower()
                    if len(body) > int(definition["max_bytes"]):
                        raise WrapperInputError(f"Input {name} exceeds the configured size limit.")
                allowed = {str(item).lower() for item in definition["extensions"]}
                if extension not in allowed:
                    raise WrapperInputError(f"Input {name} has a disallowed file extension.")
                safe_path = input_root / f"input-{name}{extension}"
                with safe_path.open("xb") as handle:
                    handle.write(body)
                input_paths[name] = str(safe_path)
            output_suffix = PurePosixPath(self.command["outputs"]["glob"]).suffix
            output_path = workdir / ("output" + output_suffix)
            context = {**values, **input_paths, "output": str(output_path)}
            argv: list[str] = []
            for token in self.command["argv"]:
                match = _PLACEHOLDER.fullmatch(token)
                replacement = context.get(match.group(1)) if match else token
                if replacement is None:
                    raise WrapperInputError(f"No value is available for argv placeholder {match.group(1)}.")
                text = str(replacement)
                if "\x00" in text:
                    raise WrapperInputError("Substituted argv values may not contain NUL.")
                argv.append(text)
            self.executor.submit(self._run, job, argv)
            return job
        except BaseException:
            with self.lock:
                self.jobs.pop(job.id, None)
            shutil.rmtree(job.workdir, ignore_errors=True)
            raise

    def _append_progress(self, job: Job, stream: str, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").rstrip("\r")
        if text:
            with self.lock:
                job.progress.append(f"{stream}: {text[:MAX_PROGRESS_LINE_CHARS]}")
                job.updated_at = time.time()

    def _reader(
        self, job: Job, stream_name: str, pipe: Any, output_count: list[int],
        output_lock: threading.Lock, overflow: threading.Event,
    ) -> None:
        pending = bytearray()
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                with output_lock:
                    output_count[0] += len(chunk)
                    if output_count[0] > int(self.command["limits"]["max_output_bytes"]):
                        overflow.set()
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    self._append_progress(job, stream_name, line)
                    pending = bytearray(remainder[-MAX_PROGRESS_LINE_CHARS * 4:])
                if len(pending) > MAX_PROGRESS_LINE_CHARS * 4:
                    self._append_progress(job, stream_name, bytes(pending[:MAX_PROGRESS_LINE_CHARS * 4]))
                    pending.clear()
        finally:
            if pending:
                self._append_progress(job, stream_name, bytes(pending))

    @staticmethod
    def _kill_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass

    def _matched_outputs(self, workdir: Path) -> list[str]:
        root = workdir.resolve()
        pattern = str(root / self.command["outputs"]["glob"])
        found: list[str] = []
        for candidate_text in glob.glob(pattern, recursive=False):
            candidate = Path(candidate_text)
            try:
                relative = candidate.relative_to(root)
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (ValueError, OSError, RuntimeError):
                continue
            if relative.parts and relative.parts[0] == "inputs":
                continue
            current = root
            unsafe = False
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    unsafe = True
                    break
            if not unsafe and resolved.is_file():
                found.append(relative.as_posix())
        return sorted(set(found))

    def _run(self, job: Job, argv: list[str]) -> None:
        output_count = [0]
        output_lock = threading.Lock()
        overflow = threading.Event()
        readers: list[threading.Thread] = []
        try:
            with self.lock:
                job.status = "running"
                job.updated_at = time.time()
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(job.workdir),
                "TMPDIR": str(job.workdir),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            spawn_options: dict[str, Any] = {}
            if os.name == "posix":
                spawn_options["start_new_session"] = True
            elif os.name == "nt":
                spawn_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            process = subprocess.Popen(
                argv, cwd=job.workdir, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **spawn_options,
            )
            job.process = process
            readers = [
                threading.Thread(target=self._reader, args=(job, "stdout", process.stdout, output_count, output_lock, overflow), daemon=True),
                threading.Thread(target=self._reader, args=(job, "stderr", process.stderr, output_count, output_lock, overflow), daemon=True),
            ]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + float(self.command["limits"]["timeout_seconds"])
            timed_out = False
            while process.poll() is None:
                if job.cancelled.is_set():
                    self._kill_tree(process)
                    raise RuntimeError("Job was cancelled.")
                if overflow.is_set():
                    self._kill_tree(process)
                    raise RuntimeError("Job exceeded max_output_bytes.")
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._kill_tree(process)
                    break
                time.sleep(0.02)
            process.wait(timeout=3)
            for reader in readers:
                reader.join(timeout=2)
            if timed_out:
                raise TimeoutError("Job timed out; its process group was killed.")
            if overflow.is_set():
                raise RuntimeError("Job exceeded max_output_bytes.")
            if process.returncode != 0:
                raise RuntimeError(f"Command exited with status {process.returncode}.")
            outputs = self._matched_outputs(job.workdir)
            output_bytes = output_count[0] + sum((job.workdir / item).stat().st_size for item in outputs)
            if output_bytes > int(self.command["limits"]["max_output_bytes"]):
                raise RuntimeError("Job exceeded max_output_bytes.")
            with self.lock:
                job.outputs = outputs
                job.status = "done"
                job.updated_at = time.time()
            if not outputs:
                shutil.rmtree(job.workdir, ignore_errors=True)
        except BaseException as exc:
            process = job.process
            if process is not None:
                self._kill_tree(process)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                for reader in readers:
                    reader.join(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            # Do not publish a terminal state until the child exits and temp files are gone.
            shutil.rmtree(job.workdir, ignore_errors=True)
            with self.lock:
                job.status = "failed"
                job.error = str(exc)[:500] or type(exc).__name__
                job.updated_at = time.time()
                job.outputs = []
        finally:
            job.process = None

    def get(self, job_id: str) -> Job:
        with self.lock:
            self._sweep_expired_locked()
            job = self.jobs.get(str(job_id))
            if job is None:
                raise KeyError(job_id)
            return job

    def output_path(self, job: Job, name: str) -> Path:
        if name not in job.outputs:
            raise KeyError(name)
        candidate = job.workdir / PurePosixPath(name)
        try:
            relative = candidate.relative_to(job.workdir)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(job.workdir.resolve())
        except (ValueError, OSError, RuntimeError) as exc:
            raise KeyError(name) from exc
        current = job.workdir
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise KeyError(name)
        if not resolved.is_file():
            raise KeyError(name)
        return resolved

    def mark_fetched(self, job: Job, name: str) -> None:
        with self.lock:
            job.served.add(name)
            job.updated_at = time.time()
            if set(job.outputs) <= job.served:
                shutil.rmtree(job.workdir, ignore_errors=True)

    def cancel(self, job_id: str) -> None:
        job = self.get(job_id)
        job.cancelled.set()
        if job.process is not None:
            self._kill_tree(job.process)

    def _sweep_expired_locked(self) -> None:
        now = time.time()
        expired = [
            job_id for job_id, job in self.jobs.items()
            if job.status not in {"queued", "running"} and now - job.updated_at >= self.retention_seconds
        ]
        for job_id in expired:
            job = self.jobs.pop(job_id)
            shutil.rmtree(job.workdir, ignore_errors=True)

    def _sweep_loop(self) -> None:
        while not self.stop_sweeper.wait(30):
            with self.lock:
                self._sweep_expired_locked()

    def close(self) -> None:
        self.stop_sweeper.set()
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.process is not None:
                self._kill_tree(job.process)
        self.executor.shutdown(wait=False, cancel_futures=True)


def _multipart_body(raw: bytes, content_type: str) -> tuple[dict[str, Any], dict[str, tuple[str, bytes, str]]]:
    try:
        message = BytesParser(policy=email_policy).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw
        )
    except (UnicodeEncodeError, email.errors.MessageError) as exc:
        raise WrapperInputError("Multipart request could not be parsed.") from exc
    if not message.is_multipart():
        raise WrapperInputError("Multipart request body is invalid.")
    fields: dict[str, str] = {}
    uploads: dict[str, tuple[str, bytes, str]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_content_disposition() != "form-data" or not isinstance(name, str) or not name:
            raise WrapperInputError("Multipart parts need a form field name.")
        if name in fields or name in uploads:
            raise WrapperInputError(f"Duplicate multipart field {name}.")
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            uploads[name] = (filename, payload, str(part.get_content_type() or "application/octet-stream"))
        else:
            try:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8")
            except (LookupError, UnicodeDecodeError) as exc:
                raise WrapperInputError(f"Multipart field {name} must be UTF-8 text.") from exc
    try:
        args = json.loads(fields.pop("args", "{}"))
    except json.JSONDecodeError as exc:
        raise WrapperInputError("Multipart args must contain JSON.") from exc
    if fields:
        raise WrapperInputError(f"Unexpected multipart field {next(iter(fields))}.")
    return args, uploads


def create_app(
    command: dict[str, Any] | None = None, *,
    command_path: str | Path | None = None,
    work_root: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> FastAPI:
    environment = dict(os.environ if environ is None else environ)
    selected = validate_command(command) if command is not None else _load_command(command_path)
    max_body = min(
        MAX_REQUEST_HARD_CAP,
        1_000_000 + sum(int(spec["max_bytes"]) for spec in selected["inputs"].values()),
    )
    manager = JobManager(
        selected,
        work_root=work_root or environment.get(
            "WRAPPER_WORKDIR", str(Path(tempfile.gettempdir()) / "lynewright-cli-wrapper"),
        ),
        allow_private_fetch=environment.get("WRAPPER_ALLOW_PRIVATE_FETCH", "0") == "1",
    )
    @asynccontextmanager
    async def lifespan(_api: FastAPI):
        try:
            yield
        finally:
            manager.close()

    api = FastAPI(
        title="Lynewright CLI Wrapper", version="1.0.0",
        docs_url=None, redoc_url=None, openapi_url="/openapi.json", lifespan=lifespan,
    )
    api.state.job_manager = manager
    api.state.command = selected

    async def read_limited(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > max_body:
                    raise HTTPException(status_code=413, detail="Request body exceeds the configured input limit.")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length.") from exc
        parts: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body:
                raise HTTPException(status_code=413, detail="Request body exceeds the configured input limit.")
            parts.append(chunk)
        return b"".join(parts)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/jobs", status_code=202)
    async def submit_job(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        raw = await read_limited(request)
        try:
            if content_type == "multipart/form-data":
                args, sources = _multipart_body(raw, request.headers.get("content-type", ""))
                if set(sources) - set(selected["inputs"]):
                    raise WrapperInputError("Multipart upload contains an undeclared input field.")
                job = manager.submit(args, sources)
            elif content_type == "application/json":
                value = json.loads(raw or b"{}")
                if not isinstance(value, dict) or set(value) - {"args", "inputs"}:
                    raise WrapperInputError("JSON body may contain only args and inputs.")
                args = value.get("args", {})
                sources = value.get("inputs", {})
                if not isinstance(sources, dict) or any(not isinstance(url, str) for url in sources.values()):
                    raise WrapperInputError("JSON inputs must map declared input names to URLs.")
                job = manager.submit(args, sources)
            else:
                raise HTTPException(status_code=415, detail="Use application/json or multipart/form-data.")
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except WrapperInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Request JSON is invalid.") from exc
        return JSONResponse({"job_id": job.id}, status_code=202)

    @api.get("/jobs/{job_id}")
    async def read_job(job_id: str, request: Request) -> dict[str, Any]:
        try:
            job = manager.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found.") from exc
        artifacts = []
        if job.status == "done":
            base = str(request.base_url).rstrip("/")
            for name in job.outputs:
                artifacts.append({
                    "name": name,
                    "url": f"{base}/jobs/{quote(job.id, safe='')}/files/{quote(name, safe='/')}",
                    "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                })
        return {
            "job_id": job.id, "status": job.status,
            "progress": list(job.progress), "outputs": list(job.outputs),
            "error": job.error or "", "artifacts": artifacts,
        }

    @api.get("/jobs/{job_id}/files/{name:path}")
    async def read_job_file(job_id: str, name: str) -> Response:
        try:
            job = manager.get(job_id)
            path = manager.output_path(job, name)
            body = path.read_bytes()
        except (KeyError, OSError) as exc:
            raise HTTPException(status_code=404, detail="Output file not found.") from exc
        manager.mark_fetched(job, name)
        return Response(
            content=body,
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            headers={"Content-Length": str(len(body))},
        )

    def custom_openapi() -> dict[str, Any]:
        if api.openapi_schema:
            return api.openapi_schema
        schema = get_openapi(title=api.title, version=api.version, routes=api.routes)
        input_properties = {
            name: {"type": "string", "format": "binary"}
            for name in selected["inputs"]
        }
        schema["paths"]["/jobs"]["post"]["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "args": {"type": "object", "additionalProperties": True},
                        "inputs": {"type": "object", "additionalProperties": {"type": "string", "format": "uri"}},
                    },
                }},
                "multipart/form-data": {"schema": {
                    "type": "object", "properties": {"args": {"type": "string"}, **input_properties},
                }, "encoding": {"args": {"contentType": "application/json"}}},
            },
        }
        schema["paths"]["/jobs"]["post"]["responses"]["202"] = {
            "description": "Job accepted.",
            "content": {"application/json": {"schema": {
                "type": "object", "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
            }}},
        }
        schema["paths"]["/jobs/{job_id}"]["get"]["responses"]["200"] = {
            "description": "Job state and output artifact links.",
            "content": {"application/json": {"schema": {
                "type": "object",
                "required": ["job_id", "status", "progress", "outputs", "error", "artifacts"],
                "properties": {
                    "job_id": {"type": "string"},
                    "status": {"enum": ["queued", "running", "done", "failed"]},
                    "progress": {"type": "array", "items": {"type": "string"}},
                    "outputs": {"type": "array", "items": {"type": "string"}},
                    "error": {"type": ["string", "null"]},
                    "artifacts": {"type": "array", "items": {
                        "type": "object", "required": ["name", "url", "content_type"],
                        "properties": {
                            "name": {"type": "string"}, "url": {"type": "string", "format": "uri"},
                            "content_type": {"type": "string"},
                        },
                    }},
                },
            }}},
        }
        schema["paths"]["/jobs/{job_id}/files/{name}"]["get"]["responses"]["200"] = {
            "description": "Output file bytes.",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
        api.openapi_schema = schema
        return schema

    api.openapi = custom_openapi  # type: ignore[method-assign]
    return api


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WRAPPER_PORT", "8001")))
