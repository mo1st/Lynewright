#!/usr/bin/env python3
"""Build exact catalogue artifacts and the deterministic signed-index payload."""
from __future__ import annotations
import argparse, base64, hashlib, json, os, re, shutil, subprocess, tarfile, tomllib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote
import jsonschema, yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "catalogue.schema.json"
STATE = ROOT / "state.json"
DIST = ROOT / "dist"
EPOCH_DEFAULT = 1792108800
IMAGE_REF_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[a-fA-F0-9]{64}$")

def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

def _safe_source(relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe source snapshot path: {relative}")
    candidate = (ROOT / Path(*path.parts)).resolve()
    candidate.relative_to(ROOT.resolve())
    if not candidate.exists():
        raise ValueError(f"Source snapshot is missing: {relative}")
    return candidate

def load_entries() -> list[dict]:
    values = []
    for path in sorted((ROOT / "entries").glob("*/*.yaml")):
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("build"), dict):
            raise ValueError(f"{path.relative_to(ROOT)} must contain an entry and build block.")
        value["_entry_path"] = path.relative_to(ROOT).as_posix()
        values.append(value)
    if not values:
        raise ValueError("No catalogue entries were found.")
    return values

def _zip_bytes(files: dict[str, bytes], *, epoch: int) -> bytes:
    import io, zipfile
    timestamp = datetime.fromtimestamp(max(epoch, 315532800), UTC)
    stamp = (min(timestamp.year, 2107), timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second - timestamp.second % 2)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()

def build_native_wheel(source_root: Path, *, epoch: int) -> tuple[str, bytes]:
    """Build a standards-compliant pure-Python wheel without importing starter modules."""
    project = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = str(project["name"]), str(project["version"])
    points = project.get("entry-points", {}).get("lynewright.integrations", {})
    if len(points) != 1:
        raise ValueError("Native starter must have exactly one lynewright.integrations entry point.")
    dist_stem = re.sub(r"[-_.]+", "_", name)
    dist_info = f"{dist_stem}-{version}.dist-info"
    package_dir = source_root / "example_native"
    files = {
        "example_native/" + item.name: item.read_bytes()
        for item in sorted(package_dir.iterdir())
        if item.is_file() and item.suffix in {".py", ".json"}
    }
    if "example_native/lynewright-manifest.json" not in files:
        raise ValueError("Native package data manifest is missing.")
    metadata = (
        "Metadata-Version: 2.1\n" f"Name: {name}\nVersion: {version}\n"
        f"Summary: {project.get('description', '')}\n"
        f"Requires-Python: {project.get('requires-python', '>=3.12')}\n\n"
    ).encode()
    wheel = (
        "Wheel-Version: 1.0\nGenerator: lynewright-catalogue-build\n"
        "Root-Is-Purelib: true\nTag: py3-none-any\n\n"
    ).encode()
    entry_points = "[lynewright.integrations]\n" + "".join(f"{key} = {value}\n" for key, value in sorted(points.items()))
    files[f"{dist_info}/METADATA"] = metadata
    files[f"{dist_info}/WHEEL"] = wheel
    files[f"{dist_info}/entry_points.txt"] = entry_points.encode()
    records = []
    for path, body in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode("ascii")
        records.append(f"{path},sha256={digest},{len(body)}\n")
    record_path = f"{dist_info}/RECORD"
    records.append(f"{record_path},,\n")
    files[record_path] = "".join(records).encode()
    return f"{dist_stem}-{version}-py3-none-any.whl", _zip_bytes(files, epoch=epoch)

def _oci_manifest_digest(archive_path: Path) -> str:
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            item = archive.extractfile("index.json")
            if item is None:
                raise ValueError("OCI archive has no index.json.")
            descriptors = json.loads(item.read()).get("manifests", [])
            if len(descriptors) != 1:
                raise ValueError("Expected a single-platform OCI index with one manifest.")
            digest = str(descriptors[0].get("digest") or "")
            algorithm, separator, hexdigest = digest.partition(":")
            if not separator or algorithm != "sha256" or not re.fullmatch(r"[a-f0-9]{64}", hexdigest):
                raise ValueError("OCI archive manifest digest is malformed.")
            blob = archive.extractfile(f"blobs/sha256/{hexdigest}")
            if blob is None or hashlib.sha256(blob.read()).hexdigest() != hexdigest:
                raise ValueError("OCI manifest blob is missing or its digest is wrong.")
            return digest
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ValueError("Builder output is not a valid OCI image archive.") from exc

