from __future__ import annotations

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"
SERVICES = APP_ROOT / "services"

EXPECTED_BACKGROUND_TASKS = {
    "run_job_worker",
    "run_operator_automation_scheduler",
}
GENERIC_CORE_MODULES = (
    "platform_services.py",
    "integration_registry.py",
    "integration_jobs.py",
    "integration_capability_evidence.py",
    "integration_media_transfer.py",
    "integration_errors.py",
    "integration_sdk.py",
    "integration_connections.py",
    "integration_lifecycle.py",
    "integration_conformance.py",
    "integration_contributions.py",
    "integration_execution.py",
    "job_queue.py",
)
PROVIDER_NAMES = (
    "postiz", "mastodon", "brightbean", "umami",
    "meta_ads", "google_ads", "tiktok_ads", "taboola_native",
)


def test_requirements_contain_no_postgresql_driver() -> None:
    manifests = [
        *BACKEND_ROOT.glob("requirements*.txt"),
        BACKEND_ROOT / "pyproject.toml",
        BACKEND_ROOT / "uv.lock",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in manifests if path.is_file()).lower()
    for driver in ("psycopg", "asyncpg", "pg8000", "postgres", "supabase"):
        assert driver not in text, f"requirements.txt must not gain a PostgreSQL driver: {driver}"


def test_lynewright_persistence_remains_sqlite() -> None:
    database_src = (APP_ROOT / "database.py").read_text(encoding="utf-8")
    assert "import sqlite3" in database_src
    lowered = database_src.lower()
    for driver in ("psycopg", "asyncpg", "pg8000", "sqlalchemy", "create_engine"):
        assert driver not in lowered, f"database.py must not adopt a non-SQLite stack: {driver}"
    config_src = (APP_ROOT / "config.py").read_text(encoding="utf-8")
    assert 'LYNEWRIGHT_DB_PATH' in config_src and ".sqlite3" in config_src


def test_no_second_scheduler_loop_is_registered_at_startup() -> None:
    main_src = (APP_ROOT / "main.py").read_text(encoding="utf-8")
    registered = set(re.findall(
        r"asyncio\.create_task\((?:asyncio\.to_thread\()?\s*(run_[a-z_]+)[,(]", main_src,
    ))
    assert registered == EXPECTED_BACKGROUND_TASKS, (
        "startup must register only the core job worker and operator automation tasks"
    )


def test_no_provider_name_appears_in_generic_core_modules() -> None:
    for name in GENERIC_CORE_MODULES:
        text = (SERVICES / name).read_text(encoding="utf-8").lower()
        for provider in PROVIDER_NAMES:
            assert provider not in text, (
                f"generic core module {name} must not reference provider '{provider}'"
            )


def test_every_single_provider_contract_is_marked_provisional() -> None:
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*_CONTRACT)\s*=\s*[\"']lynewright\.[a-z0-9_.-]+[\"']", re.MULTILINE)
    module_texts = {path: path.read_text(encoding="utf-8") for path in sorted(SERVICES.glob("*.py"))}
    contract_constants: dict[Path, dict[str, str]] = {}
    for path, text in module_texts.items():
        found = pattern.findall(text)
        if found:
            contract_constants[path] = {constant: constant[: -len("_CONTRACT")] for constant in found}
    assert contract_constants, "expected at least one lynewright.* service contract"

    for path, constants in contract_constants.items():
        text = module_texts[path]
        for constant, prefix in constants.items():
            external_references = sum(
                other_text.count(constant)
                for other_path, other_text in module_texts.items()
                if other_path != path
            )
            status_pattern = re.compile(rf"{prefix}_CONTRACT_STATUS\s*=\s*[\"']([A-Z]+)[\"']")
            declared_status = status_pattern.search(text)
            if external_references >= 2:
                continue
            assert declared_status is not None, (
                f"{path.name} defines single-provider contract {constant} without "
                f"{prefix}_CONTRACT_STATUS = 'PROVISIONAL'"
            )
            assert declared_status.group(1) == "PROVISIONAL", (
                f"{path.name}: {prefix}_CONTRACT_STATUS must be PROVISIONAL until a second real provider exists"
            )


def test_bounded_memory_ceiling_was_not_raised_in_place_of_streaming() -> None:
    from app.services.integration_media_transfer import IntegrationMediaTransferService
    assert IntegrationMediaTransferService.BOUNDED_MEMORY_MAX_BYTES == 10_000_000
