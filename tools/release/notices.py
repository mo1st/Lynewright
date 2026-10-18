#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate a dependency and bundled-asset inventory for this repository."""
from __future__ import annotations
import argparse
import importlib.metadata as metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?(.*)$")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff"}
FONT_EXT = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
PERMISSIVE = {"MIT", "MIT-0", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "0BSD", "CC0-1.0", "Unlicense", "MPL-2.0", "Python-2.0"}

def git_files() -> list[str]:
    try:
        data = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        return [item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item]
    except (OSError, subprocess.CalledProcessError):
        return [path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts]

def python_license(name: str) -> tuple[str, str]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return "unknown (not installed in generator environment)", "version unavailable"
    info = dist.metadata
    license_value = info.get("License-Expression") or info.get("License") or ""
    if not license_value or license_value.strip().lower() in {"unknown", "n/a", "none"}:
        classifiers = info.get_all("Classifier") or []
        license_value = next((item.rsplit(" :: ", 1)[-1] for item in classifiers if item.startswith("License :: OSI Approved :: ")), "")
    # The 81.0.0 release metadata on PyPI declares setuptools under MIT, but
    # the wheel's Core Metadata omits that field. Resolve this pinned build
    # dependency from the release record rather than carrying an unknown label.
    if name.casefold() == "setuptools" and dist.version == "81.0.0" and not license_value.strip():
        license_value = "MIT"
    return (license_value.strip() if license_value.strip() else "unknown"), dist.version

def read_requirement_files() -> list[tuple[str, str, str, str, str]]:
    rows = []
    tracked = git_files()
    requirement_paths = sorted({p for p in tracked if Path(p).name.startswith("requirements") and Path(p).suffix == ".txt"})
    for root_name in requirement_paths:
        path = ROOT / root_name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.split("#", 1)[0].strip()
            if not value or value.startswith(("-", "git+", "http:", "https:")):
                continue
            match = PACKAGE_NAME.match(value)
            if not match:
                continue
            name, constraint = match.groups()
            declared, version = python_license(name)
            rows.append(("Python", name, version if version != "version unavailable" else (constraint.strip() or "not locked"), declared, root_name))
    for project_path in sorted(p for p in tracked if Path(p).name == "pyproject.toml"):
        path = ROOT / project_path
        if not path.is_file():
            continue
        metadata_doc = tomllib.loads(path.read_text(encoding="utf-8"))
        project = metadata_doc.get("project", {})
        build_requires = metadata_doc.get("build-system", {}).get("requires", [])
        for value in build_requires:
            match = PACKAGE_NAME.match(value)
            if not match:
                continue
            name, constraint = match.groups()
            declared, version = python_license(name)
            rows.append(("Python build", name, version if version != "version unavailable" else (constraint.strip() or "not locked"), declared, project_path + " [build-system]"))
        for value in project.get("dependencies", []):
            match = PACKAGE_NAME.match(value)
            if not match:
                continue
            name, constraint = match.groups()
            declared, version = python_license(name)
            rows.append(("Python", name, version if version != "version unavailable" else (constraint.strip() or "not locked"), declared, project_path))
    containerfile = ROOT / "runner/Containerfile"
    if containerfile.exists():
        for row in list(rows):
            if row[4] == "backend/requirements.txt":
                rows.append((row[0], row[1], row[2], row[3], "runner/Containerfile via backend/requirements.txt"))
    return rows

def npm_rows() -> list[tuple[str, str, str, str, str]]:
    lock_path = ROOT / "frontend/package-lock.json"
    if not lock_path.exists():
        return []
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    rows = []
    for path, item in lock.get("packages", {}).items():
        if not path.startswith("node_modules/"):
            continue
        package_path = path[len("node_modules/"):]
        if package_path.startswith("@"):
            parts = package_path.split("/")
            name = "/".join(parts[:2]) if len(parts) > 1 else package_path
        else:
            name = package_path.split("/", 1)[0]
        license_value = item.get("license", "unknown")
        if isinstance(license_value, dict):
            license_value = license_value.get("type", "unknown")
        if (item.get("name") or name, item.get("version")) == ("svg-tags", "1.0.0") and str(license_value).casefold() == "unknown":
            # The lock omits this field; the exact 1.0.0 package archive LICENSE is MIT.
            license_value = "MIT"
        rows.append(("npm", item.get("name") or name, item.get("version", "unknown"), license_value,
                     item.get("resolved", "frontend/package-lock.json")))
    return rows

