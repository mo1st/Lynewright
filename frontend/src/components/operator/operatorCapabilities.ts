/**
 * Public capabilities that an Operator profile may receive.
 *
 * Keep this list in one place so the profile editor and the Telegram
 * connection shortcut cannot drift apart. Granting the full list does not
 * bypass approval policy: publication, credential, and other risky actions
 * still stop for an explicit owner approval at runtime.
 */
export const PUBLIC_OPERATOR_CAPABILITIES = [
  "system:read",
  "system:diagnostics",
  "projects:read",
  "projects:create",
  "projects:update",
  "projects:delete",
  "jobs:read",
  "jobs:create",
  "jobs:cancel",
  "jobs:retry",
  "media:read",
  "media:create",
  "media:delete",
  "ai:use",
  "ai:read-config",
  "ai:configure",
  "ai:test",
  "credentials:list",
  "credentials:status",
  "credentials:set",
  "credentials:delete",
  "credentials:use",
  "settings:read",
  "settings:write",
  "network:use",
  "events:publish",
  "storage:read",
  "storage:write",
  "mappings:read",
  "mappings:write",
  "sync:read",
  "sync:write",
  "sources:read",
  "sources:write",
  "research:read",
  "publishing:write",
  "website:read",
  "customer_studio:read",
  "customer_studio:write",
  "customer_studio:publish",
  "mcp:client:use",
  "mcp:client:configure",
] as const;

export const FULL_OPERATOR_CAPABILITIES = [...PUBLIC_OPERATOR_CAPABILITIES];