def build_container(entry: dict, artifact_dir: Path) -> tuple[str, Path]:
    builder = shutil.which("docker")
    if not builder:
        raise FileNotFoundError("Docker Buildx is required to build the thumbnail OCI image.")
    info = entry["build"]
    source_root = _safe_source(str(info["source_snapshot"]))
    rel = PurePosixPath(str(info.get("containerfile") or ""))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Containerfile path must stay within the pinned source snapshot.")
    dockerfile = source_root / Path(*rel.parts)
    if not dockerfile.is_file():
        raise ValueError(f"Containerfile is missing: {dockerfile}")
    archive_path = artifact_dir / str(info.get("artifact_filename") or "thumbnail.oci.tar")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        builder, "buildx", "build", "--platform", "linux/amd64",
        "--provenance=false", "--sbom=false", "--output", f"type=oci,dest={archive_path}",
        "--file", str(dockerfile), str(source_root),
    ], check=True, cwd=ROOT)
    digest = _oci_manifest_digest(archive_path)
    registry = str(info.get("registry_image") or "").rstrip("/")
    if not registry:
        raise ValueError("Container entry must declare build.registry_image.")
    return f"{registry}@{digest}", archive_path

def _read_state() -> dict:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("state must be an object")
        return value
    except FileNotFoundError:
        return {"serial": 0, "generated_at": "", "input_sha256": ""}

def _entry_for_schema(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key not in {"build", "_entry_path"}}