def compatibility(license_value: str) -> bool:
    token = license_value.strip()
    if token.lower().startswith("unknown") or token.lower() in {"", "unlicensed", "none"}:
        # Missing labels are reported in the unknown section, not counted again
        # as a known incompatibility.
        return True
    if (ROOT / "LICENSE").read_text(encoding="utf-8", errors="ignore").lstrip().startswith("Apache License"):
        return token in PERMISSIVE
    if token in {"GPL-2.0-only", "GPL-2.0", "GPL-2.0-or-later", "AGPL-1.0-only", "AGPL-1.0-or-later"}:
        return False
    return True

def render() -> str:
    python = read_requirement_files()
    npm = npm_rows()
    rows = python + npm
    rows.sort(key=lambda row: (row[0].lower(), row[1].lower(), row[4].lower()))
    lines = ["# Third-party notices", "", "Generated by `tools/release/notices.py`. Python licence labels come from installed distribution metadata when available; npm labels and versions come from the committed package lock. Python requirement files and project metadata are listed by source. Where no Python lock exists, the version is the generator environment version or declared requirement, as marked.", "", "| Ecosystem | Package | Version | Licence as declared | Source |", "| --- | --- | --- | --- | --- |"]
    for ecosystem, name, version, license_value, source in rows:
        safe = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {safe(ecosystem)} | {safe(name)} | {safe(version)} | {safe(license_value)} | {safe(source)} |")
    unknown = [(name, source) for _eco, name, _version, lic, source in rows if not lic or lic.lower().startswith("unknown") or "version unavailable" in lic.lower()]
    incompatible = [(name, lic, source) for _eco, name, _version, lic, source in rows if not compatibility(lic)]
    files = git_files()
    assets = [(path, "font" if Path(path).suffix.lower() in FONT_EXT else "image") for path in files if Path(path).suffix.lower() in FONT_EXT | IMAGE_EXT]
    vendors = [path for path in files if any(part.lower() in {"vendor", "vendored", "third_party", "third-party"} for part in Path(path).parts)]
    lines += ["", "## Flags for operator review", ""]
    if unknown:
        lines.append(f"- Unknown or missing licence labels: {len(unknown)} dependency source rows. Review the `unknown` entries above.")
    else:
        lines.append("- Unknown or missing licence labels: none found by the current metadata scan.")
    if incompatible:
        lines.append(f"- Potentially incompatible licences: {len(incompatible)} dependency source rows. Review the package and declared licence above against this repository's licence.")
    else:
        lines.append("- Potentially incompatible licences: none detected by the conservative SPDX-label check; this is not legal advice.")
    copyleft = [(name, lic, source) for _eco, name, _version, lic, source in rows
                if any(term in lic for term in ("GPL-", "LGPL-", "AGPL-", "MPL-"))]
    if copyleft:
        lines.append(f"- Copyleft licences for operator compatibility review: {len(copyleft)} dependency source rows.")
        lines.extend(f"  - `{name}`: `{lic}` ({source})" for name, lic, source in copyleft)
    else:
        lines.append("- Copyleft licences for operator compatibility review: none detected.")
    if assets or vendors:
        lines.append(f"- Tracked fonts/images/vendor assets: {len(assets)} asset file(s), {len(vendors)} vendored path(s). Review these files:")
        lines.extend(f"  - `{kind}`: `{path}`" for path, kind in assets)
        lines.extend(f"  - vendored: `{path}`" for path in vendors)
    else:
        lines.append("- Tracked fonts/images/vendor assets: none found in the tracked/current source tree.")
    lines.append("- This inventory is informational; resolve flagged licence questions with the operator before distribution.")
    return "\n".join(lines) + "\n"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated notices differ")
    args = parser.parse_args()
    target = ROOT / "THIRD_PARTY_NOTICES.md"
    output = render()
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != output:
            print("THIRD_PARTY_NOTICES.md is stale.")
            return 1
        print("THIRD_PARTY_NOTICES.md is current.")
        return 0
    target.write_text(output, encoding="utf-8")
    print(f"Wrote {target.relative_to(ROOT).as_posix()} with {len(read_requirement_files()) + len(npm_rows())} dependency source rows.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
