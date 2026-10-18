from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import jsonschema
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from app.services import catalogue

from app.services.catalogue import (
    MAX_INDEX_BYTES,
    MAX_SIGNATURE_BYTES,
    CatalogueError,
    CatalogueService,
    _verify_minisign,
)


FIXTURE = Path(__file__).parent / "fixtures" / "catalogue-index.json"


class FakeHTTP:
    def __init__(self) -> None:
        self.responses: dict[str, bytes] = {}
        self.calls: list[tuple[str, int, float]] = []

    def get(self, url: str, *, max_bytes: int, timeout_seconds: float) -> dict:
        self.calls.append((url, max_bytes, timeout_seconds))
        if url not in self.responses:
            raise AssertionError(f"Unexpected request: {url}")
        return {"status_code": 200, "body": self.responses[url]}


def signing_pair():
    key = Ed25519PrivateKey.generate()
    key_id = __import__("secrets").token_bytes(8)
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    encoded_public = base64.b64encode(b"Ed" + key_id + public).decode("ascii")
    return key, key_id, seed, encoded_public


def sign(index_bytes: bytes, key: Ed25519PrivateKey, key_id: bytes, *, hashed: bool = True) -> bytes:
    signed = hashlib.blake2b(index_bytes, digest_size=64).digest() if hashed else index_bytes
    primary = key.sign(signed)
    trusted = f"timestamp:{int(datetime.now(UTC).timestamp())} file:catalogue.json"
    global_signature = key.sign(primary + trusted.encode("ascii"))
    algorithm = b"ED" if hashed else b"Ed"
    return (
        b"untrusted comment: test signature\n"
        + base64.b64encode(algorithm + key_id + primary) + b"\n"
        + b"trusted comment: " + trusted.encode("ascii") + b"\n"
        + base64.b64encode(global_signature) + b"\n"
    )


def fixture_bytes() -> bytes:
    return json.dumps(json.loads(FIXTURE.read_text(encoding="utf-8-sig")), separators=(",", ":")).encode()


def configured_service(monkeypatch, tmp_path, fake: FakeHTTP, key_string: str | None = None, url: str = "https://catalogue.example.test/catalogue.json"):
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "lynewright.sqlite3"))
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_URL", url)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", key_string or "")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")
    service = CatalogueService(http_service=fake)
    return service


def set_signed_response(fake: FakeHTTP, url: str, body: bytes, signature: bytes) -> None:
    fake.responses[url] = body
    fake.responses[url + ".minisig"] = signature


