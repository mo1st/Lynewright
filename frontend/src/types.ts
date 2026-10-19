export type AiSettingsPayload = {
  provider: string;
  api_keys: Record<string, string>;
  model: string;
  base_url: string;
};

export type AiSettingsRead = {
  provider: string;
  model: string;
  base_url: string;
  api_keys: Record<string, string>;
  api_key_set: boolean;
  updated_at: string;
  storage_warning: string;
  providers: AiProviderProfile[];
};

export type AiProviderProfilePayload = {
  name: string;
  provider: string;
  protocol: "openai_chat" | "openai_responses" | "anthropic_messages" | "gemini_generate" | "vertex_generate" | "codex" | "gemini_cli";
  model: string;
  base_url: string;
  api_key?: string | null;
  credential_ref: string;
  priority: number;
  enabled: boolean;
  /** Codex only: low | medium | high | xhigh …; blank = the default. */
  reasoning_effort?: string;
};

export type AiProviderProfile = Omit<AiProviderProfilePayload, "api_key"> & {
  id: number;
  api_key_set: boolean;
  api_key_masked: string;
  updated_at: string;
};

export type AiProviderTestResponse = {
  ok: boolean;
  provider:
    | "mock"
    | "codex"
    | "gemini_cli"
    | "gemini"
    | "vertex"
    | "openai"
    | "openai_compatible"
    | "anthropic"
    | "grok"
    | "mistral"
    | "deepseek";
  model: string;
  base_url: string;
  fallback_available: boolean;
  error: string | null;
  /** failure_kind, endpoint, http_status, provider_response, attempts… */
  details?: Record<string, string | number | boolean | null>;
  hint?: string;
};

export type AiModelOption = {
  id: string;
  name: string;
  /** Codex only: the reasoning efforts this model supports. */
  efforts?: string[];
  default_effort?: string;
};

export type AIProviderCredentialMode = "credential_ref" | "env_var" | "raw_key";

export type AIProviderProtocol = "openai_chat" | "openai_responses" | "anthropic_messages" | "gemini_generate" | "vertex_generate" | "codex" | "gemini_cli";

export type AIProviderTaskSettings = {
  task_type: string;
  provider: string;
  protocol: AIProviderProtocol;
  model: string;
  credential_mode: AIProviderCredentialMode;
  api_key_env_name: string;
  credential_ref: string;
  api_key_value_masked: string;
  base_url: string;
  fallback_provider?: string;
  fallback_model?: string;
  /** Codex routes only: low | medium | high | xhigh …; blank = the runner default. */
  reasoning_effort?: string;
  enabled: boolean;
};

export type ApplicationLogLevel = "debug" | "info" | "warning" | "error" | "critical";

export type ApplicationLog = {
  id: number; level: ApplicationLogLevel; category: string; source: string;
  message: string; details: Record<string, unknown>; correlation_id: string;
  request_path: string; project_id?: string | null; created_at: string;
};

export type ApplicationLogList = {
  items: ApplicationLog[]; total: number; counts: Record<string, number>;
};

export type AITaskReadiness = {
  task_type: string; provider: string; model: string;
  enabled: boolean; key_configured: boolean; will_use_fallback: boolean;
};

export type OperatorRunState =
  | "queued"
  | "running"
  | "waiting_tool"
  | "waiting_job"
  | "waiting_approval"
  | "sleeping"
  | "completed"
  | "failed"
  | "cancelled";

export type OperatorProfile = {
  id: string;
  project_id: string;
  name: string;
  enabled: boolean;
  instructions: string;
  granted_capabilities: string[];
  max_steps: number;
  max_consecutive_tool_failures: number;
  max_model_failures: number;
  max_total_tokens: number;
  max_model_decisions: number;
  max_network_attempts: number;
  no_progress_limit: number;
  retrieval_mode?: "lexical" | "hybrid_local";
  retrieval_canary_percent?: number;
  decision_policy?: "cloud_only" | "needle_shadow" | "needle_assisted" | "needle_only";
  needle_canary_percent?: number;
  needle_read_confidence?: number;
  needle_write_confidence?: number;
  needle_sensitive_confidence?: number;
  created_at?: string;
  updated_at?: string;
};

