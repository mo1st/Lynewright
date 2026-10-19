"""Environment-backed Lynewright configuration."""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = BACKEND_DIR.parent
_OPEN_POLICY_WARNED = False


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _integer(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)).strip()))
    except ValueError:
        return default


def _number(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default)).strip()))
    except ValueError:
        return default


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str
    database_path: Path
    packages_dir: Path
    packages_host_dir: str
    cors_origins: list[str]
    admin_token: str
    bootstrap_admin_email: str
    bootstrap_admin_password: str
    session_ttl_hours: int
    login_rate_limit_per_ip: int
    login_rate_limit_per_account: int
    public_url: str
    redis_url: str
    worker_count: int
    rate_limit_max_keys: int
    ai_allow_private_base_url: bool
    disable_background_schedulers: bool
    runtime_worker_enabled: bool
    runtime_worker_mode: str
    runtime_worker_poll_seconds: float
    runtime_job_lease_seconds: int
    runtime_worker_heartbeat_seconds: int
    runtime_completed_job_retention_days: int
    runtime_cancelled_job_retention_days: int
    runtime_failed_job_retention_days: int
    runtime_worker_retention_days: int
    runtime_recovery_interval_seconds: int
    runtime_worker_error_backoff_initial_seconds: float
    runtime_worker_error_backoff_max_seconds: float
    runtime_worker_restart_backoff_initial_seconds: float
    runtime_worker_restart_backoff_max_seconds: float
    runtime_worker_stable_period_seconds: int
    runtime_worker_max_restarts_per_hour: int
    runtime_supervisor_max_restarts_per_hour: int
    runtime_supervisor_generation_health_enabled: bool
    runtime_supervisor_task_watcher_enabled: bool
    runtime_instance_health_enabled: bool
    runtime_worker_health_coalescing_enabled: bool
    runtime_worker_shutdown_timeout_seconds: int
    operator_automation_enabled: bool
    operator_automation_tick_seconds: float
    operator_dispatch_enabled: bool
    operator_objectives_per_tick: int
    operator_interactions_per_tick: int
    operator_max_tenants_per_tick: int
    interaction_link_token_ttl_seconds: int
    interaction_action_ttl_seconds: int
    interaction_webhook_max_body_bytes: int
    telegram_enabled: bool
    telegram_api_base_url: str
    application_version: str
    feature_flags: dict[str, bool]
    catalogue_url: str = ""
    catalogue_keys: list[str] = field(default_factory=list)
    catalogue_internal_endpoint_ref: str = ""
    catalogue_allow_dev_key: bool = False
    catalogue_file_max_bytes: int = 1_048_576
    catalogue_wheel_max_bytes: int = 100 * 1024 * 1024
    install_policy: str = "confirm"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