def test_committed_schema_and_fixture_cover_each_kind_and_yanked_entry():
    schema = json.loads((Path(__file__).parents[1] / "schemas" / "catalogue.schema.json").read_text(encoding="utf-8-sig"))
    jsonschema.Draft202012Validator.check_schema(schema)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    jsonschema.validate(fixture, schema, format_checker=jsonschema.FormatChecker())
    assert all(not ({"name", "summary", "description"} & set(entry)) for entry in fixture["entries"])
    with_text = deepcopy(fixture)
    with_text["entries"][0].update(
        name="OpenAPI starter",
        summary="A short summary.",
        description="A plain-text description.\nLine breaks are retained.",
    )
    jsonschema.validate(with_text, schema, format_checker=jsonschema.FormatChecker())
    missing_licence = deepcopy(fixture)
    missing_licence["entries"][0].pop("licence")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_licence, schema, format_checker=jsonschema.FormatChecker())
    assert {entry["kind"] for entry in fixture["entries"]} == {"file", "package", "container"}
    assert any("yanked" in entry for entry in fixture["entries"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "n" * 81),
        ("summary", "s" * 161),
        ("description", "d" * 2001),
        ("name", "bad\x01name"),
        ("summary", "two lines\nnot allowed"),
        ("description", "tab\there is not a display line break"),
    ],
)
def test_catalogue_entry_text_limits_and_controls_are_rejected(field: str, value: str) -> None:
    schema = json.loads((Path(__file__).parents[1] / "schemas" / "catalogue.schema.json").read_text(encoding="utf-8-sig"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    fixture["entries"][0][field] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(fixture, schema, format_checker=jsonschema.FormatChecker())


@pytest.mark.parametrize("field", ["name", "summary", "description"])
def test_catalogue_entry_text_uses_the_k41_secret_guard(field: str) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    fixture["entries"][0][field] = "sk-12345678901234567890"
    with pytest.raises(CatalogueError, match="secret-like literals"):
        catalogue._validate_index(json.dumps(fixture, separators=(",", ":")).encode())


def test_catalogue_index_and_signature_limits_fit_buffered_http_cap() -> None:
    from app.services.platform_services import HTTPService

    assert MAX_INDEX_BYTES == 5 * 1024 * 1024
    assert MAX_SIGNATURE_BYTES == 64 * 1024
    assert MAX_INDEX_BYTES <= HTTPService.MAX_BUFFERED_RESPONSE_BYTES
    assert MAX_SIGNATURE_BYTES <= HTTPService.MAX_BUFFERED_RESPONSE_BYTES


@pytest.mark.parametrize("hashed", [True, False], ids=["minisign-ED-prehash", "minisign-Ed-direct"])
def test_valid_minisign_signature_loads_entries_and_hides_yanked(monkeypatch, tmp_path, hashed):
    key, key_id, _seed, public = signing_pair()
    body = fixture_bytes()
    fake = FakeHTTP()
    url = "https://catalogue.example.test/catalogue.json"
    set_signed_response(fake, url, body, sign(body, key, key_id, hashed=hashed))
    service = configured_service(monkeypatch, tmp_path, fake, public)

    status = service.refresh()

    assert status["status"] == "ready"
    assert status["serial"] == 1
    assert status["last_error"] is None
    assert status["verified_key_id"] == key_id.hex()
    entries = service.list_installable()
    assert {entry["kind"] for entry in entries} == {"file", "package", "container"}
    assert all("yanked" not in entry for entry in entries)
    assert service.get("fixture.yanked", "0.9.0")["yanked"]["reason"]
    assert [call[1] for call in fake.calls] == [MAX_INDEX_BYTES, 64 * 1024]


@pytest.mark.parametrize("failure", ["wrong-signature", "unknown-key", "modified-bytes"])
def test_bad_or_untrusted_signature_is_refused_without_cache(monkeypatch, tmp_path, failure):
    trusted, trusted_id, _seed, trusted_public = signing_pair()
    other, other_id, _other_seed, _other_public = signing_pair()
    body = fixture_bytes()
    fake = FakeHTTP()
    url = "https://catalogue.example.test/catalogue.json"
    if failure == "wrong-signature":
        signature = bytearray(sign(body, trusted, trusted_id))
        signature[80] = ord("A") if signature[80] != ord("A") else ord("B")
        signature = bytes(signature)
    elif failure == "unknown-key":
        signature = sign(body, other, other_id)
    else:
        signature = sign(body + b" ", trusted, trusted_id)
    set_signed_response(fake, url, body, signature)
    service = configured_service(monkeypatch, tmp_path, fake, trusted_public)

    status = service.refresh()

    assert status["status"] == "error"
    assert status["serial"] is None
    assert status["last_error"]
    assert service.list_installable() == []


def test_lower_or_equal_conflicting_serial_preserves_previous_good_cache(monkeypatch, tmp_path):
    key, key_id, _seed, public = signing_pair()
    fake = FakeHTTP()
    url = "https://catalogue.example.test/catalogue.json"
    service = configured_service(monkeypatch, tmp_path, fake, public)
    first = json.loads(fixture_bytes())
    first["serial"] = 7
    first_bytes = json.dumps(first, separators=(",", ":")).encode()
    set_signed_response(fake, url, first_bytes, sign(first_bytes, key, key_id))
    assert service.refresh()["status"] == "ready"
    cached_hash = hashlib.sha256(first_bytes).hexdigest()

    for serial, name, expected in [
        (6, "lower", "lower than the cached serial"),
        (7, "different bytes", "equal serial has different index bytes"),
    ]:
        changed = deepcopy(first)
        changed["serial"] = serial
        changed["name"] = name
        changed_bytes = json.dumps(changed, separators=(",", ":")).encode()
        set_signed_response(fake, url, changed_bytes, sign(changed_bytes, key, key_id))
        failed = service.refresh()
        assert expected in failed["last_error"]
        assert service._cache_row()["index_sha256"] == cached_hash
        assert service.status()["serial"] == 7


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["entries"][2].update(source="ghcr.io/lynewright/wrapper:latest"), "schema error"),
        (lambda data: data["entries"][0].pop("sha256"), "schema error"),
        (lambda data: data["entries"][2].update(sha256="e" * 64), "schema error"),
        (lambda data: data["entries"].append(deepcopy(data["entries"][0])), "duplicate (id, version)"),
        (lambda data: data.update(name="sk-12345678901234567890"), "secret-like literals"),
    ],
)
def test_schema_duplicate_and_secret_checks(monkeypatch, tmp_path, mutation, message):
    key, key_id, _seed, public = signing_pair()
    data = json.loads(fixture_bytes())
    mutation(data)
    body = json.dumps(data, separators=(",", ":")).encode()
    fake = FakeHTTP()
    url = "https://catalogue.example.test/catalogue.json"
    set_signed_response(fake, url, body, sign(body, key, key_id))
    service = configured_service(monkeypatch, tmp_path, fake, public)

    status = service.refresh()

    assert message in status["last_error"]
    assert status["serial"] is None


