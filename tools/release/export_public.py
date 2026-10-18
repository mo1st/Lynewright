#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build and validate the local, fresh-history public repository from committed archives."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable

AUTHOR_NAME = "Lynewright maintainers"
AUTHOR_EMAIL = "156139896+mo1st@users.noreply.github.com"
COMMIT_MESSAGE = "Initial public release"
CORE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = CORE_ROOT.parent / "public" / "lynewright"
DEFAULT_IDENTITY_LIST = os.environ.get("LYNEWRIGHT_IDENTITY_SCAN_LIST", "")
MAX_FILE_BYTES = 5 * 1024 * 1024

ROOT_CI = """name: Lynewright checks
on: [push, pull_request]
permissions:
  contents: read
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          fetch-depth: 0
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.12"
      - uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0
        with:
          node-version: "22"
      - name: Export hygiene
        run: python tools/release/export_public.py --check-tree
      - name: Identity scan (skips when the private list is absent)
        run: python tools/release/identity_scan.py --history
      - name: DCO
        if: github.event_name == 'pull_request'
        run: python tools/release/check_dco.py
      - name: Install Python test dependencies
        run: |
          python -m pip install setuptools==81.0.0
          python -m pip install ./backend -e ./testkit
      - name: Backend tests and guards
        working-directory: backend
        run: python -m pytest app/tests
      - name: Testkit
        run: python -m pytest testkit/tests
      - name: OpenAPI starter (including Lynewright tests)
        working-directory: templates/starters/openapi
        run: |
          python -m pip install -r requirements-test.txt
          python -m pytest tests
      - name: MCP starter (including Lynewright tests)
        working-directory: templates/starters/mcp
        run: |
          python -m pip install -r requirements-test.txt
          python -m pytest tests
      - name: Native starter (including Lynewright tests)
        working-directory: templates/starters/native
        run: |
          python -m pip install -r requirements-test.txt
          python -m pip install -e .
          python -m pytest tests
      - name: CLI wrapper starter
        working-directory: templates/starters/cli-wrapper
        run: |
          python -m pip install -r requirements-test.txt
          python -m pytest tests
      - name: Catalogue verification and tests
        working-directory: catalogue
        run: |
          python -m pip install -r requirements-test.txt
          python tools/verify.py
          python -m pytest -q --ignore=sources
      - name: Documentation snippets
        run: python tools/docs/sync_snippets.py --check
      - name: Frontend tests and build
        working-directory: frontend
        run: |
          npm ci
          npm test
          npm run build
  secrets:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e # v3.0.0
        env:
          GITHUB_TOKEN: ${{ github.token }}
"""

EXCLUDED_PATHS = (
    "docs/extraction-log.md",
    "tools/extraction",
    "tools/release/rename_identity.py",
    "backend/data",
    "templates/starters/.github",
    "templates/starters/THIRD_PARTY_NOTICES.md",
    "catalogue/.github",
    "catalogue/THIRD_PARTY_NOTICES.md",
)

EXCLUSION_REASONS = {
    "backend/data/": "the source contains only an empty runtime-data sentinel; no data directory or database belongs in the public export.",
    "catalogue/.github/": "the catalogue’s nested repository workflow is replaced by the merged root CI workflow.",
    "catalogue/THIRD_PARTY_NOTICES.md": "the nested notice file is replaced by the regenerated combined notice at the repository root.",
    "docs/extraction-log.md": "private extraction journal; it records internal implementation history and is not product documentation.",
    "tools/extraction/": "private extraction-only scripts; they are not part of the public product or release process.",
    "tools/release/rename_identity.py": "one-time private identity migration helper; it is not needed to build or maintain the public export.",
    "templates/starters/.github/": "the starters’ nested repository workflow is replaced by the merged root CI workflow, which runs all four starters.",
    "templates/starters/THIRD_PARTY_NOTICES.md": "the nested notice file is replaced by the regenerated combined notice at the repository root.",
}


def exclusion_reason(path: str) -> str:
    if path in EXCLUSION_REASONS:
        return EXCLUSION_REASONS[path]
    if path.startswith("docs/extraction-"):
        return "private extraction-generated inventory or route audit; it is replaced by the public project documentation and merged route checks."
    raise RuntimeError(f"No public-export exclusion reason is recorded for {path}.")


