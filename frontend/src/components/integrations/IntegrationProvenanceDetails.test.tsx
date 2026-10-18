/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import IntegrationProvenanceDetails from "./IntegrationProvenanceDetails";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("IntegrationProvenanceDetails", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  it("renders catalogue provenance details and the drift warning", () => {
    act(() => root.render(<IntegrationProvenanceDetails provenance={{
      kind: "catalogue", status: "catalogue_drift", recorded_at: "2026-10-15T12:00:00Z",
      catalogue_entry_id: "fixture.openapi", entry_version: "1.0.0", sha256: "a".repeat(64),
      catalogue_serial: 8, signing_key_id: "ed25519:deadbeef",
      review: { reviewer: "Security team", date: "2026-10-01" }, drift_reason: "Catalogue entry is yanked: review withdrawn.",
    }} />));
    expect(host.textContent).toContain("Catalogue");
    expect(host.textContent).toContain("Catalogue serial: 8");
    expect(host.textContent).toContain("Reviewed by Security team on 2026-10-01");
    expect(host.textContent).toContain("Catalogue drift");
    expect(host.textContent).toContain("Catalogue entry is yanked: review withdrawn.");
  });

  it("renders the outside warning badge", () => {
    act(() => root.render(<IntegrationProvenanceDetails provenance={{
      kind: "outside", status: "current", recorded_at: "2026-10-15T12:00:00Z",
      source: "file sha256:abc", confirmed_by: "admin-1",
    }} />));
    expect(host.textContent).toContain("Outside");
    expect(host.textContent).toContain("Installed by admin-1");
  });

  it("marks provenance signed by the development key", () => {
    act(() => root.render(<IntegrationProvenanceDetails provenance={{
      kind: "catalogue", status: "current", recorded_at: "2026-10-15T12:00:00Z",
      catalogue_entry_id: "fixture.openapi", entry_version: "1.0.0", sha256: "a".repeat(64),
      signing_key_id: "dev:deadbeef",
    }} />));
    expect(host.textContent).toContain("Catalogue (dev key)");
    expect(host.querySelector("svg")).not.toBeNull();
  });

  it("marks a package as awaiting confirmation", () => {
    act(() => root.render(<IntegrationProvenanceDetails provenance={{
      kind: "outside", status: "awaiting_confirmation", recorded_at: "2026-10-15T12:00:00Z",
      source: "package:fixture==1.0.0",
    }} />));
    expect(host.textContent).toContain("Outside · Awaiting confirmation");
    expect(host.textContent).toContain("remains disabled");
  });

  it("shows the red policy status for outside installs in catalogue-only mode", () => {
    act(() => root.render(<IntegrationProvenanceDetails installPolicy="catalogue-only" provenance={{
      kind: "outside", status: "current", recorded_at: "2026-10-15T12:00:00Z",
      source: "file sha256:abc", confirmed_by: "admin-1",
    }} />));
    expect(host.textContent).toContain("Not allowed by policy");
    expect(host.querySelector(".callout--danger")).not.toBeNull();
    expect(host.textContent).toContain("may keep running while enabled");
  });
});