def test_duplicate_json_object_keys_are_refused(monkeypatch, tmp_path):
    key, key_id, _seed, public = signing_pair()
    body = b'{"format":"lynewright-catalogue/1","format":"lynewright-catalogue/1"}'
    fake = FakeHTTP()
    url = "https://catalogue.example.test/catalogue.json"
    set_signed_response(fake, url, body, sign(body, key, key_id))
    service = configured_service(monkeypatch, tmp_path, fake, public)
    assert "duplicate object key" in service.refresh()["last_error"]


def test_http_private_and_metadata_targets_are_refused(monkeypatch, tmp_path):
    fake = FakeHTTP()
    _key, _key_id, _seed, public = signing_pair()
    service = configured_service(
        monkeypatch, tmp_path, fake, public, url="http://catalogue.example.test/index.json",
    )
    assert "HTTPS" in service.refresh()["last_error"]
    assert fake.calls == []

    from app.services.catalogue import CatalogueService
    for url in ("https://127.0.0.1/catalogue.json", "https://169.254.169.254/latest/meta-data/"):
        configured_service(monkeypatch, tmp_path, fake, url=url)
        guarded = CatalogueService()
        assert guarded.refresh()["last_error"]
    assert fake.calls == []


def test_oversize_index_is_refused(monkeypatch, tmp_path):
    fake = FakeHTTP()
    url = "https://catalogue.example.test/catalogue.json"
    fake.responses[url] = b"x" * (MAX_INDEX_BYTES + 1)
    _key, _key_id, _seed, public = signing_pair()
    service = configured_service(monkeypatch, tmp_path, fake, public)
    assert "5 MiB" in service.refresh()["last_error"]
    assert len(fake.calls) == 1


def test_empty_url_is_offline_without_error_and_startup_logs_dev_key_warning(monkeypatch, tmp_path):
    fake = FakeHTTP()
    service = configured_service(monkeypatch, tmp_path, fake, url="")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "1")
    status = service.startup_refresh()
    assert status["status"] == "offline"
    assert status["last_error"] is None
    assert status["dev_key_trusted"] is True
    assert status["dev_key_warning"] is True
    assert "not for production" in status["dev_key_warning_message"]
    assert fake.calls == []
    db = sqlite3.connect(tmp_path / "lynewright.sqlite3")
    try:
        rows = db.execute("SELECT message FROM application_logs WHERE message LIKE '%development catalogue key%'").fetchall()
    finally:
        db.close()
    assert rows


