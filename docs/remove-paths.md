# Add and remove paths

This audit maps user-facing creation paths to their matching removal actions. “API” names the route that creates or removes the durable item. “UI” names the page or control exposed to an operator. Run history and audit records are retained unless noted.

| Addable item | Add/create path | Remove path | API | UI | Notes |
|---|---|---|---|---|---|
| Bundled integration | Startup discovery | `POST /api/integration-management/{id}/disable` | ✓ | ✓ | Bundled code is part of Lynewright and cannot be uninstalled. |
| Manual integration | `POST /api/integration-management/manual` | Preview + `DELETE /api/integration-management/{id}` | ✓ | ✓ | Generic removal supports all non-bundled kinds. |
| Declarative file import | `POST /api/integration-management/import` | Preview + `DELETE /api/integration-management/{id}` | ✓ | ✓ | Includes outside files and catalogue files. |
| Link/docs draft enable | `POST /api/integration-management/drafts/{id}/enable` | Preview + `DELETE /api/integration-management/{id}` | ✓ | ✓ | Draft itself has a separate discard action. |
| Catalogue file install | `POST /api/catalogue/entries/{id}/{version}/install` | Preview + `DELETE /api/integration-management/{id}` | ✓ | ✓ | Removing the install makes the catalogue entry installable again. |
| Catalogue package install | `POST /api/catalogue/entries/{id}/{version}/install` | Preview + `DELETE /api/integration-management/{id}` | ✓ | ✓ | Revokes runner tokens, removes the stored wheel, and unregisters live definitions. |
| Catalogue container install | `POST /api/catalogue/entries/{id}/{version}/install` | Preview + `DELETE /api/integration-management/{id}` | ✓ | ✓ | Lynewright asks the operator to stop/remove the container; it never controls containers. |
| Outside package confirmation | `POST /api/integration-management/{id}/confirm-install` | Preview + `DELETE /api/integration-management/{id}` | ✓ | ✓ | Listed packages must also be removed from `LYNEWRIGHT_INTEGRATION_PACKAGES`. |
| Integration connections | `POST /api/integration-management/{id}/connections` | `DELETE /api/integration-management/{id}/connections/{connection_id}` | ✓ | ✓ | Remote resources and credentials are not deleted. |
| MCP/OpenAPI contract and resource mappings | `POST /api/integration-management/{integration_id}/connections/{connection_id}/mcp/contract-mappings` or the matching OpenAPI/MCP resource-mapping route | `DELETE /api/integration-management/{integration_id}/connections/{connection_id}/mcp/contract-mappings/{mapping_id}` (also the corresponding OpenAPI contract/resource and MCP resource routes) | ✓ | ✓ | Mapping rows have per-row remove controls. |
| External resources created through a declared integration action | `POST /api/integration-management/{integration_id}/connections/{connection_id}/resources/{resource_type}/action` | `POST /api/integration-management/{integration_id}/connections/{connection_id}/resources/{resource_type}/action` (`action=delete`, `archive` or `retire` when declared) | ✓ | ✓ | Only integration-declared managed resources expose destructive actions; each uses an in-app confirmation. |
| Resource bindings | `POST /api/integration-management/resources/{inventory_id}/bind` | `POST /api/integration-management/bindings/{binding_id}/unbind` | ✓ | ✓ | Unbind detaches the relationship; the remote resource and run history stay. |
| Stored credentials | `PUT /api/settings/credentials` | `DELETE /api/settings/credentials/{name}` | ✓ | ✓ | Removal is blocked while any consumer still references the credential. |
| Runner tokens | `POST /api/integration-management/{id}/runner-token` | `DELETE /api/integration-management/{id}/runner-token` | ✓ | ✓ | Revocation is available from the Runner section. |
| Production lines | `POST /api/production-lines` | `DELETE /api/production-lines/{line_id}` | ✓ | ✓ | Active runs block deletion; run history remains. |
| Production runs | `POST /api/production-lines/{line_id}/runs` | `DELETE /api/production-lines/runs/{run_id}` (terminal runs are archived; active runs can be cancelled) | ✓ | ✓ | Archive hides a terminal run from the list and retains its history and review trail. |
| Operator runtime runs | `POST /api/operator/runs` | `DELETE /api/operator/runs` with the selected project (clears terminal runs only) | ✓ | ✓ | The Operator panel can clear completed, failed and cancelled runs; live runs are kept. |
| AI provider profiles | `POST /api/settings/ai/providers` | `DELETE /api/settings/ai/providers/{profile_id}` | ✓ | ✓ | The provider table has a removal action. |
| Telegram connections | `POST /api/interactions/connections` | `DELETE /api/interactions/connections/{id}` | ✓ | ✓ | Delete action appears in Telegram settings. |
| Telegram links | `POST /api/interactions/links/token` | `DELETE /api/interactions/links/{id}` | ✓ | ✓ | Revoke action appears in Telegram settings. |
| Telegram notification routes | `PUT /api/interactions/notification-routes` | `DELETE /api/interactions/notification-routes/{event_class}` | ✓ | ✓ | Remove action appears in Telegram settings. |
| Operator profiles | `POST /api/operator/profiles` | `DELETE /api/operator/profiles/{profile_id}` | ✓ | ✓ | Removal archives a profile and is blocked while it has active runs. |
| Operator automations | `POST /api/operator/automations` | `DELETE /api/operator/automations/{automation_id}` | ✓ | ✓ | Delete action appears in the Automations section. |
| Users | `POST /api/auth/users` | `DELETE /api/auth/users/{user_id}` | ✓ | ✓ | Cannot delete yourself or the last active admin. |
| Integration drafts | `POST /api/integration-management/drafts` | `POST /api/integration-management/drafts/{id}/discard` | ✓ | ✓ | Enabled or locked drafts cannot be discarded. |
| Managed media/artifacts | `POST /api/runner/v1/invocations/{request_id}/artifacts` or integration SDK media API | `DELETE /api/media/{media_id}` or `DELETE /api/integration-management/{id}` | ✓ | ✓ | Deletion revokes links and removes owned bytes; integration removal removes all media it owns. |
| Container runtime | `GET /api/integration-management/{id}/runtime-config` generates deployment instructions | `DELETE /api/integration-management/{id}` removes the integration and tells the operator to stop/remove the container | ✓ | ✓ | Lynewright never controls containers itself. |

The guard test in `backend/app/tests/test_remove_paths.py` checks that every audited row is marked complete and that the corresponding API route is registered. UI controls are covered by the frontend removal-dialog and settings tests.

Pinned items are not a user-created feature in this Lynewright build: there is no pin/unpin API or dashboard action, so there is no pinned-item removal path to expose. It is intentionally recorded here as an audited absence rather than as a completed add/remove row.
