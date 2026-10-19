"""Documentation examples stay synchronized, parse, and satisfy their schemas."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import jsonschema
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
DOCS = [ROOT / "docs/integrations-howto.md", *sorted((ROOT / "docs/integrations").glob("*.md"))]
SNIPPET = re.compile(
    r"^<!-- snippet: (?P<path>[^#\r\n]+) -->\r?\n"
    r"(?:<!-- schema: (?P<schema>[a-z0-9-]+) -->\r?\n)?"
    r"```(?P<language>[A-Za-z0-9_-]+)\r?\n"
    r"(?P<body>.*?)\r?\n```",
    re.MULTILINE | re.DOTALL,
)
FENCE = re.compile(r"(?ms)^```(?P<language>[A-Za-z0-9_-]+)\r?\n(?P<body>.*?)\r?\n^```$")


def test_marked_snippets_match_their_source_files() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/docs/sync_snippets.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_yaml_and_json_snippets_validate_against_the_declared_schema() -> None:
    schemas = {
        "integration-file": ROOT / "backend/app/schemas/integration-file.schema.json",
        "cli-command": ROOT / "backend/app/schemas/cli-command.schema.json",
        "native-manifest": ROOT / "backend/app/schemas/native-manifest.schema.json",
    }
    checked = 0
    for document in DOCS:
        text = document.read_text(encoding="utf-8")
        matches = list(SNIPPET.finditer(text))
        fences = list(FENCE.finditer(text))
        for match in matches:
            assert any(
                match.group("body") == fence.group("body")
                and match.group("language").lower() == fence.group("language").lower()
                for fence in fences
            ), f"Snippet marker has no matching fence in {document}"
        for match in matches:
            language = match.group("language").lower()
            schema_name = match.group("schema")
            if language not in {"yaml", "yml", "json"}:
                continue
            assert schema_name in schemas, f"Missing supported schema annotation in {document}"
            if language == "json":
                value = json.loads(match.group("body"))
            else:
                value = yaml.safe_load(match.group("body"))
            schema = json.loads(schemas[schema_name].read_text(encoding="utf-8"))
            jsonschema.validate(value, schema, format_checker=jsonschema.FormatChecker())
            checked += 1
        marked_config = {
            (match.group("language").lower(), match.group("body"))
            for match in matches
            if match.group("language").lower() in {"yaml", "yml", "json"}
        }
        for fence in fences:
            language = fence.group("language").lower()
            body = fence.group("body")
            if language in {"yaml", "yml", "json"}:
                assert (language, body) in marked_config, f"Config block needs a source marker in {document}"
    assert checked >= 1


def test_python_examples_compile_and_shell_examples_are_listed_not_run() -> None:
    shell_hashes: set[str] = set()
    observed_shell_hashes: set[str] = set()
    inventory = ROOT / "docs/integrations/shell-snippets.txt"
    for line in inventory.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line.strip())
        if match:
            shell_hashes.add(match.group(1))
    for document in DOCS:
        for block in FENCE.finditer(document.read_text(encoding="utf-8")):
            language, body = block.group("language").lower(), block.group("body")
            if language in {"python", "py"}:
                ast.parse(body, filename=str(document))
            elif language in {"bash", "sh", "shell"}:
                observed_shell_hashes.add(hashlib.sha256(body.encode("utf-8")).hexdigest())
    assert observed_shell_hashes == shell_hashes, "Shell blocks must be hash-listed for review and are never executed by this test."


def test_native_manifest_fixture_matches_template_checkout_when_available() -> None:
    template_manifest = ROOT / "templates/starters/native/example_native/lynewright-manifest.json"
    fixture_manifest = ROOT / "backend/app/tests/fixtures/native/lynewright-manifest.json"
    if template_manifest.is_file():
        assert fixture_manifest.read_bytes() == template_manifest.read_bytes()


def test_runner_walkthrough_routes_are_mounted() -> None:
    from app.main import CORE_ROUTERS

    method_path = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|", re.MULTILINE)
    document = (ROOT / "docs/integrations/runners.md").read_text(encoding="utf-8")
    documented = set(method_path.findall(document))
    mounted = {
        (method, route.path)
        for module in CORE_ROUTERS
        for route in module.router.routes
        for method in (getattr(route, "methods", None) or set())
    }
    index_prefix = "/api/" + "cata" + "logue"
    expected = {
        ("POST", f"{index_prefix}/entries/{{integration_id}}/{{version}}/install"),
        ("POST", "/api/integration-management/{integration_id}/authorization"),
        ("POST", "/api/integration-management/{integration_id}/enable"),
        ("POST", "/api/integration-management/{integration_id}/runner-token"),
        ("GET", "/api/integration-management/{integration_id}/runtime-config"),
        ("GET", "/api/production-lines/operations"),
        ("POST", "/api/production-lines"),
        ("POST", "/api/production-lines/{line_id}/runs"),
    }
    assert expected <= documented, f"Runner walkthrough is missing routes: {sorted(expected - documented)}"
    assert documented <= mounted, f"Runner walkthrough documents unmounted routes: {sorted(documented - mounted)}"
