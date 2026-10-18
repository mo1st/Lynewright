from __future__ import annotations

import sqlite3

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_ui import IntegrationUIService
from app.services.platform_services import PlatformStore


def test_generic_integration_ui_models_show_connections_activity_and_safe_metadata() -> None:
    db=sqlite3.connect(":memory:"); db.row_factory=sqlite3.Row; store=PlatformStore(lambda:db); integrations=IntegrationRegistry(store); integration_id="lynewright.integration.ui-mock"
    integrations.register({"schema_version":"1.0","id":integration_id,"name":"UI Mock","version":"0.1.0","type":"service","lynewright_version":"^0.1.0","sdk_version":"^1.0.0","requested_capabilities":[],"service":{"kind":"http","health_check":"/health"},"backend":"dist/backend.mjs"}); integrations.enable(integration_id)
    connections=ConnectionProfileService(store,registry=integrations,endpoint_validator=lambda _url:{}); connections.create(integration_id,connection_id="primary",name="Primary",endpoint_url="https://api.example.test")
    contributions=ContributionRegistry(integrations,connections=connections); contributions.register(integration_id=integration_id,connection_id="primary",local_id="panel",kind="ui",metadata={"page":"settings","token":"redacted"})
    ui=IntegrationUIService(integrations,connections,contributions); summary=ui.summary(integration_id)
    assert summary["activity"]=={"connection_count":1,"contribution_count":1}; assert "token" not in summary["contributions"][0]["metadata"]; assert ui.diagnostics(integration_id)["result"]["runtime"]=="not_registered"; db.close()
