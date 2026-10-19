from __future__ import annotations

from pathlib import Path


def _word(*parts: str) -> str:
    return "".join(parts)


BANNED_WORDS = (
    _word("bel", "vio"), _word("k", "dp"), _word("epu", "bli"), _word("bo", "ok"),
    _word("skin", "care"), _word("you", "cam"), _word("what", "sapp"), _word("wid", "get"),
    _word("landing", "_page"), _word("lp", "_"), _word("cata", "logue"), _word("cata", "log"),
    _word("q", "uiz"), _word("cam", "paign"), _word("woo", "commerce"), _word("shop", "ify"),
    _word("money", "printer"), _word("ve", "lith"), _word("emo", " atlas"),
    _word("product", "_", "cata", "log"), _word("cata", "log", "_sync"),
    _word("cata", "logue", "_classification"), _word("cata", "logue", "_snapshot"),
    _word("quor", "lyx"), _word("core", "lyx"), _word("grapes", "js"),
)

APP_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = APP_ROOT.parents[1] / "frontend" / "src"
APP_RELATIVE = "backend/app/"
FRONTEND_RELATIVE = "frontend/src/"

# Each tuple is one narrow carry-forward exception with its reason.
ALLOWLIST = (
    (
        {
            "backend/app/services/integration_operations.py",
            "backend/app/services/operator_integration_tools.py",
            "backend/app/services/operator_runtime/loop.py",
            "backend/app/services/integration_conformance.py",
            "backend/app/routers/integration_management.py",
            "backend/app/services/operator_mapped_tools.py",
            "backend/app/services/integration_file.py",
            "backend/app/services/integration_install_policy.py",
            "backend/app/services/integration_management.py",
            "backend/app/services/production_integrations.py",
            "backend/app/tests/test_integration_install_policy.py",
        },
        {_word("cata", "logue"), _word("cata", "log")},
        "These files describe declared integration operations, discovery, and exact integration artifact matching.",
    ),
    (
        {
            "backend/app/routers/production_lines.py",
            "backend/app/services/line_engine.py",
            "backend/app/services/line_runner.py",
            "backend/app/tests/test_line_integration_" + _word("cata", "logue") + ".py",
        },
        {_word("cata", "logue"), _word("cata", "log")},
        "Production lines resolve declared integration operation " + _word("cata", "logue") + " entries at save and run time.",
    ),
)

# Signed integration index validation and its operator-facing status are core security metadata.
ALLOWLIST = ALLOWLIST + (
    (
        {
            "backend/app/config.py",
            "backend/app/database.py",
            "backend/app/main.py",
            "backend/app/routers/" + _word("cata", "logue") + ".py",
            "backend/app/services/" + _word("cata", "logue") + ".py",
            "backend/app/schemas/" + _word("cata", "logue") + ".schema.json",
            "backend/app/tests/test_" + _word("cata", "logue") + ".py",
            "backend/app/tests/fixtures/" + _word("cata", "logue") + "-index.json",
            "backend/app/services/integration_provenance.py",
            "backend/app/tests/test_integration_provenance.py",
            "backend/app/tests/test_minisign_interop.py",
            "backend/app/services/runner_runtime_config.py",
            "backend/app/tests/test_integration_runner.py",
            "backend/app/services/integration_file.py",
            "backend/app/services/integration_install_policy.py",
            "backend/app/services/integration_management.py",
            "backend/app/services/production_integrations.py",
            "backend/app/tests/test_integration_install_policy.py",
            "frontend/src/types.ts",
            "frontend/src/components/integrations/IntegrationProvenanceDetails.tsx",
            "frontend/src/components/integrations/IntegrationProvenanceDetails.test.tsx",
        },
        {_word("cata", "logue"), _word("cata", "log")},
        "These files verify signed integration " + _word("cata", "logue") + " metadata, exact artifact matches, or generate pinned image settings.",
    ),
    (
        {
            "backend/app/services/" + _word("cata", "logue") + "_installation.py",
            "backend/app/tests/test_" + _word("cata", "logue") + "_installation.py",
            "backend/app/tests/fixtures/" + _word("cata", "logue") + "-k55/" + _word("cata", "logue") + ".json",
            "backend/app/tests/fixtures/" + _word("cata", "logue") + "-k55/" + _word("cata", "logue") + ".json.minisig",
        },
        {_word("cata", "logue"), _word("cata", "log")},
        "These files install exact signed integration " + _word("cata", "logue") + " artifacts or provide the signed test index.",
    ),
    (
        {
            "backend/app/services/integration_removal.py",
            "backend/app/tests/test_integration_removal.py",
        },
        {_word("cata", "logue"), _word("cata", "log")},
        "Removal previews and tests inspect exact signed integration installation metadata.",
    ),
)