def build(*, base_url: str, allow_missing_container: bool, build_container_image: bool,
          refresh_entry_hashes: bool, bump_serial: bool, check: bool) -> tuple[bytes, dict[str, bytes], list[str]]:
    if not base_url.startswith("https://"):
        raise ValueError("--base-url must be an HTTPS URL.")
    base_url = base_url.rstrip("/")
    entries = load_entries()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    blobs: dict[str, bytes] = {}
    published: list[dict] = []
    notes: list[str] = []
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", EPOCH_DEFAULT))
    updates: dict[Path, dict] = {}

    for entry in entries:
        kind, info = entry.get("kind"), entry["build"]
        if kind == "container":
            source = str(entry.get("source") or "")
            if build_container_image:
                try:
                    source, archive_path = build_container(entry, DIST / "artifacts")
                    blobs[f"artifacts/{archive_path.name}"] = archive_path.read_bytes()
                    if not check:
                        entry["source"] = source
                        updates[ROOT / entry["_entry_path"]] = entry
                except FileNotFoundError as exc:
                    if not allow_missing_container:
                        raise
                    notes.append(f"container skipped: {exc}")
                    continue
            elif IMAGE_REF_RE.fullmatch(source):
                archive_name = str(info.get("artifact_filename") or "")
                archive_path = DIST / "artifacts" / archive_name
                if archive_path.is_file():
                    body = archive_path.read_bytes()
                    if _oci_manifest_digest(archive_path) != source.rsplit("@", 1)[-1]:
                        raise ValueError("Stored OCI archive does not match the signed image digest.")
                    blobs[f"artifacts/{archive_name}"] = body
                elif allow_missing_container:
                    notes.append("container entry omitted: its OCI archive is missing")
                    continue
                else:
                    raise FileNotFoundError("Container OCI archive is not built; refusing an unverified image.")
            elif allow_missing_container:
                notes.append("container entry omitted: no real immutable OCI digest is available")
                continue
            else:
                raise ValueError("Container entry has no real immutable image digest; build it before publishing.")
            final = _entry_for_schema(entry)
            published.append(final)
            continue

        source_path = _safe_source(str(info["source_snapshot"]))
        artifact_name = str(info["artifact_filename"])
        if kind == "file":
            body = source_path.read_bytes()
        elif kind == "package":
            artifact_name, body = build_native_wheel(source_path, epoch=epoch)
        else:
            raise ValueError(f"Unsupported entry kind: {kind!r}")
        digest = hashlib.sha256(body).hexdigest()
        if entry.get("sha256") != digest:
            if not refresh_entry_hashes:
                raise ValueError(f"{entry['_entry_path']} sha256 is stale; rerun with --refresh-entry-hashes.")
            entry["sha256"] = digest
            updates[ROOT / entry["_entry_path"]] = entry
        canonical_source = f"{base_url}/{quote(artifact_name, safe='.-_')}"
        if entry.get("source") != canonical_source:
            entry["source"] = canonical_source
            updates[ROOT / entry["_entry_path"]] = entry
        blobs[f"artifacts/{artifact_name}"] = body
        final = _entry_for_schema(entry)
        published.append(final)

    if not published:
        raise ValueError("There are no buildable catalogue entries.")
    index_data = {
        "format": "lynewright-catalogue/1",
        "name": "Lynewright Community Integration Catalogue",
        "entries": sorted(published, key=lambda item: (item["id"], item["version"])),
    }
    state = _read_state()
    fingerprint = hashlib.sha256(canonical_json({
        "entries": index_data["entries"],
        "artifacts": {key: hashlib.sha256(body).hexdigest() for key, body in sorted(blobs.items())},
    })).hexdigest()
    serial = int(state.get("serial") or 0)
    generated_at = str(state.get("generated_at") or "")
    previous = str(state.get("input_sha256") or "")
    if serial == 0:
        serial = 1
        generated_at = generated_at or datetime.fromtimestamp(epoch, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    elif fingerprint != previous:
        if check or not bump_serial:
            raise ValueError("Catalogue inputs changed; review them and rerun with --bump-serial.")
        serial += 1
        generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    elif not generated_at:
        raise ValueError("Catalogue state is missing generated_at.")
    index = {**index_data, "serial": serial, "generated_at": generated_at}
    encoded = canonical_json(index)
    jsonschema.validate(index, schema, format_checker=jsonschema.FormatChecker())

    if check:
        index_file = DIST / "catalogue.json"
        if not index_file.is_file() or index_file.read_bytes() != encoded:
            raise ValueError("dist/catalogue.json is stale; rebuild it from pinned inputs.")
        for relative, body in blobs.items():
            path = DIST / relative
            if not path.is_file() or path.read_bytes() != body:
                raise ValueError(f"dist/{relative} is stale or missing.")
    else:
        for path, entry in updates.items():
            clean = _entry_for_schema(entry)
            clean["build"] = entry["build"]
            path.write_text(yaml.safe_dump(clean, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="")
        for relative, body in blobs.items():
            path = DIST / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        DIST.mkdir(parents=True, exist_ok=True)
        (DIST / "catalogue.json").write_bytes(encoded)
        STATE.write_text(json.dumps({
            "serial": serial, "generated_at": generated_at,
            "input_sha256": fingerprint, "source_date_epoch": epoch,
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="")
    return encoded, blobs, notes

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("LYNEWRIGHT_CATALOGUE_BASE_URL", "https://catalogue.example.invalid/artifacts"))
    parser.add_argument("--allow-missing-container", action="store_true")
    parser.add_argument("--build-container", action="store_true")
    parser.add_argument("--refresh-entry-hashes", action="store_true")
    parser.add_argument("--bump-serial", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        encoded, artifacts, notes = build(
            base_url=args.base_url, allow_missing_container=args.allow_missing_container,
            build_container_image=args.build_container, refresh_entry_hashes=args.refresh_entry_hashes,
            bump_serial=args.bump_serial, check=args.check,
        )
    except Exception as exc:
        print(f"catalogue build failed: {exc}")
        return 1
    index = json.loads(encoded)
    print(f"serial={index['serial']} entries={len(index['entries'])} sha256={hashlib.sha256(encoded).hexdigest()}")
    for name, body in sorted(artifacts.items()):
        print(f"artifact {name} sha256={hashlib.sha256(body).hexdigest()} bytes={len(body)}")
    for note in notes:
        print(f"NOTE: {note}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
