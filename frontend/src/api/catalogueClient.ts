import { apiRequest } from "./client";

export type CatalogueLimits = {
  cpus: number;
  memory_mb: number;
  pids: number;
  timeout_seconds: number;
};

export type CatalogueEntry = {
  id: string;
  name?: string;
  summary?: string;
  description?: string;
  version: string;
  kind: "file" | "package" | "container";
  licence: string;
  source: string;
  sha256?: string;
  lynewright_version: string;
  permissions: {
    capabilities: string[];
    egress: "none" | "internet";
    media: "none" | "read" | "write";
    credential_refs: string[];
  };
  limits?: CatalogueLimits;
  requires_operator_setup?: string[];
  review: { reviewer: string; date: string; catalogue_commit: string; notes_url?: string };
  yanked?: { reason: string; date: string };
  installed?: boolean;
  installation_incomplete?: boolean;
  provenance_kind?: "bundled" | "catalogue" | "outside" | null;
  provenance_status?: string | null;
};

export type CatalogueStatus = {
  url: string;
  status: "offline" | "empty" | "ready" | "error" | "untrusted";
  trusted_key_ids: Array<{ id: string; source: string }>;
  dev_key_trusted: boolean;
  dev_key_warning: boolean;
  dev_key_warning_message: string | null;
  verified_key_id: string | null;
  serial: number | null;
  generated_at: string | null;
  fetched_at: string | null;
  last_error: string | null;
};

export type CatalogueResponse = { status: CatalogueStatus; entries: CatalogueEntry[] };

export const catalogueApi = {
  get() { return apiRequest<CatalogueResponse>("/api/catalogue"); },
  refresh() { return apiRequest<CatalogueResponse>("/api/catalogue/refresh", { method: "POST" }); },
  install(id: string, version: string, internalEndpointRef = "") {
    return apiRequest<{ integration: unknown; installed: boolean }>(
      `/api/catalogue/entries/${encodeURIComponent(id)}/${encodeURIComponent(version)}/install`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ internal_endpoint_ref: internalEndpointRef }),
      },
    );
  },
  repair(id: string, version: string) {
    return apiRequest<{ integration: unknown; repaired: boolean }>(
      `/api/catalogue/entries/${encodeURIComponent(id)}/${encodeURIComponent(version)}/repair`,
      { method: "POST" },
    );
  },
};