export type OperatorProfileInput = {
  name: string;
  project_id?: string;
  enabled?: boolean;
  instructions?: string;
  granted_capabilities?: string[];
  max_steps?: number;
  max_consecutive_tool_failures?: number;
  max_model_failures?: number;
  max_total_tokens?: number;
  max_model_decisions?: number;
  max_network_attempts?: number;
  no_progress_limit?: number;
  retrieval_mode?: "lexical" | "hybrid_local";
  retrieval_canary_percent?: number;
  decision_policy?: "cloud_only" | "needle_shadow" | "needle_assisted" | "needle_only";
  needle_canary_percent?: number;
  needle_read_confidence?: number;
  needle_write_confidence?: number;
  needle_sensitive_confidence?: number;
};

export type OperatorRunStep = {
  sequence: number;
  step_type: string;
  status: string;
  tool_name: string;
  input_tokens?: number;
  output_tokens?: number;
  cached_input_tokens?: number;
  reasoning_tokens?: number;
  provider_attempts?: number;
  prompt_chars?: number;
  decision_backend?: string;
  decision_confidence?: number | null;
  decision_risk_class?: string;
  needle_latency_ms?: number;
  needle_schema_fingerprint?: string;
  evidence_validation_result?: string;
  sensitive_verification_required?: boolean;
  fallback_reason?: string;
  duration_ms: number;
  error_code: string;
  error_summary: string;
  created_at: string;
};

export type OperatorRun = {
  id: string;
  profile_id: string;
  project_id: string;
  objective: string;
  status: OperatorRunState | string;
  summary: string;
  current_step: number;
  pending_approval_id: string;
  error_code: string;
  error_summary: string;
  wake_at?: string | null;
  last_provider?: string;
  last_model?: string;
  decision_policy?: "cloud_only" | "needle_shadow" | "needle_assisted" | "needle_only";
  needle_canary_percent?: number;
  needle_read_confidence?: number;
  needle_write_confidence?: number;
  needle_sensitive_confidence?: number;
  decision_model_checksum?: string;
  tool_schema_fingerprint?: string;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  usage_summary?: {
    attempts: number;
    network_attempts: number;
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number;
    reasoning_tokens: number;
    total_tokens: number;
    unknown_attempts: number;
    http_attempts: number;
    cloud_input_tokens?: number;
    cloud_output_tokens?: number;
    cloud_total_tokens?: number;
    local_decisions?: number;
    needle_accepted?: number;
    needle_rejected?: number;
    needle_fallbacks?: number;
    estimated_cloud_tokens_avoided?: number;
  };
  steps?: OperatorRunStep[];
};

export type OperatorDecisionModel = {
  installed: boolean;
  healthy: boolean;
  deployment_safe?: boolean;
  needle_installed?: boolean;
  engine_verified?: boolean;
  judge_route_ready?: boolean;
  ready_for_shadow?: boolean;
  ready_for_control?: boolean;
  judge_provider?: string;
  judge_model?: string;
  package: string;
  package_version: string;
  generation: string;
  engine_checksum: string;
  global_mode: "off" | "shadow" | "canary";
  profile_policies: Record<string, number>;
  acceptance_rate: number;
  fallback_rate: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  cloud_tokens_avoided: number;
  fallback_reasons: Record<string, number>;
};

