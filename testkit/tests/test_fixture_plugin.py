from __future__ import annotations

import importlib
import json
from pathlib import Path


def _write_native_fixture(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    distribution = "lynewright-testkit-fixture"
    package_name = "lynewright_testkit_fixture"
    package = root / package_name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    manifest = {
            "schema_version": "1.2",
            "id": "lynewright.integration.testkit-fixture",
            "name": "Testkit Fixture",
            "version": "1.0.0",
            "type": "native",
            "lynewright_version": "^0.1.0",
            "sdk_version": "1.0.0",
            "requested_capabilities": [],
            "contributions": ["services"],
            "provides": [{
                "contract": "lynewright.testkit.fixture",
                "implementation": f"{package_name}.provider:FixtureProvider",
                "version": "1.0.0",
                "capabilities": ["echo"],
            }],
        }
    (package / "lynewright-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "manifest.py").write_text(
        "raise RuntimeError('the API must not import package manifest code')\n",
        encoding="utf-8",
    )
    (package / "provider.py").write_text(
        """class FixtureProvider:
    operation_schemas = {
        'echo': {
            'type': 'object',
            'properties': {'value': {'type': 'string'}},
            'required': ['value'],
            'additionalProperties': False,
            'caller_capability': 'system:read',
            'read_only': True,
            'risk_class': 'READ_ONLY',
        },
    }

    def invoke(self, operation, payload, *, invocation_context=None):
        if operation != 'echo':
            raise ValueError('Unsupported operation.')
        return {'value': payload['value']}
""",
        encoding="utf-8",
    )
    info = root / "lynewright_testkit_fixture-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: lynewright-testkit-fixture\nVersion: 1.0.0\n\n",
        encoding="utf-8",
    )
    (info / "entry_points.txt").write_text(
        "[lynewright.integrations]\nfixture = lynewright_testkit_fixture.manifest\n",
        encoding="utf-8",
    )
    package_files = [f"{package_name}/{path.name}" for path in package.iterdir() if path.is_file()]
    (info / "RECORD").write_text(
        "".join(f"{name},,\n" for name in package_files)
        + f"{info.name}/METADATA,,\n{info.name}/entry_points.txt,,\n{info.name}/RECORD,,\n",
        encoding="utf-8",
    )
    importlib.invalidate_caches()
    return distribution


def test_fixture_runs_allowlisted_native_operation_in_a_line(lynewright, tmp_path: Path) -> None:
    distribution = _write_native_fixture(tmp_path / "package")
    package_root = tmp_path / "package"
    lynewright.monkeypatch.syspath_prepend(str(package_root))
    lynewright.monkeypatch.setenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "1")
    integration_id = lynewright.install_native_package(distribution)

    available = lynewright.operations(integration_id=integration_id)
    assert available, {
        "integration": lynewright.management.detail(integration_id),
        "definitions": lynewright.lifecycle.definitions_for(integration_id),
        "contributions": lynewright.contributions.list(kind="service"),
        "diagnostics": lynewright.management.diagnostics(integration_id),
    }
    matching = [item for item in available if item["operation"] == "echo"]
    assert matching, available
    operation = matching[0]
    run = lynewright.run_line(operation, arguments={"value": "fixture value"})

    assert run["status"] == "done", run
    assert run["stages"][0]["output"] == {"value": "fixture value"}


def test_fixture_uses_a_private_database_and_project_scope(lynewright, tmp_path: Path) -> None:
    assert lynewright.root == tmp_path
    assert lynewright.project_id.startswith("testkit-")
    assert lynewright.operations() == []
