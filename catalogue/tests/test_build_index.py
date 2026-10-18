from __future__ import annotations
import hashlib, json, os
from pathlib import Path
import sys
import jsonschema
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
import build_index
from build_index import SCHEMA, build_native_wheel, load_entries

def test_native_wheel_is_byte_deterministic_from_pinned_source():
    native=ROOT/"sources/template/native"
    first_name,first=build_native_wheel(native,epoch=1792108800)
    second_name,second=build_native_wheel(native,epoch=1792108800)
    assert first_name=="lynewright_integration_example-1.0.0-py3-none-any.whl"
    assert first==second
    assert hashlib.sha256(first).hexdigest()==hashlib.sha256(second).hexdigest()

def test_four_entry_drafts_are_pinned_and_schema_supported():
    entries=load_entries()
    assert len(entries)==4
    schema=json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert {item["kind"] for item in entries}=={"file","package","container"}
    assert all(item["build"]["template_commit"]==(ROOT/"sources/template/COMMIT").read_text(encoding="ascii").strip() for item in entries)

def test_published_index_is_deterministic_and_schema_valid():
    index_path=ROOT/"dist/catalogue.json"
    assert index_path.is_file()
    encoded=index_path.read_bytes()
    parsed=json.loads(encoded)
    schema=json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(parsed,schema,format_checker=jsonschema.FormatChecker())
    assert encoded==json.dumps(parsed,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")

def test_container_build_requires_docker_buildx_without_fabricating_a_digest(monkeypatch):
    monkeypatch.setattr(build_index.shutil, "which", lambda _name: None)
    entry=next(item for item in load_entries() if item["kind"]=="container")
    with pytest.raises(FileNotFoundError, match="Docker Buildx is required"):
        build_index.build_container(entry,ROOT/"dist")


def test_artifact_license_is_required_spdx_metadata():
    entries=load_entries()
    assert len(entries)==4
    schema=json.loads(SCHEMA.read_text(encoding="utf-8"))
    catalogue={"format":"lynewright-catalogue/1","serial":1,"generated_at":"2026-10-16T00:00:00Z",
               "name":"fixture","entries":[]}
    for entry in entries:
        public={key:value for key,value in entry.items() if key not in {"build","_entry_path"}}
        assert public["licence"]=="Apache-2.0"
        if public["kind"]=="container":
            public["source"]="registry.example.test/catalogue/thumbnail@sha256:"+"a"*64
        catalogue["entries"]=[public]
        jsonschema.validate(catalogue,schema,format_checker=jsonschema.FormatChecker())
        invalid=dict(public)
        invalid.pop("licence")
        catalogue["entries"]=[invalid]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(catalogue,schema,format_checker=jsonschema.FormatChecker())
        invalid=dict(public)
        invalid["licence"]="not a SPDX identifier"
        catalogue["entries"]=[invalid]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(catalogue,schema,format_checker=jsonschema.FormatChecker())
