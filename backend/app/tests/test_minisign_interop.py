from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.services import catalogue


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "catalogue" / "minisign-interop"
INDEX = FIXTURE_DIR / "index.json"
PUBLIC_KEY = FIXTURE_DIR / "minisign.pub"


def _fixture_key_map():
    encoded = base64.b64decode(PUBLIC_KEY.read_text(encoding="ascii").splitlines()[1], validate=True)
    assert encoded[:2] == b"Ed" and len(encoded) == 42
    key_id, public_bytes = encoded[2:10], encoded[10:]
    return {key_id: (f"interop:{key_id.hex()}", False, Ed25519PublicKey.from_public_bytes(public_bytes))}


@pytest.mark.parametrize(("signature_name", "algorithm"), [("prehashed.minisig", b"ED"), ("legacy.minisig", b"Ed")])
def test_minisign_release_signatures_verify_with_lynewright(monkeypatch, signature_name, algorithm):
    monkeypatch.setattr(catalogue, "_verified_key_map", _fixture_key_map)
    signature_bytes = (FIXTURE_DIR / signature_name).read_bytes()
    assert base64.b64decode(signature_bytes.decode("ascii").splitlines()[1], validate=True)[:2] == algorithm
    label, is_dev = catalogue._verify_minisign(INDEX.read_bytes(), signature_bytes)
    assert label.startswith("interop:")
    assert is_dev is False


@pytest.mark.parametrize("signature_name", ["prehashed.minisig", "legacy.minisig"])
def test_minisign_release_signatures_reject_changed_index_and_trusted_comment(monkeypatch, signature_name):
    monkeypatch.setattr(catalogue, "_verified_key_map", _fixture_key_map)
    signature = (FIXTURE_DIR / signature_name).read_bytes()
    changed_index = bytearray(INDEX.read_bytes())
    changed_index[0] ^= 1
    with pytest.raises(catalogue.CatalogueError, match="verification failed"):
        catalogue._verify_minisign(bytes(changed_index), signature)

    lines = signature.decode("ascii").splitlines()
    lines[2] += " tampered"
    with pytest.raises(catalogue.CatalogueError, match="verification failed"):
        catalogue._verify_minisign(INDEX.read_bytes(), ("\n".join(lines) + "\n").encode("ascii"))


def _minisign_binary() -> str | None:
    configured = os.environ.get("LYNEWRIGHT_MINISIGN_BIN")
    if configured and Path(configured).is_file():
        return configured
    on_path = shutil.which("minisign")
    if on_path:
        return on_path
    root = Path(tempfile.gettempdir()) / "lynewright-k52-minisign-0.12" / "minisign-win64"
    for architecture in ("x86_64", "aarch64"):
        candidate = root / architecture / "minisign.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def test_lynewright_signature_is_accepted_by_real_minisign(tmp_path):
    binary = _minisign_binary()
    if binary is None:
        pytest.skip("real minisign is absent; set LYNEWRIGHT_MINISIGN_BIN to run reverse interoperability")

    seed = os.urandom(32)
    key_id = os.urandom(8)
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    index_path = tmp_path / "catalogue.json"
    index_path.write_bytes(INDEX.read_bytes())
    seed_path = tmp_path / "signing-seed.bin"
    seed_path.write_bytes(seed)
    public_path = tmp_path / "minisign.pub"
    public_path.write_text(
        "untrusted comment: Lynewright interoperability test key\n"
        + base64.b64encode(b"Ed" + key_id + public_bytes).decode("ascii")
        + "\n",
        encoding="ascii",
    )
    signature_path = tmp_path / "catalogue.json.minisig"
    signer = Path(__file__).parents[3] / "tools" / "catalogue" / "sign_index.py"
    signing = subprocess.run(
        [sys.executable, str(signer), "--index", str(index_path), "--key-file", str(seed_path),
         "--output", str(signature_path), "--key-id", key_id.hex()],
        capture_output=True, text=True, check=False,
    )
    assert signing.returncode == 0, signing.stderr or signing.stdout

    verified = subprocess.run(
        [binary, "-V", "-m", str(index_path), "-x", str(signature_path), "-p", str(public_path)],
        capture_output=True, text=True, check=False,
    )
    output = (verified.stdout + verified.stderr).strip()
    print(f"real minisign -V exit={verified.returncode}: {output}")
    assert verified.returncode == 0, output
    assert "Signature and comment signature verified" in output
