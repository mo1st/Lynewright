from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]


def _module_exists(module: str) -> bool:
    if not module.startswith("app"):
        return True
    if module == "app":
        return (APP_ROOT / "__init__.py").is_file()
    parts = module.split(".")[1:]
    candidate = APP_ROOT.joinpath(*parts)
    return (
        candidate.with_suffix(".py").is_file()
        or (candidate / "__init__.py").is_file()
        or candidate.is_dir()
    )


def test_every_absolute_app_import_resolves_inside_lynewright() -> None:
    unresolved: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            unresolved.append(f"{path.relative_to(APP_ROOT.parent.parent).as_posix()}: parse error: {exc}")
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names if alias.name.startswith("app."))
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                modules.append(node.module)
                base = APP_ROOT.joinpath(*node.module.split(".")[1:])
                if base.is_dir() and not (base / "__init__.py").is_file():
                    modules.extend(
                        f"{node.module}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                    )
            for module in modules:
                if not _module_exists(module):
                    relative = path.relative_to(APP_ROOT.parent.parent).as_posix()
                    unresolved.append(f"{relative}: {module}")

    assert not unresolved, "Unresolved Lynewright imports:\n" + "\n".join(unresolved)
