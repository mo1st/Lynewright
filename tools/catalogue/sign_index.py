#!/usr/bin/env python3
"""Create a detached minisign-format signature for exact catalogue index bytes.

The key file is a raw 32-byte Ed25519 seed and must be kept outside source control.
This signer uses the hashed ED variant: Ed25519(BLAKE2b-512(index_bytes)).
"""
from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def sign_index(index_bytes: bytes, seed: bytes, key_id: bytes, filename: str) -> bytes:
    if len(seed) != 32 or len(key_id) != 8:
        raise ValueError("The signing seed must be 32 bytes and the key id must be 8 bytes.")
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    digest = hashlib.blake2b(index_bytes, digest_size=64).digest()
    signature = private_key.sign(digest)
    trusted_comment = (
        f"timestamp:{int(datetime.now(UTC).timestamp())} file:{Path(filename).name} hashed"
    )
    global_signature = private_key.sign(signature + trusted_comment.encode("ascii"))
    primary = base64.b64encode(b"ED" + key_id + signature).decode("ascii")
    global_encoded = base64.b64encode(global_signature).decode("ascii")
    return (
        "untrusted comment: signature from Lynewright catalogue signer\n"
        f"{primary}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{global_encoded}\n"
    ).encode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path, help="catalogue.json input")
    parser.add_argument("--key-file", required=True, type=Path, help="outside-repository raw 32-byte Ed25519 seed")
    parser.add_argument("--output", type=Path, help="signature output (default: <index>.minisig)")
    parser.add_argument("--key-id", help="8-byte key id as 16 hexadecimal characters")
    args = parser.parse_args()
    seed = args.key_file.read_bytes()
    if len(seed) != 32:
        parser.error("--key-file must contain exactly a raw 32-byte Ed25519 seed")
    if args.key_id:
        try:
            key_id = bytes.fromhex(args.key_id)
        except ValueError:
            parser.error("--key-id must be 16 hexadecimal characters")
        if len(key_id) != 8:
            parser.error("--key-id must be 16 hexadecimal characters")
    else:
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        key_id = hashlib.sha256(public).digest()[:8]
    output = args.output or Path(str(args.index) + ".minisig")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(sign_index(args.index.read_bytes(), seed, key_id, args.index.name))
    print(f"Wrote detached signature to {output}")
    print(f"Key id: {key_id.hex()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