def command(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
            timeout: int = 3600, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                            errors="replace", timeout=timeout, check=False)
    if check and result.returncode:
        tail = (result.stdout + result.stderr).splitlines()[-240:]
        raise RuntimeError(f"Command failed ({result.returncode}): {args!r}\n" + "\n".join(tail))
    return result


def git(root: Path, *args: str, check: bool = True) -> str:
    return command(["git", "-C", str(root), *args], check=check).stdout.strip()


def safe_extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise RuntimeError(f"Unsafe path in Git archive: {member.name}")
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise RuntimeError(f"Unsafe link in Git archive: {member.name}")
        archive.extractall(destination, members=members, filter="data")
    for path in destination.rglob("*"):
        path.resolve().relative_to(root)


def archive_head(repo: Path, destination: Path) -> str:
    head = git(repo, "rev-parse", "HEAD")
    with destination.open("wb") as output:
        subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "archive", "--format=tar", "HEAD"],
                       stdout=output, stderr=subprocess.PIPE, check=True)
    return head


def remove_export_path(root: Path, relative: str, removed: list[str]) -> None:
    path = root / Path(relative)
    if path.is_dir():
        shutil.rmtree(path)
        removed.append(relative.rstrip("/") + "/")
    elif path.exists():
        path.unlink()
        removed.append(relative)


def replace_if_present(root: Path, relative: str, replacements: Iterable[tuple[str, str]]) -> None:
    path = root / Path(relative)
    if not path.is_file():
        raise RuntimeError(f"Expected archived file is missing: {relative}")
    text = path.read_text(encoding="utf-8")
    original = text
    for before, after in replacements:
        text = text.replace(before, after)
    if text == original:
        raise RuntimeError(f"Expected public-tree fix-up did not apply to {relative}")
    path.write_text(text, encoding="utf-8", newline="\n")


def update_contributing(root: Path) -> None:
    path = root / "CONTRIBUTING.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("Catalogue, from the catalogue repository root:", "Catalogue tooling (run these commands from `catalogue/`):")
    text = text.replace("Template starters, from the template repository root:", "Template starters (all four, including Lynewright-marked tests):")
    old_catalogue = """```powershell
python -m pip install -r requirements-test.txt
python tools/verify.py
python -m pytest -q
```"""
    new_catalogue = """```powershell
Set-Location catalogue
python -m pip install -r requirements-test.txt
python tools/verify.py
python -m pytest -q --ignore=sources
Set-Location ..
```"""
    text = text.replace(old_catalogue, new_catalogue)
    old_starters = """```powershell
python -m pip install -r openapi/requirements-test.txt
python -m pytest openapi/tests -m "not lynewright"
python -m pip install -r mcp/requirements-test.txt
python -m pytest mcp/tests -m "not lynewright"
python -m pip install -r native/requirements-test.txt
python -m pytest native/tests -m "not lynewright"
python -m pip install -r cli-wrapper/requirements-test.txt
python -m pytest cli-wrapper/tests
```"""
    new_starters = """```powershell
python -m pip install ./backend -e ./testkit
Push-Location templates/starters/openapi
python -m pip install -r requirements-test.txt
python -m pytest tests
Pop-Location
Push-Location templates/starters/mcp
python -m pip install -r requirements-test.txt
python -m pytest tests
Pop-Location
Push-Location templates/starters/native
python -m pip install -r requirements-test.txt
python -m pip install -e .
python -m pytest tests
Pop-Location
Push-Location templates/starters/cli-wrapper
python -m pip install -r requirements-test.txt
python -m pytest tests
Pop-Location
```"""
    text = text.replace(old_starters, new_starters)
    text = text.replace("The template CI runs the same starter matrix. The first three starters additionally have Lynewright-dependent tests that run when Lynewright and `lynewright-testkit` are installed.", "The root CI runs every starter test, including the `lynewright`-marked tests, after installing the backend and testkit from this repository.")
    text = text.replace("Use `lynewright` until the public organisation is selected.", "Public project links use the `lynewright` organisation name.")
    if text == path.read_text(encoding="utf-8"):
        raise RuntimeError("CONTRIBUTING.md fix-ups did not apply")
    path.write_text(text, encoding="utf-8", newline="\n")


