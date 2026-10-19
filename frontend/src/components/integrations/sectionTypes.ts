export type Mutate = <T>(key: string, action: () => Promise<T>, message: string) => Promise<T | undefined>;

export type ResourceField = {
  key: string;
  label: string;
  type: "text" | "number" | "boolean" | "enum" | "list" | string;
  description?: string;
  required?: boolean;
  default?: unknown;
  options?: string[];
  min?: number;
  max?: number;
  max_items?: number;
  normalization?: "trim" | "hostname" | string;
};

export type ManualIntegrationDraft = {
  name: string;
  runtime_kind: "mcp" | "openapi" | "webhook";
  endpoint_url: string;
  document: string;
  credential_refs: string;
  verification_mode: string;
  event_type: string;
  payload_mapping: string;
  signature_header: string;
  timestamp_header: string;
  delivery_header: string;
};
