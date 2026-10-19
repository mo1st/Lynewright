#!/usr/bin/env python3
"""Guard the installer smoke so its host interpreter only needs the stdlib."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest


SMOKE_PATH = Path(__file__).with_name("ci_smoke.py")
SMOKE_SOURCE = SMOKE_PATH.read_text(encoding="utf-8")
SMOKE_TREE = ast.parse(SMOKE_SOURCE, filename=str(SMOKE_PATH))


class InstallerSmokeHostGuardTests(unittest.TestCase):
    def test_host_imports_are_standard_library_only(self) -> None:
        imported_roots: set[str] = set()
        for node in ast.walk(SMOKE_TREE):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

        non_stdlib = sorted(imported_roots - sys.stdlib_module_names)
        self.assertEqual(non_stdlib, [], f"host-side imports require non-stdlib modules: {non_stdlib}")

    def test_mcp_sdk_is_invoked_by_python_in_the_mcp_container(self) -> None:
        calls = [
            node for node in ast.walk(SMOKE_TREE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
        ]
        command_lists = [call.args[0] for call in calls if call.args and isinstance(call.args[0], ast.List)]
        command_parts = [
            [item.value for item in command.elts if isinstance(item, ast.Constant)]
            for command in command_lists
        ]
        self.assertIn(
            ["docker", "compose", "exec", "-T", "mcp", "python", "-"],
            command_parts,
            "the smoke must invoke the MCP SDK inside the mcp container",
        )

        self.assertIn('from mcp import ClientSession', SMOKE_SOURCE)
        self.assertIn('"http://127.0.0.1:8001/mcp"', SMOKE_SOURCE)

    def test_compose_commands_run_in_the_installed_stack_directory(self) -> None:
        # Compose names the project after its directory; running from the
        # checkout instead of LYNEWRIGHT_DIR finds no services.
        import importlib.util
        import os
        from unittest import mock

        spec = importlib.util.spec_from_file_location("ci_smoke_under_test", SMOKE_PATH)
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        with mock.patch.dict(os.environ, {"LYNEWRIGHT_DIR": "/srv/lynewright-install"}):
            self.assertEqual(smoke.compose_dir(), "/srv/lynewright-install")

    def test_failure_diagnostics_include_compose_state_and_both_service_logs(self) -> None:
        self.assertIn('["ps", "-a"]', SMOKE_SOURCE)
        self.assertIn('["logs", "--no-color", "mcp", "api"]', SMOKE_SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
