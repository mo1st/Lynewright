# Automation plans API

The first automation-planning release lets an authorized assistant search installed operations, draft and validate a production-line definition, and request approval to add that line in a disabled state. It does not install projects, enable lines, or schedule runs.

## Authentication and permissions

External REST clients use a Lynewright client credential provisioned with audience `api`. The Assistants settings panel can create either an MCP credential or a REST API credential. REST calls send that token as a Bearer credential to `/api/automation`; an MCP credential is rejected by these routes, and a REST credential is rejected by `/mcp`.

Grant only the scopes the client needs:

- `automation:plan` creates or revises a plan.
- `integrations:discover` searches installed operation schemas.
- `automation:read` reads plans and approval status.
- `automation:provision` applies a current, approved plan by creating its disabled preview line.

An authenticated tenant administrator approves a concrete revision from Settings → Assistants. Approval is stored against the server-generated plan hash and expires after 30 days. It authorizes creation of that disabled line only; enabling and running it remain separate actions.

## Routes

All routes require a project selected by `project_id` or `X-Lynewright-Project-ID`. Service credentials must also be scoped to that project.

| Method | Route | Access |
| --- | --- | --- |
| `GET` | `/api/automation/operations?q=...&limit=20` | `integrations:discover` |
| `GET` | `/api/automation/plans` | `automation:read` |
| `POST` | `/api/automation/plans` | `automation:plan` |
| `GET` | `/api/automation/plans/{plan_id}` | `automation:read` |
| `POST` | `/api/automation/plans/{plan_id}/authorize` | Tenant administrator session |
| `POST` | `/api/automation/plans/{plan_id}/apply` | `automation:provision` plus an unexpired approval |

Plan creation accepts `project_id`, `objective`, `line_name`, a standard production-line `definition`, and optional `constraints`. A revision also supplies `plan_id` and `expected_revision`; stale revisions are rejected. Plan definitions are checked against operations currently available to the requesting client and their operation revisions are included in the plan hash. Inline secret-like fields are rejected; integrations should use configured credentials.

Applying a plan rechecks the operation revisions and writes the line with `enabled=false`. A changed operation requires a new plan revision and a new administrator review. The assistant receives no raw credential values or approval authority.