def signed_catalogue_paths(root: Path) -> set[str]:
    import json
    from urllib.parse import urlsplit
    index = root / "catalogue" / "dist" / "catalogue.json"
    allow = {"catalogue/dist/catalogue.json", "catalogue/dist/catalogue.json.minisig"}
    payload = json.loads(index.read_text(encoding="utf-8"))
    for entry in payload.get("entries", []):
        if entry.get("kind") not in {"file", "package"}:
            continue
        name = Path(urlsplit(str(entry.get("source", ""))).path).name
        if name:
            allow.add(f"catalogue/dist/artifacts/{name}")
    return allow


def check_tree(root: Path) -> list[str]:
    errors: list[str] = []
    banned = ("".join(("core", "lyx")), "".join(("quor", "lyx")))
    allowed_large = signed_catalogue_paths(root)
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if ".git" in Path(relative).parts:
            continue
        if path.is_dir():
            if relative in {"data", "backend/data", "frontend/data"}:
                errors.append(f"runtime data directory: {relative}")
            continue
        if path.name == ".env" or path.suffix.casefold() in {".pem", ".seed"} or path.name.casefold().endswith((".minisig.key", ".minisig.secret")):
            errors.append(f"sensitive filename: {relative}")
        body = path.read_bytes()
        lowered = body.lower()
        if re.search(rb"-----begin [a-z0-9 ]*private key-----", lowered):
            errors.append(f"private-key material: {relative}")
        for term in banned:
            if term.encode() in lowered:
                errors.append(f"private working name in tree: {relative}")
        if path.stat().st_size > MAX_FILE_BYTES and relative not in allowed_large:
            errors.append(f"file over 5 MiB outside signed catalogue artifacts: {relative} ({path.stat().st_size} bytes)")
    return errors


def print_gate(label: str, result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"{label} failed ({result.returncode}):\n{output[-8000:]}")
    lines = (result.stdout + result.stderr).strip().splitlines()
    summary = " | ".join(line.strip() for line in lines[-5:] if line.strip()) or "passed"
    text = f"{label}: PASS — {summary}"
    print(text, flush=True)
    return text


def run_gate(label: str, args: list[str], *, cwd: Path, env: dict[str, str] | None = None,
             timeout: int = 3600) -> str:
    return print_gate(label, command(args, cwd=cwd, env=env, timeout=timeout))


def validate_git(repo: Path, author_name: str, author_email: str) -> str:
    branch = git(repo, "branch", "--show-current")
    count = git(repo, "rev-list", "--count", "HEAD")
    remotes = git(repo, "remote", "-v")
    if branch != "main" or count != "1" or remotes:
        raise RuntimeError(f"Export must be main, one commit, and no remotes (branch={branch!r}, commits={count}, remotes={remotes!r})")
    metadata = git(repo, "show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", "HEAD").splitlines()
    if len(metadata) < 5 or metadata[0:4] != [author_name, author_email, author_name, author_email]:
        raise RuntimeError("Export commit author or committer does not match the configured public identity.")
    signoff = f"Signed-off-by: {author_name} <{author_email}>"
    if signoff not in metadata[4:]:
        raise RuntimeError("Initial export commit is missing the matching DCO sign-off.")
    if COMMIT_MESSAGE not in metadata[4:]:
        raise RuntimeError("Export commit message is not the required initial release message.")
    return git(repo, "rev-parse", "HEAD")


def run_suites(repo: Path, gitleaks: str, identity_list: str) -> list[str]:
    summaries: list[str] = []
    env = os.environ.copy()
    if identity_list:
        env["LYNEWRIGHT_IDENTITY_SCAN_LIST"] = identity_list
    else:
        env.pop("LYNEWRIGHT_IDENTITY_SCAN_LIST", None)
    summaries.append(run_gate("identity tree + commit metadata", [sys.executable, "tools/release/identity_scan.py", "--history"], cwd=repo, env=env))
    summaries.append(run_gate("public-tree hygiene", [sys.executable, "tools/release/export_public.py", "--check-tree"], cwd=repo, env=env))
    summaries.append(run_gate("gitleaks full history", [gitleaks, "detect", "--source", str(repo), "--redact", "--exit-code", "1"], cwd=repo, timeout=600))
    word_env = os.environ.copy()
    word_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    summaries.append(run_gate("core business-word scan", [sys.executable, "-m", "pytest", "backend/app/tests/test_no_business_words.py", "-q"], cwd=repo, env=word_env, timeout=600))
    summaries.append(run_gate("starter business-word scan", [sys.executable, "tools/release/check_words.py"], cwd=repo / "templates" / "starters", timeout=300))
    summaries.append(run_gate("catalogue verification / business-word scan", [sys.executable, "tools/verify.py"], cwd=repo / "catalogue", timeout=600))
    summaries.append(run_gate("documentation snippet sync", [sys.executable, "tools/docs/sync_snippets.py", "--check"], cwd=repo, timeout=300))
    return summaries