def test_dev_key_is_opt_in_and_operator_key_remains_trusted(monkeypatch, tmp_path):
    fixture_dir = Path(__file__).parent / "fixtures" / "catalogue" / "minisign-interop"
    encoded_dev = base64.b64decode(
        (fixture_dir / "minisign.pub").read_text(encoding="ascii").splitlines()[1],
        validate=True,
    )
    dev_key_id, dev_raw_public = encoded_dev[2:10], encoded_dev[10:]
    dev_public = base64.b64encode(encoded_dev).decode("ascii")
    monkeypatch.setattr(catalogue, "_BUILTIN_DEV_PUBLIC_KEY", dev_public)

    operator, operator_id, _seed, operator_public = signing_pair()
    configured_service(monkeypatch, tmp_path, FakeHTTP(), operator_public)
    body = (fixture_dir / "index.json").read_bytes()
    dev_signature = (fixture_dir / "prehashed.minisig").read_bytes()
    with pytest.raises(CatalogueError, match="unknown trusted key"):
        _verify_minisign(body, dev_signature)

    operator_signature = sign(body, operator, operator_id)
    label, is_dev = _verify_minisign(body, operator_signature)
    assert label == operator_id.hex()
    assert is_dev is False

    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "1")
    dev_label, is_dev = _verify_minisign(body, dev_signature)
    assert dev_label == f"dev:{dev_key_id.hex()}"
    assert is_dev is True
    assert _verify_minisign(body, operator_signature) == (operator_id.hex(), False)

    service = CatalogueService(http_service=FakeHTTP())
    status = service.status()
    assert status["dev_key_trusted"] is True
    assert status["dev_key_warning"] is True
    assert any(item["id"] == dev_label for item in status["trusted_key_ids"])
    assert len(dev_raw_public) == 32


def test_no_trusted_keys_refuse_refresh_and_catalogue_only_warns_at_startup(monkeypatch, tmp_path):
    fake = FakeHTTP()
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
    service = configured_service(monkeypatch, tmp_path, fake)
    status = service.startup_refresh()
    assert status["status"] == "untrusted"
    assert status["last_error"] == "no catalogue keys configured"
    assert status["trusted_key_ids"] == []
    assert status["dev_key_trusted"] is False
    assert fake.calls == []
    with sqlite3.connect(tmp_path / "lynewright.sqlite3") as db:
        warnings = db.execute(
            "SELECT message FROM application_logs WHERE level='warning' AND message LIKE '%catalogue%'"
        ).fetchall()
    assert any("no catalogue signing keys are trusted" in row[0].lower() for row in warnings)
    assert any("no integrations can be installed" in row[0].lower() for row in warnings)


def test_operator_key_id_collision_with_builtin_dev_key_refuses_startup(monkeypatch, tmp_path):
    fixture_dir = Path(__file__).parent / "fixtures" / "catalogue" / "minisign-interop"
    dev_public = base64.b64decode(
        (fixture_dir / "minisign.pub").read_text(encoding="ascii").splitlines()[1],
        validate=True,
    )
    monkeypatch.setattr(catalogue, "_BUILTIN_DEV_PUBLIC_KEY", base64.b64encode(dev_public).decode())
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", base64.b64encode(dev_public).decode())
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "collision.sqlite3"))
    with pytest.raises(RuntimeError, match="Catalogue key id collision"):
        CatalogueService(http_service=FakeHTTP()).startup_refresh()

