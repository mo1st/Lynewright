/**
 * Contract tests for the Operator API client.
 *
 * This module is the single boundary between the product UI and the Operator
 * management API, so it is where a contract drift shows up first. The UI was
 * built against the frozen plan contract while the API was built in parallel,
 * and exactly one mismatch slipped through: `bootstrap()` sent no body while
 * the API requires `project_id`, which 422'd. These tests pin the request
 * shapes so that class of drift fails here instead of in the browser.
 *
 * No DOM and no component rendering: `vitest` and `jsdom` were already
 * dependencies, `@testing-library/react` is not, and adding a dependency was
 * out of scope.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiRequest = vi.fn();
vi.mock("./client", () => ({ apiRequest: (...args: unknown[]) => apiRequest(...args) }));

// Imported after the mock is registered.
const { operatorApi } = await import("./operatorClient");

function lastCall(): { path: string; options?: RequestInit } {
  const calls = apiRequest.mock.calls;
  const call = calls[calls.length - 1];
  if (!call) throw new Error("apiRequest was never called");
  return { path: call[0] as string, options: call[1] as RequestInit | undefined };
}

function lastBody(): Record<string, unknown> {
  const { options } = lastCall();
  return JSON.parse(String(options?.body ?? "{}"));
}

beforeEach(() => {
  apiRequest.mockReset();
  apiRequest.mockResolvedValue({});
});

describe("bootstrap", () => {
  it("sends project_id, which the API requires", async () => {
    await operatorApi.bootstrap("proj_42");
    const { path, options } = lastCall();
    expect(path).toBe("/api/operator/bootstrap");
    expect(options?.method).toBe("POST");
    // The regression this pins: a bodyless POST is a guaranteed 422.
    expect(lastBody()).toMatchObject({ project_id: "proj_42" });
  });

  it("unwraps the profile from the bootstrap envelope", async () => {
    apiRequest.mockResolvedValue({ profile: { id: "prof_1" }, created: true });
    await expect(operatorApi.bootstrap("proj_42")).resolves.toMatchObject({ id: "prof_1" });
  });
});

describe("list envelope tolerance", () => {
  // The plan froze the routes but not the envelope shape, so the client must
  // accept both. This is what absorbed the API's `{"runs": [...]}` choice.
  it("accepts a keyed object", async () => {
    apiRequest.mockResolvedValue({ runs: [{ id: "run_1" }] });
    await expect(operatorApi.listRuns()).resolves.toHaveLength(1);
  });

  it("accepts a bare array", async () => {
    apiRequest.mockResolvedValue([{ id: "run_1" }, { id: "run_2" }]);
    await expect(operatorApi.listRuns()).resolves.toHaveLength(2);
  });

  it("degrades to an empty list rather than throwing", async () => {
    apiRequest.mockResolvedValue(null);
    await expect(operatorApi.listRuns()).resolves.toEqual([]);
  });
});

describe("request shapes", () => {
  it("targets the documented run paths", async () => {
    await operatorApi.getRun("run_1");
    expect(lastCall().path).toBe("/api/operator/runs/run_1");

    await operatorApi.cancelRun("run_1");
    expect(lastCall().path).toBe("/api/operator/runs/run_1/cancel");
    expect(lastCall().options?.method).toBe("POST");

    await operatorApi.resumeRun("run_1");
    expect(lastCall().path).toBe("/api/operator/runs/run_1/resume");
  });

  it("sends decision and note when resolving an approval", async () => {
    await operatorApi.resolveApproval("ap_1", "approved", "looks fine");
    expect(lastCall().path).toBe("/api/operator/approvals/ap_1/resolve");
    expect(lastBody()).toMatchObject({ decision: "approved", note: "looks fine" });
  });

  it("encodes identifiers so a stray slash cannot forge a path", async () => {
    await operatorApi.getRun("run/../../admin");
    expect(lastCall().path).toBe("/api/operator/runs/run%2F..%2F..%2Fadmin");
  });

  it("omits empty query parameters instead of sending blanks", async () => {
    await operatorApi.listRuns(20, undefined);
    expect(lastCall().path).toBe("/api/operator/runs?limit=20");
  });
});