export type OperatorWorkflow = {
  id: string;
  project_id: string;
  name: string;
  objective_template: string;
  plan: Record<string, unknown>;
  contract_revisions: Record<string, unknown>;
  status: "draft" | "reviewed" | "enabled" | "stale" | string;
  version: number;
  reviewed_by: string;
  reviewed_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type OperatorApproval = {
  id: string;
  run_id: string;
  project_id: string;
  action: string;
  summary: string;
  status: "pending" | "approved" | "denied" | "cancelled" | string;
  resolution_note: string;
  resolved_by?: string;
  created_at: string;
  resolved_at?: string | null;
};

export type OperatorAutomationTriggerConfig =
  | { kind: "daily"; time: string; timezone: string }
  | { kind: "weekly"; days: string[]; time: string; timezone: string }
  | { kind: "interval"; interval_seconds: number }
  | { patterns: string[] }
  | { all: Array<{ metric: string; op: string; value?: string | number | boolean }> };

export type OperatorAutomation = {
  id: string;
  project_id: string;
  name: string;
  enabled: boolean;
  trigger_type: "schedule" | "event" | "condition" | string;
  trigger_config: OperatorAutomationTriggerConfig | Record<string, unknown>;
  objective_template: string;
  objective_title: string;
  cooldown_seconds: number;
  next_due_at_epoch?: number | null;
  last_triggered_at?: string | null;
  schedule?: { kind: string; timezone: string; time: string; days: string[]; interval_seconds: number };
  created_at?: string;
  updated_at?: string;
};

export type OperatorAutomationInput = {
  name: string;
  trigger_type: string;
  trigger_config: Record<string, unknown>;
  objective_template: string;
  objective_title?: string;
  project_id?: string | null;
  cooldown_seconds?: number;
  enabled?: boolean;
};

export type InteractionConnection = {
  id: string;
  project_id: string;
  channel_type: string;
  name: string;
  external_identity: string;
  /** Empty means automatic selection; otherwise this bot's chosen agent role. */
  operator_profile_id: string;
  enabled: boolean;
  verified_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** "polling" needs no public address; "webhook" is registered by the app. */
  delivery_mode?: "polling" | "webhook";
  webhook_url?: string;
  last_update_at?: string | null;
  last_error?: string;
};

export type TelegramSettings = {
  enabled: boolean;
  webhook_available: boolean;
  public_url: string;
};

export type LinkTokenStatus = {
  id: string;
  linked: boolean;
  expired: boolean;
  external_username: string;
};

export type InteractionConnectionCreated = InteractionConnection & {
  webhook_secret: string;
  bot_username: string;
  webhook_path?: string;
};

export type InteractionLinkToken = {
  id: string;
  token: string;
  connection_id: string;
  project_id: string;
  expires_at_epoch: number;
  ttl_seconds: number;
  /** https://t.me/<bot>?start=<token>; empty until the bot is verified. */
  deep_link?: string;
};

export type InteractionLink = {
  id: string;
  project_id: string;
  user_id: number;
  connection_id: string;
  channel_type: string;
  external_user_id: string;
  external_username: string;
  status: string;
  created_at?: string | null;
};

export type NotificationRouteChannel = {
  channel: "telegram" | "email" | "webhook" | string;
  target?: string;
  chat_ref?: string;
};

export type NotificationRoute = {
  id: string;
  project_id: string;
  event_class: string;
  intent: "IMMEDIATE" | "DIGEST" | "SILENT" | string;
  channels: NotificationRouteChannel[];
  enabled: boolean;
  updated_at?: string | null;
};

export type NotificationSendResult = {
  duplicate?: boolean;
  event_class?: string;
  results: Array<{ channel: string; target?: string; status: string; detail?: string }>;
};

export type IntegrationManagementResourceType = {
  type: string;
  label: string;
  plural_label: string;
  parent_type: string | null;
  discoverable: boolean;
  bindable: boolean;
  operations: string[];
  display_fields: string[];
  discovery_contract: string | null;
  discovery_operation: string | null;
  operation_contract?: string | null;
  operation_names?: Record<string, string>;
  fields?: IntegrationSettingSchemaField[];
};

export type IntegrationSettingSchemaField = {
  key: string;
  label: string;
  type: "text" | "number" | "boolean" | "enum" | "credential_ref" | "list" | string;
  description?: string;
  required?: boolean;
  default?: unknown;
  options?: string[];
  min?: number;
  max?: number;
  max_items?: number;
  credential_kind?: string;
  runtime_binding?: { target: string; name: string; collection?: boolean };
  required_when?: Record<string, unknown>;
  normalization?: "trim" | "hostname" | string;
};

export type IntegrationSettingsModel = {
  schema_id: string;
  fields: IntegrationSettingSchemaField[];
  values: Record<string, unknown>;
  updated_at?: string | null;
};

export type IntegrationManagementConnection = {
  id: string;
  integration_id: string;
  name: string;
  project_id: string | null;
  endpoint_url: string;
  origin_kind: string;
  internal_endpoint_ref: string;
  credential_refs: string[];
  configuration?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  enabled: boolean;
  health: Record<string, unknown>;
  last_health_check_at: string | null;
  last_error: string;
  discovered_tools?: IntegrationMcpTool[];
  created_at?: string;
  updated_at?: string;
};

export type IntegrationProvenance = {
  kind: "bundled" | "catalogue" | "outside";
  status: "current" | "catalogue_drift" | string;
  source?: string | null;
  confirmed_by?: string | null;
  confirmed_at?: string | null;
  recorded_at: string;
  catalogue_entry_id?: string | null;
  entry_version?: string | null;
  sha256?: string | null;
  image_digest?: string | null;
  catalogue_serial?: number | null;
  signing_key_id?: string | null;
  review?: { reviewer?: string | null; date?: string | null } | null;
  drift_reason?: string | null;
};

export type IntegrationInstallConfirmation = { integration_id: string; summary_hash: string };
export type IntegrationInstallSummary = {
  integration_id: string;
  name: string;
  version: string;
  kind: "declarative file" | "native package" | "container image" | string;
  source: string;
  declared_source?: string;
  sha256?: string | null;
  runs_code: boolean;
  execution: string;
  requested_capabilities: string[];
  egress: string;
  credential_refs: string[];
  media_access: string[] | string | boolean | null;
  catalogue_status: "not listed" | "id listed but hash differs" | "yanked" | "matched" | string;
};

export type IntegrationManagementSummary = {
  integration: {
    id: string;
    name: string;
    version: string;
    type: string;
    status: string;
    enabled: boolean;
    requested_capabilities: string[];
    granted_capabilities: string[];
    permission_mode: string;
    last_error: string;
    product_role?: string;
    kind?: string;
    source?: "bundled" | "package" | "manual" | string;
    source_distribution?: string;
    source_version?: string;
    source_entrypoint?: string;
    execution_plane?: "direct" | "worker" | "container" | string;
    trusted_inprocess?: boolean;
    manifest?: Record<string, unknown>;
    configuration?: { manual?: { runtime?: string } };
  };
  provenance?: IntegrationProvenance | null;
  availability: {
    available?: boolean;
    enabled: boolean;
    configured: boolean;
    manual: boolean;
    declarative?: boolean;
    product_role?: string;
    kind?: string;
    package_status?: string;
    execution_plane?: string;
    runner_status?: "connected" | "stale" | "never_seen" | string;
    runner_last_seen_at?: string | null;
  };
  capabilities: { requested: string[]; granted: string[]; permission_mode: string };
  connections: IntegrationManagementConnection[];
  management: {
    sections: string[];
    resource_types: IntegrationManagementResourceType[];
    supported_actions: string[];
    settings_schema?: IntegrationSettingsModel | null;
  };
  activity: { connection_count: number; resource_count: number; binding_count: number };
  setup_requirements: Array<{ id: string; binding_id: string; requirement_type: string; state: string; instructions: Record<string, unknown>; verification?: Record<string, unknown> }>;
  health: Record<string, unknown>;
  attention: string[];
  attention_items: IntegrationAttentionItem[];
  oauth: { supported: boolean };
  inbound: IntegrationInboundHandler[];
};

export type IntegrationPackageStatus = {
  distribution: string;
  version: string;
  status: string;
  message: string;
  integration_id?: string;
};

export type IntegrationAttentionItem = {
  id: string;
  kind: string;
  severity: "info" | "warning" | "error" | string;
  binding_id: string;
  outbox_id: string;
  code: string;
  message: string;
  payload: Record<string, unknown>;
  status: string;
  updated_at: string;
};

export type IntegrationInboundHandler = {
  id: string;
  handler_id: string;
  kind: "webhook" | "callback" | string;
  connection_id: string | null;
  endpoint: string;
  verification_mode: string;
  signature_header: string;
  timestamp_header: string;
  delivery_header: string;
};

export type IntegrationOAuthStatus = {
  integration_id: string;
  connection_id: string;
  state: "DISABLED" | "NOT_AUTHORIZED" | "AUTHORIZED" | "EXPIRED" | string;
  token_type?: string;
  scope?: string;
  expires_at_epoch?: number | null;
  expired?: boolean;
  updated_at?: string;
};

export type IntegrationExternalResource = {
  id: string;
  integration_id: string;
  connection_id: string;
  external_id: string;
  resource_type: string;
  parent_external_id: string;
  name: string;
  url: string;
  remote_status: string;
  state: "ACTIVE" | "STALE" | "REMOTE_MISSING" | string;
  metadata: Record<string, unknown>;
  available_actions: string[];
  binding_status: string;
  binding: {
    id: string;
    revision: number;
    state: string;
    resource_id: string;
    ownership_mode: string;
  } | null;
  last_discovered_at: string | null;
  updated_at: string;
  setup_requirements?: Array<{ id: string; requirement_type: string; state: string }>;
  attention?: IntegrationAttentionItem[];
};

export type IntegrationOpenApiOperation = {
  operation_id: string;
  method: string;
  path: string;
  summary: string;
  approved: boolean;
  destructive: boolean;
};

export type IntegrationMappingPathSuggestion = {
  path: string;
  score: number;
  reason: string;
};

export type IntegrationMappingSuggestions = {
  items_path: IntegrationMappingPathSuggestion[];
  id_path: IntegrationMappingPathSuggestion[];
  name_path: IntegrationMappingPathSuggestion[];
  url_path: IntegrationMappingPathSuggestion[];
  status_path: IntegrationMappingPathSuggestion[];
};

export type IntegrationOpenApiResourceMapping = {
  mapping_id: string;
  resource_type: string;
  label: string;
  plural_label: string;
  list_operation_id: string;
  get_operation_id: string | null;
  create_operation_id: string | null;
  update_operation_id: string | null;
  delete_operation_id: string | null;
  get_external_id_target: string | null;
  update_external_id_target: string | null;
  delete_external_id_target: string | null;
  items_path: string;
  id_path: string;
  name_path: string;
  parent_id_path: string | null;
  url_path: string | null;
  status_path: string | null;
  display_fields: string[];
  metadata_paths: Record<string, string>;
  pagination: Record<string, unknown> | null;
  enabled: boolean;
};

export type IntegrationMcpResourceMapping = {
  mapping_id: string;
  resource_type: string;
  label: string;
  plural_label: string;
  list_tool_name: string;
  list_arguments: Record<string, string | number | boolean | null>;
  get_tool_name: string | null;
  create_tool_name: string | null;
  update_tool_name: string | null;
  delete_tool_name: string | null;
  get_external_id_target: string | null;
  update_external_id_target: string | null;
  delete_external_id_target: string | null;
  items_path: string;
  id_path: string;
  name_path: string;
  parent_id_path: string | null;
  url_path: string | null;
  status_path: string | null;
  display_fields: string[];
  metadata_paths: Record<string, string>;
  enabled: boolean;
};

export type IntegrationResourceActionResult = {
  action: string;
  inventory: IntegrationExternalResource;
  binding: IntegrationExternalResource["binding"];
  result: Record<string, unknown>;
};

export type IntegrationMcpTool = {
  name: string;
  description: string;
  input_schema?: Record<string, unknown>;
  approved: boolean;
  read_only?: boolean | null;
  destructive?: boolean | null;
  safety_known?: boolean;
};

export type IntegrationMappingPreview = {
  mapping: IntegrationOpenApiResourceMapping | IntegrationMcpResourceMapping;
  projected_resources: Array<Record<string, unknown>>;
  raw_sample: unknown;
  raw_sample_truncated: boolean;
  raw_sample_serialized_bytes: number;
  suggestions: IntegrationMappingSuggestions;
  path_errors: string[];
  runtime: "mcp" | "openapi" | string;
};

export type IntegrationMcpToolProbeResult = {
  integration_id: string;
  connection_id: string;
  tool_name: string;
  result: unknown;
  truncated: boolean;
  serialized_bytes: number;
};

export type IntegrationMcpToolSchemaResult = {
  integration_id: string;
  connection_id: string;
  tool_name: string;
  input_schema: unknown;
  truncated: boolean;
  serialized_bytes: number;
};

export type IntegrationMcpContractTarget = {
  contract: string;
  operation: string;
  required_capability: string;
  risk_class?: string;
  output_schema: Record<string, unknown>;
};

export type IntegrationMcpContractMapping = {
  mapping_id: string;
  tool_name: string;
  target_contract: string;
  target_operation: string;
  argument_mapping: Record<string, IntegrationMcpMappingValue>;
  output_mapping: Record<string, IntegrationMcpMappingValue>;
  enabled: boolean;
  write_enabled?: boolean;
  destructive?: boolean;
  required_capability?: string;
  risk_class?: string;
  output_schema?: Record<string, unknown>;
};

export type IntegrationMcpMappingValue = string | number | boolean | null | { $literal: string | number | boolean | null };

export type IntegrationOpenApiContractMapping = {
  mapping_id: string;
  operation_id: string;
  target_contract: string;
  target_operation: string;
  argument_mapping: Record<string, string | number | boolean | null>;
  output_mapping: Record<string, string | number | boolean | null>;
  enabled: boolean;
  write_enabled: boolean;
  destructive?: boolean;
  required_capability?: string;
  risk_class?: string;
  output_schema?: Record<string, unknown>;
};