def test_read_only_catalogue_routes_require_admin_and_startup_runs_offline(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_URL", "")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", "")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")
    monkeypatch.setenv("LYNEWRIGHT_ADMIN_TOKEN", "catalogue-route-test")
    monkeypatch.setenv("LYNEWRIGHT_RUNTIME_WORKER_ENABLED", "0")
    monkeypatch.setenv("LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED", "0")
    monkeypatch.setenv("LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS", "1")

    with TestClient(app) as client:
        denied = client.get("/api/catalogue")
        assert denied.status_code == 401
        headers = {"Authorization": "Bearer catalogue-route-test"}
        response = client.get("/api/catalogue", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"]["status"] == "untrusted"
        assert response.json()["status"]["last_error"] == "no catalogue keys configured"
        assert response.json()["entries"] == []
        refreshed = client.post("/api/catalogue/refresh", headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()["status"]["last_error"] == "no catalogue keys configured"
        missing = client.get("/api/catalogue/entries/missing/1.0.0", headers=headers)
        assert missing.status_code == 404

def test_sign_index_tool_emits_a_signature_the_service_verifies(monkeypatch, tmp_path):
    import subprocess
    import sys
    import tempfile

    _key, key_id, seed, public = signing_pair()
    body = fixture_bytes()
    configured_service(monkeypatch, tmp_path, FakeHTTP(), public)
    script = Path(__file__).parents[3] / "tools" / "catalogue" / "sign_index.py"
    with tempfile.TemporaryDirectory(prefix="lynewright-sign-index-") as directory:
        root = Path(directory)
        index_path = root / "catalogue.json"
        key_path = root / "temporary-seed.bin"
        signature_path = root / "catalogue.json.minisig"
        index_path.write_bytes(body)
        key_path.write_bytes(seed)
        result = subprocess.run(
            [
                sys.executable, str(script), "--index", str(index_path),
                "--key-file", str(key_path), "--output", str(signature_path),
                "--key-id", key_id.hex(),
            ],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        signature = signature_path.read_bytes()

    assert _verify_minisign(body, signature)[0] == key_id.hex()


def test_catalogue_internal_endpoint_ref_pins_index_signature_and_artifact_fetch(
    monkeypatch, tmp_path,
):
    key, key_id, _seed, public = signing_pair()
    index = json.loads(fixture_bytes())
    index["entries"][0]["source"] = "https://catalogue.example.test/artifacts/fixture.yaml"
    body = json.dumps(index, separators=(",", ":")).encode()
    signature = sign(body, key, key_id)
    url = "https://catalogue.example.test/catalogue.json"
    artifact = b"fixture artifact"

    class InternalHTTP:
        def __init__(self):
            self.calls = []

        def request(self, request_url, *, method, internal_endpoint_ref, **_kwargs):
            self.calls.append((request_url, method, internal_endpoint_ref))
            responses = {
                "http://catalogue.internal:8080/catalogue.json": body,
                "http://catalogue.internal:8080/catalogue.json.minisig": signature,
                "http://catalogue.internal:8080/artifacts/fixture.yaml": artifact,
            }
            return {"status_code": 200, "body": responses[request_url]}

    http = InternalHTTP()
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "lynewright.sqlite3"))
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_URL", url)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", public)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF", "catalogue_test")
    monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__CATALOGUE_TEST", "http://catalogue.internal:8080")
    service = CatalogueService(http_service=http)

    assert service.refresh()["status"] == "ready"
    assert service.fetch_artifact(
        index["entries"][0]["source"], max_bytes=1024, timeout_seconds=5,
    )["body"] == artifact
    assert [call[0] for call in http.calls] == [
        "http://catalogue.internal:8080/catalogue.json",
        "http://catalogue.internal:8080/catalogue.json.minisig",
        "http://catalogue.internal:8080/artifacts/fixture.yaml",
    ]
    assert all(call[1:] == ("GET", "catalogue_test") for call in http.calls)
    with pytest.raises(CatalogueError, match="configured catalogue origin"):
        service.fetch_artifact(
            "https://attacker.example.test/artifact.yaml", max_bytes=1024, timeout_seconds=5,
        )
    call_count = len(http.calls)
    with pytest.raises(CatalogueError, match="Buffered catalogue fetches"):
        service.fetch_artifact(
            index["entries"][0]["source"], max_bytes=10_000_001, timeout_seconds=5,
        )
    assert len(http.calls) == call_count



def test_empty_release_key_slot_adds_no_builtin_release_key(monkeypatch, tmp_path: Path) -> None:
    slot = tmp_path / "catalogue-release-key.pub"
    slot.write_bytes(b"")
    monkeypatch.setattr(catalogue, "RELEASE_KEY_PATH", slot)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", "")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")

    catalogue.validate_catalogue_key_configuration()

    assert catalogue._verified_key_map() == {}


def test_malformed_release_key_fails_during_application_startup(monkeypatch, tmp_path: Path) -> None:
    from fastapi import FastAPI
    from app import main

    slot = tmp_path / "catalogue-release-key.pub"
    slot.write_text("not-a-minisign-public-key", encoding="ascii")
    monkeypatch.setattr(catalogue, "RELEASE_KEY_PATH", slot)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", "")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")
    monkeypatch.setattr(main, "init_db", lambda: pytest.fail("database initialized before key validation"))

    async def start() -> None:
        async with main.lifespan(FastAPI()):
            pytest.fail("startup unexpectedly completed")

    with pytest.raises(RuntimeError, match="built-in release catalogue public key is malformed"):
        asyncio.run(start())
