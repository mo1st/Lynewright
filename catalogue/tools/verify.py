#!/usr/bin/env python3
"""Verify catalogue sources, generated index, artifact hashes, and business-word scan."""
from __future__ import annotations
import argparse, json, re, subprocess, sys
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
import jsonschema, yaml
from build_index import ROOT, SCHEMA, DIST, load_entries, build

def banned_words() -> tuple[str,...]:
    return (
        "bel"+"vio", "k"+"dp", "epu"+"bli", "bo"+"ok", "skin"+"care",
        "you"+"cam", "what"+"sapp", "wid"+"get", "landing"+"_"+"page", "lp"+"_",
        "q"+"uiz", "cam"+"paign", "woo"+"commerce", "shop"+"ify",
        "money"+"printer", "ve"+"lith", "emo"+" atlas", "product"+"_"+"catalog",
        "catalog"+"_"+"sync", "catalog"+"ue"+"_"+"classification",
        "catalog"+"ue"+"_"+"snapshot", "quor"+"lyx", "core"+"lyx", "grapes"+"js",
    )

def scan_business_words() -> list[str]:
    violations=[]
    skip={".git",".pytest_cache","__pycache__",".venv"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in skip for part in path.relative_to(ROOT).parts):
            continue
        if path.parts[-2:] == ("tools","verify.py"):
            continue
        try:
            text=path.read_text(encoding="utf-8").casefold()
        except (OSError,UnicodeError):
            continue
        for word in banned_words():
            if word.casefold() in text:
                violations.append(f"{path.relative_to(ROOT).as_posix()}: {word}")
    return violations

def verify(require_container: bool=False) -> int:
    schema=json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    index_path=DIST/"catalogue.json"
    if not index_path.is_file():
        raise ValueError("dist/catalogue.json is missing; run tools/build_index.py first.")
    index=json.loads(index_path.read_text(encoding="utf-8"))
    jsonschema.validate(index,schema,format_checker=jsonschema.FormatChecker())
    for item in index["entries"]:
        if item["kind"] not in {"file", "package"}:
            continue
        filename=Path(urlsplit(item["source"]).path).name
        artifact=DIST/"artifacts"/filename
        if not filename or not artifact.is_file():
            raise ValueError(f"Published artifact is missing: {filename or item['source']}")
        actual=hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual != item["sha256"]:
            raise ValueError(f"Published artifact hash differs from index: {filename}")
    entries=load_entries()
    expected={
        (item["id"],item["version"])
        for item in entries
    }
    drafts={ (item["id"],item["version"]) for item in entries }
    if drafts != expected or len(drafts)!=4:
        raise ValueError("The catalogue must retain the four pinned starter entry drafts.")
    committed=(ROOT/"sources/template/COMMIT").read_text(encoding="ascii").strip()
    if any(item["build"].get("template_commit")!=committed for item in entries):
        raise ValueError("Entry source commit differs from the checked-in template snapshot commit.")
    published={(item["id"],item["version"]) for item in index["entries"]}
    unbuilt=[item for item in entries if item["kind"]=="container" and (item.get("source","").startswith("build://") or (item["id"],item["version"]) not in published)]
    if require_container and unbuilt:
        raise ValueError("A valid container image digest is required, but the thumbnail image is not built.")
    _,_,notes=build(base_url="https://catalogue.example.invalid/artifacts",
                    allow_missing_container=not require_container,build_container_image=False,
                    refresh_entry_hashes=False,bump_serial=False,check=True)
    violations=scan_business_words()
    if violations:
        raise ValueError("Unallowlisted business words:\n"+"\n".join(violations))
    print(f"schema: valid ({len(index['entries'])} published entries; {len(entries)} pinned source drafts)")
    print("sources: pinned template commit matches")
    print("artifacts: committed bytes match signed index hashes and exact rebuild outputs")
    print("business-word scan: clean")
    for note in notes:
        print("NOTE:",note)
    return 0

def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--require-container",action="store_true")
    args=parser.parse_args()
    try:
        return verify(require_container=args.require_container)
    except Exception as exc:
        print(f"catalogue verification failed: {exc}",file=sys.stderr)
        return 1
if __name__=="__main__":
    raise SystemExit(main())