def fresh_clone_suites(repo: Path) -> list[str]:
    summaries: list[str] = []
    clone_parent = Path(tempfile.mkdtemp(prefix="lynewright-public-clone-"))
    clone = clone_parent / "checkout"
    succeeded = False
    clean_env = os.environ.copy()
    for name in list(clean_env):
        if name.upper().startswith("LYNEWRIGHT_"):
            clean_env.pop(name, None)
    try:
        command(["git", "clone", "--no-hardlinks", str(repo), str(clone)], timeout=300)
        py = sys.executable
        summaries.append(run_gate("fresh-clone setuptools licence metadata", [py, "-m", "pip", "install", "setuptools==81.0.0"], cwd=clone, env=clean_env, timeout=900))
        summaries.append(run_gate("fresh-clone backend + testkit install", [py, "-m", "pip", "install", "./backend", "-e", "./testkit"], cwd=clone, env=clean_env, timeout=1800))
        summaries.append(run_gate("fresh-clone backend suite", [py, "-m", "pytest", "app/tests"], cwd=clone / "backend", env=clean_env, timeout=3600))
        summaries.append(run_gate("fresh-clone testkit suite", [py, "-m", "pytest", "testkit/tests"], cwd=clone, env=clean_env, timeout=1800))
        for starter in ("openapi", "mcp", "native", "cli-wrapper"):
            starter_dir = clone / "templates" / "starters" / starter
            summaries.append(run_gate(f"fresh-clone {starter} requirements", [py, "-m", "pip", "install", "-r", "requirements-test.txt"], cwd=starter_dir, env=clean_env, timeout=1200))
            if starter == "native":
                summaries.append(run_gate("fresh-clone native starter install", [py, "-m", "pip", "install", "-e", "."], cwd=starter_dir, env=clean_env, timeout=900))
            summaries.append(run_gate(f"fresh-clone {starter} starter tests (marked enabled)", [py, "-m", "pytest", "tests"], cwd=starter_dir, env=clean_env, timeout=3600))
        summaries.append(run_gate("fresh-clone catalogue dependencies", [py, "-m", "pip", "install", "-r", "requirements-test.txt"], cwd=clone / "catalogue", env=clean_env, timeout=1200))
        summaries.append(run_gate("fresh-clone catalogue verify", [py, "tools/verify.py"], cwd=clone / "catalogue", env=clean_env, timeout=900))
        summaries.append(run_gate("fresh-clone catalogue tests", [py, "-m", "pytest", "-q", "--ignore=sources"], cwd=clone / "catalogue", env=clean_env, timeout=1800))
        summaries.append(run_gate("fresh-clone docs snippet sync", [py, "tools/docs/sync_snippets.py", "--check"], cwd=clone, env=clean_env, timeout=300))
        npm = shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm")
        if not npm:
            raise RuntimeError("npm executable is required for fresh-clone frontend gates.")
        summaries.append(run_gate("fresh-clone frontend npm ci", [npm, "ci"], cwd=clone / "frontend", env=clean_env, timeout=2400))
        summaries.append(run_gate("fresh-clone frontend tests", [npm, "test"], cwd=clone / "frontend", env=clean_env, timeout=2400))
        summaries.append(run_gate("fresh-clone frontend build", [npm, "run", "build"], cwd=clone / "frontend", env=clean_env, timeout=2400))
        succeeded = True
        return summaries
    finally:
        if succeeded:
            shutil.rmtree(clone_parent, ignore_errors=True)
        else:
            print(f"Failed validation clone preserved for diagnosis: {clone}", flush=True)


def tree_summary(repo: Path) -> tuple[int, int, str]:
    tracked = git(repo, "ls-files", "-z").split("\0")
    files = [repo / Path(item) for item in tracked if item]
    total = sum(path.stat().st_size for path in files if path.is_file())
    top: dict[str, set[str]] = {}
    for path in files:
        relative = path.relative_to(repo).parts
        if relative:
            top.setdefault(relative[0], set()).add(relative[1] if len(relative) > 1 else "(root files)")
    lines = []
    for key in sorted(top):
        root_item = repo / key
        lines.append(f"{key}/" if root_item.is_dir() else key)
        if root_item.is_dir():
            for child in sorted(top[key]):
                child_item = root_item / child
                lines.append(f"  {child}{'/' if child_item.is_dir() else ''}")
    return len(files), total, "\n".join(lines)