# Only signed-index trust status surfaces use this term in the frontend.
FRONTEND_ALLOWLIST: tuple[tuple[set[str], set[str], str], ...] = (
    (
        {
            "frontend/src/pages/Integrations.tsx",
            "frontend/src/api/" + _word("cata", "logue") + "Client.ts",
            "frontend/src/components/integrations/" + _word("Cata", "logue") + "Panel.tsx",
            "frontend/src/components/integrations/" + _word("Cata", "logue") + "Panel.test.tsx",
            "frontend/src/types.ts",
            "frontend/src/components/integrations/IntegrationProvenanceDetails.tsx",
            "frontend/src/components/integrations/IntegrationProvenanceDetails.test.tsx",
            "frontend/src/api/integrationManagementClient.ts",
            "frontend/src/components/integrations/InstallConfirmationDialog.tsx",
            "frontend/src/components/integrations/InstallConfirmationDialog.test.tsx",
            "frontend/src/components/integrations/InstallPolicyBanner.tsx",
            "frontend/src/App.tsx",
            "frontend/src/components/integrations/DevCata" + "logueKeyBanner.tsx",
            "frontend/src/components/integrations/DevCata" + "logueKeyBanner.test.tsx",
        },
        {_word("cata", "logue"), _word("cata", "log")},
        "These files show signed integration " + _word("cata", "logue") + " trust, exact artifact status, and install confirmation details.",
    ),
    (
        {
            "frontend/src/components/ui/SystemWarnings.tsx",
            "frontend/src/components/ui/SystemWarnings.test.tsx",
            "frontend/src/navigation.ts",
            "frontend/src/navigation.test.ts",
            "frontend/src/router.ts",
            "frontend/src/routes.test.ts",
            "frontend/src/pages/Overview.tsx",
            "frontend/src/pages/" + _word("Cata", "logue") + ".tsx",
        },
        {_word("cata", "logue"), _word("cata", "log")},
        "These files add the signed integration " + _word("cata", "logue") + " dashboard area, its routes, and its trust warnings.",
    ),
)


def _allowlist_reason(
    relative_path: str,
    word: str,
    entries: tuple[tuple[set[str], set[str], str], ...],
) -> str | None:
    for paths, words, reason in entries:
        if word in words and ("*" in paths or relative_path in paths):
            return reason
    return None


def _violations(root: Path, prefix: str, allowlist: tuple[tuple[set[str], set[str], str], ...]) -> list[str]:
    violations: list[str] = []
    if not root.exists():
        return violations
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError):
            continue
        relative = prefix + path.relative_to(root).as_posix()
        for word in BANNED_WORDS:
            if word.casefold() in source and _allowlist_reason(relative, word, allowlist) is None:
                violations.append(f"{relative}: {word}")
    return violations


def test_core_and_frontend_contain_no_business_words() -> None:
    violations = _violations(APP_ROOT, APP_RELATIVE, ALLOWLIST)
    violations.extend(_violations(FRONTEND_ROOT, FRONTEND_RELATIVE, FRONTEND_ALLOWLIST))
    assert not violations, "Unallowlisted business terms:\n" + "\n".join(violations)