def get_settings() -> Settings:
    global _OPEN_POLICY_WARNED
    environment = os.getenv("LYNEWRIGHT_ENV", "development").strip() or "development"
    install_policy = os.getenv("LYNEWRIGHT_INSTALL_POLICY", "confirm").strip().lower() or "confirm"
    if install_policy not in {"catalogue-only", "confirm", "open"}:
        raise RuntimeError(
            "LYNEWRIGHT_INSTALL_POLICY must be one of: catalogue-only, confirm, open."
        )
    if install_policy == "open" and not _OPEN_POLICY_WARNED:
        logging.getLogger("lynewright.install_policy").warning(
            "LYNEWRIGHT_INSTALL_POLICY=open disables outside-install confirmation; this is for development only."
        )
        _OPEN_POLICY_WARNED = True
    database_path = Path(os.getenv(
        "LYNEWRIGHT_DB_PATH", str(BACKEND_DIR / "data" / "lynewright.sqlite3"),
    ))
    packages_dir = Path(os.getenv("LYNEWRIGHT_PACKAGES_DIR", str(database_path.parent / "packages"))).expanduser()
    if not packages_dir.is_absolute():
        raise RuntimeError("LYNEWRIGHT_PACKAGES_DIR must be an absolute path.")
    origins = _split_csv(os.getenv("LYNEWRIGHT_CORS_ORIGINS", "*")) or ["*"]
    if environment.lower() == "production" and "*" in origins:
        raise RuntimeError("LYNEWRIGHT_CORS_ORIGINS='*' is not allowed in production.")
    return Settings(
        environment=environment,
        database_path=database_path,
        packages_dir=packages_dir,
        packages_host_dir=os.getenv("LYNEWRIGHT_PACKAGES_HOST_DIR", "").strip(),
        cors_origins=origins,
        admin_token=os.getenv("LYNEWRIGHT_ADMIN_TOKEN", "").strip(),
        bootstrap_admin_email=os.getenv("LYNEWRIGHT_BOOTSTRAP_EMAIL", "").strip(),
        bootstrap_admin_password=os.getenv("LYNEWRIGHT_BOOTSTRAP_PASSWORD", ""),
        session_ttl_hours=_integer("LYNEWRIGHT_SESSION_TTL_HOURS", 720, minimum=1),
        login_rate_limit_per_ip=_integer("LYNEWRIGHT_LOGIN_RATE_LIMIT_PER_IP", 30, minimum=1),
        login_rate_limit_per_account=_integer("LYNEWRIGHT_LOGIN_RATE_LIMIT_PER_ACCOUNT", 10, minimum=1),
        public_url=os.getenv("LYNEWRIGHT_PUBLIC_URL", "http://127.0.0.1:8000").strip(),
        redis_url=os.getenv("LYNEWRIGHT_REDIS_URL", "").strip(),
        worker_count=_integer("LYNEWRIGHT_WORKER_COUNT", 1, minimum=1),
        rate_limit_max_keys=_integer("LYNEWRIGHT_RATE_LIMIT_MAX_KEYS", 10000, minimum=100),
        ai_allow_private_base_url=_boolean("LYNEWRIGHT_AI_ALLOW_PRIVATE_BASE_URL", False),
        disable_background_schedulers=_boolean("LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS", False),
        runtime_worker_enabled=_boolean("LYNEWRIGHT_RUNTIME_WORKER_ENABLED", True),
        runtime_worker_mode=os.getenv("LYNEWRIGHT_RUNTIME_WORKER_MODE", "embedded").strip() or "embedded",
        runtime_worker_poll_seconds=_number("LYNEWRIGHT_RUNTIME_WORKER_POLL_SECONDS", 1.0, minimum=0.1),
        runtime_job_lease_seconds=_integer("LYNEWRIGHT_RUNTIME_JOB_LEASE_SECONDS", 60, minimum=1),
        runtime_worker_heartbeat_seconds=_integer("LYNEWRIGHT_RUNTIME_WORKER_HEARTBEAT_SECONDS", 10, minimum=1),
        runtime_completed_job_retention_days=_integer("LYNEWRIGHT_RUNTIME_COMPLETED_JOB_RETENTION_DAYS", 14, minimum=1),
        runtime_cancelled_job_retention_days=_integer("LYNEWRIGHT_RUNTIME_CANCELLED_JOB_RETENTION_DAYS", 7, minimum=1),
        runtime_failed_job_retention_days=_integer("LYNEWRIGHT_RUNTIME_FAILED_JOB_RETENTION_DAYS", 30, minimum=1),
        runtime_worker_retention_days=_integer("LYNEWRIGHT_RUNTIME_WORKER_RETENTION_DAYS", 14, minimum=1),
        runtime_recovery_interval_seconds=_integer("LYNEWRIGHT_RUNTIME_RECOVERY_INTERVAL_SECONDS", 30, minimum=1),
        runtime_worker_error_backoff_initial_seconds=_number("LYNEWRIGHT_RUNTIME_WORKER_ERROR_BACKOFF_INITIAL_SECONDS", 1.0, minimum=0.1),
        runtime_worker_error_backoff_max_seconds=_number("LYNEWRIGHT_RUNTIME_WORKER_ERROR_BACKOFF_MAX_SECONDS", 30.0, minimum=1.0),
        runtime_worker_restart_backoff_initial_seconds=_number("LYNEWRIGHT_RUNTIME_WORKER_RESTART_BACKOFF_INITIAL_SECONDS", 1.0, minimum=0.1),
        runtime_worker_restart_backoff_max_seconds=_number("LYNEWRIGHT_RUNTIME_WORKER_RESTART_BACKOFF_MAX_SECONDS", 30.0, minimum=1.0),
        runtime_worker_stable_period_seconds=_integer("LYNEWRIGHT_RUNTIME_WORKER_STABLE_PERIOD_SECONDS", 60, minimum=1),
        runtime_worker_max_restarts_per_hour=_integer("LYNEWRIGHT_RUNTIME_WORKER_MAX_RESTARTS_PER_HOUR", 10, minimum=1),
        runtime_supervisor_max_restarts_per_hour=_integer("LYNEWRIGHT_RUNTIME_SUPERVISOR_MAX_RESTARTS_PER_HOUR", 10, minimum=1),
        runtime_supervisor_generation_health_enabled=_boolean("LYNEWRIGHT_RUNTIME_SUPERVISOR_GENERATION_HEALTH_ENABLED", True),
        runtime_supervisor_task_watcher_enabled=_boolean("LYNEWRIGHT_RUNTIME_SUPERVISOR_TASK_WATCHER_ENABLED", True),
        runtime_instance_health_enabled=_boolean("LYNEWRIGHT_RUNTIME_INSTANCE_HEALTH_ENABLED", True),
        runtime_worker_health_coalescing_enabled=_boolean("LYNEWRIGHT_RUNTIME_WORKER_HEALTH_COALESCING_ENABLED", True),
        runtime_worker_shutdown_timeout_seconds=_integer("LYNEWRIGHT_RUNTIME_WORKER_SHUTDOWN_TIMEOUT_SECONDS", 10, minimum=1),
        operator_automation_enabled=_boolean("LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED", True),
        operator_automation_tick_seconds=_number("LYNEWRIGHT_OPERATOR_AUTOMATION_TICK_SECONDS", 15.0, minimum=5.0),
        operator_dispatch_enabled=_boolean("LYNEWRIGHT_OPERATOR_DISPATCH_ENABLED", True),
        operator_objectives_per_tick=_integer("LYNEWRIGHT_OPERATOR_OBJECTIVES_PER_TICK", 4, minimum=1),
        operator_interactions_per_tick=_integer("LYNEWRIGHT_OPERATOR_INTERACTIONS_PER_TICK", 4, minimum=1),
        operator_max_tenants_per_tick=_integer("LYNEWRIGHT_OPERATOR_MAX_TENANTS_PER_TICK", 100, minimum=1),
        interaction_link_token_ttl_seconds=_integer("LYNEWRIGHT_INTERACTION_LINK_TOKEN_TTL_SECONDS", 3600, minimum=60),
        interaction_action_ttl_seconds=_integer("LYNEWRIGHT_INTERACTION_ACTION_TTL_SECONDS", 900, minimum=60),
        interaction_webhook_max_body_bytes=_integer("LYNEWRIGHT_INTERACTION_WEBHOOK_MAX_BODY_BYTES", 262144, minimum=1024),
        telegram_enabled=_boolean("LYNEWRIGHT_TELEGRAM_ENABLED", True),
        telegram_api_base_url=os.getenv("LYNEWRIGHT_TELEGRAM_API_BASE_URL", "https://api.telegram.org").strip(),
        application_version=os.getenv("LYNEWRIGHT_APPLICATION_VERSION", "development").strip() or "development",
        feature_flags={},
        catalogue_url=os.getenv("LYNEWRIGHT_CATALOGUE_URL", "").strip(),
        catalogue_keys=_split_csv(os.getenv("LYNEWRIGHT_CATALOGUE_KEYS", "")),
        catalogue_internal_endpoint_ref=os.getenv("LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF", "").strip(),
        catalogue_allow_dev_key=_boolean("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", False),
        catalogue_file_max_bytes=min(_integer("LYNEWRIGHT_CATALOGUE_FILE_MAX_BYTES", 1_048_576, minimum=1), 1_048_576),
        catalogue_wheel_max_bytes=min(_integer("LYNEWRIGHT_CATALOGUE_WHEEL_MAX_BYTES", 100 * 1024 * 1024, minimum=1), 100 * 1024 * 1024),
        install_policy=install_policy,
    )


def feature_enabled(name: str) -> bool:
    return bool(get_settings().feature_flags.get(name))