def export(args: argparse.Namespace) -> None:
    core = Path(args.core).resolve() if args.core else CORE_ROOT
    starters = Path(args.starters).resolve()
    catalogue = Path(args.catalogue).resolve()
    output = Path(args.output).resolve() if args.output else DEFAULT_OUTPUT.resolve()
    report = Path(args.report).resolve() if args.report else output.parent / "lynewright-export-report.md"
    identity_list = args.identity_list or DEFAULT_IDENTITY_LIST
    gitleaks = args.gitleaks or os.environ.get("LYNEWRIGHT_GITLEAKS_BIN", "") or shutil.which("gitleaks") or ""
    if not gitleaks or not Path(gitleaks).is_file():
        raise RuntimeError("Provide the verified gitleaks executable with --gitleaks or LYNEWRIGHT_GITLEAKS_BIN.")
    if output.exists():
        raise RuntimeError(f"Refusing to overwrite existing export target: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="lynewright-public-build-", dir=str(output.parent)))
    repo = work / "repo"
    repo.mkdir()
    core_tar, starters_tar, catalogue_tar = work / "core.tar", work / "starters.tar", work / "catalogue.tar"
    removed: list[str] = []
    try:
        heads = {"core": archive_head(core, core_tar), "starters": archive_head(starters, starters_tar), "catalogue": archive_head(catalogue, catalogue_tar)}
        safe_extract(core_tar, repo)
        safe_extract(starters_tar, repo / "templates" / "starters")
        safe_extract(catalogue_tar, repo / "catalogue")
        original = work / "catalogue-archive-files"
        safe_extract(catalogue_tar, original)
        dist_hashes = {p.relative_to(original).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (original / "dist").rglob("*") if p.is_file()}
        for relative in EXCLUDED_PATHS:
            remove_export_path(repo, relative, removed)
        for path in list((repo / "docs").glob("extraction-*.md")) + list((repo / "docs").glob("extraction-*.txt")):
            remove_export_path(repo, path.relative_to(repo).as_posix(), removed)
        root_ignore = repo / ".gitignore"
        ignore_text = root_ignore.read_text(encoding="utf-8")
        ignore_text += "\n# Keep the signed catalogue release artifacts in the merged repository.\n!catalogue/dist/\n!catalogue/dist/**\n"
        root_ignore.write_text(ignore_text, encoding="utf-8", newline="\n")
        (repo / ".gitattributes").write_text(
            "catalogue/dist/** -text\ncatalogue/sources/** -text\n"
            "**/*.minisig -text\n"
            "backend/app/tests/fixtures/catalogue-k55/** -text\n"
            "backend/app/tests/fixtures/catalogue/minisign-interop/** -text\n"
            "backend/app/tests/fixtures/catalogue-index.json -text\n",
            encoding="utf-8", newline="\n")
        (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (repo / ".github" / "workflows" / "ci.yml").write_text(ROOT_CI, encoding="utf-8", newline="\n")
        for path in repo.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if "lynewright" in text:
                path.write_text(text.replace("lynewright", "lynewright"), encoding="utf-8", newline="\n")
        replace_if_present(repo, "README.md", [("The starter projects are at `https://github.com/lynewright/lynewright-integration-template` (replace `lynewright` when the public organisation is chosen).", "The starter projects are available in [`templates/starters/`](templates/starters/README.md). Catalogue tooling is in [`catalogue/`](catalogue/README.md).")])
        replace_if_present(repo, "backend/pytest.ini", [("addopts = --basetemp=data/.pytest-tmp", "addopts = --basetemp=.pytest-tmp")])
        replace_if_present(repo, "backend/pyproject.toml", [("\"resources/runner/Containerfile.integration\"]", "\"resources/runner/Containerfile.integration\", \"resources/catalogue-release-key.pub\"]")])
        readme = repo / "README.md"
        readme_text = readme.read_text(encoding="utf-8")
        if "## Licences by folder" not in readme_text:
            readme.write_text(readme_text.rstrip() + "\n\n## Licences by folder\n\nThe core application at the repository root is licensed under AGPL-3.0-or-later. The integration starters in `templates/starters/` and catalogue tooling in `catalogue/` are each licensed under Apache-2.0; every catalogue artifact keeps the licence stated by its own entry.\n", encoding="utf-8", newline="\n")
        update_contributing(repo)
        replace_if_present(repo, "docs/integrations/catalogue.md", [("`lynewright-catalogue/README.md` in the sibling catalogue repository", "`catalogue/README.md` in this repository")])
        replace_if_present(repo, "docs/integrations/cli-wrapper.md", [("the companion template repository's `cli-wrapper/` directory", "the `templates/starters/cli-wrapper/` directory")])
        replace_if_present(repo, "docs/integrations/openapi.md", [("In the companion template checkout's `openapi/` directory", "In `templates/starters/openapi/`"), ("from the template checkout", "from `templates/starters/`")])
        replace_if_present(repo, "docs/integrations/known-limits.md", [("../lynewright-integration-template/", "../../templates/starters/"), ("../lynewright-catalogue/", "../../catalogue/"), ("- **Template CI:** the starter workflow skips tests marked `lynewright` until Lynewright is installed from a published distribution. Source: `../../templates/starters/.github/workflows/ci.yml:11-17`.\n", "")])
        replace_if_present(repo, "templates/starters/cli-wrapper/README.md", [("for the K4.5 template repository", "in this repository's `templates/starters/` folder")])
        replace_if_present(repo, "backend/app/tests/test_docs_snippets.py", [("ROOT.parent / \"lynewright-integration-template/native/example_native/lynewright-manifest.json\"", "ROOT / \"templates/starters/native/example_native/lynewright-manifest.json\"")])
        replace_if_present(repo, "backend/app/tests/test_integration_runner.py", [("Path(os.getenv(\"LYNEWRIGHT_TEMPLATE_REPO\", ROOT.parent / \"lynewright-integration-template\")) / \"native\"", "Path(os.getenv(\"LYNEWRIGHT_TEMPLATE_REPO\", ROOT / \"templates/starters\")) / \"native\"")])
        replace_if_present(repo, "backend/app/tests/test_runner_container_build.py", [("DOCUMENTS = ROOT.parent\nTEMPLATE = Path(os.getenv(\"LYNEWRIGHT_TEMPLATE_REPO\", DOCUMENTS / \"lynewright-integration-template\"))", "TEMPLATE = Path(os.getenv(\"LYNEWRIGHT_TEMPLATE_REPO\", ROOT / \"templates/starters\"))")])
        replace_if_present(repo, "tools/docs/walkthrough_openapi.py", [("DEFAULT_TEMPLATE = ROOT.parent / \"lynewright-integration-template\"", "DEFAULT_TEMPLATE = ROOT / \"templates/starters\""), ("path to the companion lynewright-integration-template checkout", "path to the included templates/starters directory")])
        for relative in ("frontend/scripts/run-u1-screens.ps1", "frontend/scripts/run-u3-screens.ps1", "frontend/scripts/run-u4-screens.ps1"):
            private_catalogue_dir = "".join(("core", "lyx")) + "-catalogue"
            replace_if_present(repo, relative, [(f'Join-Path (Split-Path $repoRoot -Parent) "{private_catalogue_dir}"', 'Join-Path $repoRoot "catalogue"')])
        notice_result = command([sys.executable, "tools/release/notices.py"], cwd=repo, timeout=300)
        notice_text = (repo / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").casefold()
        if "unknown (" in notice_text or "unknown |" in notice_text:
            unknown_rows = [line for line in notice_text.splitlines() if "| unknown" in line]
            raise RuntimeError("Merged dependency notices contain unresolved licence labels:\n" + "\n".join(unknown_rows) + "\n" + notice_result.stdout)
        actual_dist = {p.relative_to(repo / "catalogue").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (repo / "catalogue" / "dist").rglob("*") if p.is_file()}
        expected_dist = {key.removeprefix("catalogue/"): value for key, value in dist_hashes.items()}
        if actual_dist != expected_dist:
            raise RuntimeError("One or more signed catalogue dist bytes changed during export layout.")
        hygiene = check_tree(repo)
        if hygiene:
            raise RuntimeError("Export hygiene failed before commit:\n" + "\n".join(hygiene))
        protected_binary_files: set[Path] = set()
        for relative in (
            "catalogue/dist", "catalogue/sources",
            "backend/app/tests/fixtures/catalogue-k55",
            "backend/app/tests/fixtures/catalogue/minisign-interop",
            "backend/app/tests/fixtures/catalogue-index.json",
        ):
            candidate = repo / relative
            if candidate.is_dir():
                protected_binary_files.update(path for path in candidate.rglob("*") if path.is_file())
            elif candidate.is_file():
                protected_binary_files.add(candidate)
        protected_binary_files.update(path for path in repo.rglob("*.minisig") if path.is_file())
        protected_binary_hashes = {
            path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in protected_binary_files
        }
        git(repo, "init", "-b", "main")
        git(repo, "config", "core.autocrlf", "false")
        git(repo, "add", "-A")
        commit_env = os.environ.copy()
        commit_env.update({"GIT_AUTHOR_NAME": args.author_name, "GIT_AUTHOR_EMAIL": args.author_email,
                           "GIT_COMMITTER_NAME": args.author_name, "GIT_COMMITTER_EMAIL": args.author_email})
        command(["git", "-C", str(repo), "-c", f"user.name={args.author_name}", "-c", f"user.email={args.author_email}", "commit", "--signoff", "-m", COMMIT_MESSAGE], env=commit_env, timeout=300)
        commit_hash = validate_git(repo, args.author_name, args.author_email)
        for relative, expected_hash in protected_binary_hashes.items():
            committed = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{relative}"],
                                       capture_output=True, check=True).stdout
            if hashlib.sha256(committed).hexdigest() != expected_hash:
                raise RuntimeError(f"Protected signed or binary artifact changed in the export commit: {relative}")
        gate_summaries = run_suites(repo, gitleaks, identity_list)
        clone_summaries = fresh_clone_suites(repo)
        file_count, total_bytes, tree = tree_summary(repo)
        shutil.move(str(repo), str(output))
        report.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Local public export report", "", f"- Export: `{output}`", f"- Commit: `{commit_hash}`", "- Branch: `main`; commit count: `1`; remotes: none", f"- Author / committer / sign-off: `{args.author_name} <{args.author_email}>`", f"- Source HEADs: core `{heads['core']}`, starters `{heads['starters']}`, catalogue `{heads['catalogue']}`", f"- Tracked files: {file_count}", f"- Tracked bytes: {total_bytes} ({total_bytes / (1024 * 1024):.2f} MiB)", "", "## Tree (top two levels)", "", "```text", tree, "```", "", "## Exclusions and reasons", ""]
        lines.extend(f"- `{item}` — {exclusion_reason(item)}" for item in sorted(set(removed)))
        lines += ["", "## Export gates", ""] + [f"- {item}" for item in gate_summaries]
        lines += ["", "## Fresh-clone gates", ""] + [f"- {item}" for item in clone_summaries]
        lines += ["", "## Notes", "", "Signed catalogue `dist/` files were SHA-256 compared before and after layout assembly. Root `.gitattributes` prevents text conversion for `catalogue/dist/**` and `catalogue/sources/**`. This export was built from Git archives of the listed `HEAD`s only; no source working-tree files or private-history objects were copied.", ""]
        report.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        print(f"EXPORT={output}")
        print(f"COMMIT={commit_hash}")
        print(f"REPORT={report}")
        print(f"FILES={file_count} BYTES={total_bytes}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", help="core repository root; defaults to the repository containing this script")
    parser.add_argument("--starters", help="starter source repository root (Git archive HEAD only)")
    parser.add_argument("--catalogue", help="catalogue source repository root (Git archive HEAD only)")
    parser.add_argument("--output", help="new export path; defaults to ../public/lynewright")
    parser.add_argument("--report", help="report path outside the export")
    parser.add_argument("--identity-list", default=DEFAULT_IDENTITY_LIST, help="external private identity list (never copied)")
    parser.add_argument("--gitleaks", help="verified gitleaks executable")
    parser.add_argument("--author-name", default=AUTHOR_NAME)
    parser.add_argument("--author-email", default=AUTHOR_EMAIL)
    parser.add_argument("--check-tree", action="store_true", help="check public tree naming, file size, and secret hygiene")
    args = parser.parse_args()
    try:
        if args.check_tree:
            errors = check_tree(CORE_ROOT)
            if errors:
                print("Export hygiene failed:\n" + "\n".join(errors), file=sys.stderr)
                return 1
            print("Export hygiene: clean")
            return 0
        if not args.starters or not args.catalogue:
            parser.error("--starters and --catalogue are required when building an export")
        export(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
