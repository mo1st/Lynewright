/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRoute } from "../router";
import type { ProductionLine, ProductionOperation, ProductionRun } from "../api/productionLinesClient";

vi.mock("../api/productionLinesClient", () => ({ productionLinesApi: {
  lines: vi.fn(), operations: vi.fn(), runs: vi.fn(), run: vi.fn(),
  createLine: vi.fn(), updateLine: vi.fn(), start: vi.fn(), retry: vi.fn(), continue: vi.fn(), cancel: vi.fn(), runAction: vi.fn(), reviewItems: vi.fn(), submitReview: vi.fn(),
  rules: vi.fn(), rule: vi.fn(), updateRule: vi.fn(), revertRule: vi.fn(), testRule: vi.fn(), ruleAudit: vi.fn(), factorySettings: vi.fn(), updateFactorySettings: vi.fn(),
} }));

import { productionLinesApi } from "../api/productionLinesClient";
import Production from "./Production";

function ProductionHarness() {
  const route = useRoute();
  const view = route.page === "runs" ? "runs" : route.page === "reviews" ? "review" : "lines";
  return <Production projectId="project-1" view={view} runId={route.params.run_id} lineId={route.params.line_id} />;
}

const line: ProductionLine = {
  id: "fixture-line", name: "Fixture line", project_id: "project-1", enabled: true,
  definition: { stages: [
    { id: "transform", kind: "operation", operation_id: "lynewright.fixture.transform", contract: "lynewright.fixture", operation: "transform", args_template: { item_id: "{{run.vars.item_id}}" } },
    { id: "review", kind: "gate:review", items_template: [] },
  ] },
};
const operation: ProductionOperation = {
  operation_id: "lynewright.fixture.transform",
  kind: "native",
  contract: "lynewright.fixture", operation: "transform", description: "Transform a declared item.",
  schema: { type: "object", properties: { item_id: { type: "string", title: "Item id" } }, required: ["item_id"] },
  run_actions: [{ label: "Reprocess item", operation: "reprocess", needs_reason: true, inputs: [
    { name: "item_id", label: "Item id", required: true }, { name: "reason", label: "Reason", required: true },
  ] }],
};

let run: ProductionRun;
let host: HTMLDivElement;
let root: Root;

function sampleRun(status = "waiting"): ProductionRun {
  return {
    id: "run-42", line_id: line.id, project_id: "project-1", status, current_stage: status === "done" ? "" : "review",
    context: { vars: { title: "Review sample" }, stages: {} }, created_at: 1_790_000_000, updated_at: 1_790_000_060,
    stages: [
      { stage_id: "transform", status: "done", attempts: 1, started_at: 1_790_000_000, ended_at: 1_790_000_010, duration_s: 10, output_ref: "", output: { value: "ready" }, progress: {}, error: "" },
      { stage_id: "review", status: status === "done" ? "done" : "waiting", attempts: 0, started_at: 1_790_000_010, ended_at: null, duration_s: null, output_ref: "", output: { items: [{ id: "item-1", label: "Sample item", caption: "Check this result" }] }, progress: { label: "Pictures", step: 42, total: 96, detail: "drawing" }, error: "" },
    ],
    timeline: [{ id: 1, type: "stage.waiting", stage_id: "review", detail: { result: { progress: { label: "Pictures", step: 42, total: 96, detail: "drawing" } } }, actor_id: "", created_at: 1_790_000_010 }],
  };
}

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    await Promise.resolve();
  });
}

async function mount(url: string): Promise<void> {
  window.history.replaceState({}, "", url);
  await act(async () => {
    root = createRoot(host);
    root.render(<ProductionHarness />);
    await Promise.resolve();
  });
  await settle();
}

function buttonContaining(text: string): HTMLButtonElement {
  const button = [...host.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
  if (!button) throw new Error(`Could not find button containing: ${text}`);
  return button;
}

function changeInput(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.appendChild(host);
  run = sampleRun();
  vi.mocked(productionLinesApi.lines).mockResolvedValue({ items: [line] });
  vi.mocked(productionLinesApi.operations).mockResolvedValue({ items: [operation] });
  vi.mocked(productionLinesApi.runs).mockImplementation(async () => ({ items: [run] }));
  vi.mocked(productionLinesApi.run).mockImplementation(async () => run);
  vi.mocked(productionLinesApi.continue).mockImplementation(async () => { run = sampleRun("done"); return run; });
  vi.mocked(productionLinesApi.runAction).mockResolvedValue({ result: { ok: true } });
  vi.mocked(productionLinesApi.reviewItems).mockResolvedValue({ items: [] });
  vi.mocked(productionLinesApi.submitReview).mockImplementation(async () => ({ result: { reviewed: 1 }, run: sampleRun("done") }));
});

afterEach(() => {
  if (root) act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe("Production control center", () => {
  it("shows generic line health and opens the schema guided line editor", async () => {
    await mount("/lines");
    expect(host.textContent).toContain("Fixture line");
    expect(host.textContent).toContain("runs today");
    expect(host.querySelector(".production-metrics")).toBeNull();
    expect(host.querySelector('[aria-label="Production views"]')).toBeNull();
    expect(host.querySelector("h1")?.textContent).toBe("Lines");
    await act(async () => { buttonContaining("Edit line").click(); });
    await settle();
    expect(host.textContent).toContain("Line builder");
    expect(host.textContent).toContain("Transform a declared item.");
    expect(host.querySelector("input[aria-label='Item id (required)']")).toBeTruthy();
  });

  it("groups runs by stage and opens a timeline detail", async () => {
    await mount("/runs");
    expect(host.querySelector("h1")?.textContent).toBe("Runs");
    expect(host.querySelector(".production-metrics")).toBeNull();
    expect(host.querySelector('[aria-label="Production views"]')).toBeNull();
    expect(host.textContent).toContain("review");
    expect(host.textContent).toContain("Pictures 42 / 96");
    await act(async () => { buttonContaining("Review sample").click(); });
    await settle();
    expect(host.textContent).toContain("Run detail");
    expect(host.textContent).toContain("transform");
    expect(host.textContent).toContain("Run events");
    expect(host.textContent).toContain("Pictures 42 / 96 · drawing");
  });

  it("submits a review gate decision through the existing continue API", async () => {
    await mount("/reviews");
    await act(async () => { buttonContaining("Review sample").click(); });
    await settle();
    expect(host.textContent).toContain("Check this result");
    await act(async () => { buttonContaining("Approve").click(); });
    await act(async () => { buttonContaining("Submit review").click(); });
    await settle();
    expect(productionLinesApi.continue).toHaveBeenCalledWith("run-42", {
      approved: true,
      items: [{ id: "item-1", decision: "approved", reason: "" }],
    });
  });

  it("renders integration declared actions and sends their inputs", async () => {
    await mount("/runs");
    await act(async () => { buttonContaining("Review sample").click(); });
    await settle();
    await act(async () => { buttonContaining("Reprocess item").click(); });
    const actionInputs = host.querySelectorAll<HTMLInputElement>(".production-run-action input");
    const sectionId = actionInputs[0];
    const reason = actionInputs[1];
    await act(async () => {
      changeInput(sectionId, "item-42");
      changeInput(reason, "Invalid source data");
    });
    await act(async () => { buttonContaining("Run Reprocess item").click(); });
    await settle();
    expect(productionLinesApi.runAction).toHaveBeenCalledWith("run-42", "transform", 0, { item_id: "item-42", reason: "Invalid source data" });
  });

  it("a failed run shows one Retry and none of the step actions", async () => {
    run = { ...sampleRun(), status: "failed", current_stage: "review",
      stages: sampleRun().stages.map((item) => item.stage_id === "review" ? { ...item, status: "failed", error: "boom" } : item) };
    await mount("/runs");
    await act(async () => { buttonContaining("Review sample").click(); });
    await settle();
    expect(host.textContent).toContain("Retry failed step (review)");
    expect(host.textContent).not.toContain("Reprocess item");
  });

  it("renders generic media previews in operation review and submits decisions", async () => {
    const reviewLine: ProductionLine = { ...line, definition: { stages: [
      { id: "advance", kind: "stepped_operation", contract: operation.contract, operation: operation.operation },
    ] } };
    run = { ...sampleRun(), current_stage: "advance", stages: [{
      stage_id: "advance", status: "waiting", attempts: 1, started_at: 1_790_000_010, ended_at: null, duration_s: null,
      output_ref: "", output: { waiting: { kind: "review", ref: "factory-1" } }, progress: {}, error: "",
    }] };
    vi.mocked(productionLinesApi.lines).mockResolvedValue({ items: [reviewLine] });
    vi.mocked(productionLinesApi.runs).mockResolvedValue({ items: [run] });
    const previewUrl = "http://localhost:8000/api/media/l/opaque?tenant_id=ten_default";
    vi.mocked(productionLinesApi.reviewItems).mockResolvedValue({ items: [{
      id: "review-item-1", label: "Preview", caption: "A generic review preview.",
      media_previews: [{ media_id: "med_preview", name: "preview.png", content_type: "image/png", kind: "preview", url: previewUrl }],
    }] });
    await mount("/reviews");
    await act(async () => { buttonContaining("Review sample").click(); });
    await settle();
    expect(host.querySelector(`img[src='${previewUrl}']`)).toBeTruthy();
    expect(host.textContent).toContain("A generic review preview.");
    await act(async () => { buttonContaining("Refuse").click(); });
    const reason = host.querySelector<HTMLInputElement>(".production-operation-review input[required]")!;
    await act(async () => {
      changeInput(reason, "Title is unreadable");
    });
    await act(async () => { buttonContaining("Submit review").click(); });
    await settle();
    expect(productionLinesApi.submitReview).toHaveBeenCalledWith("run-42", [{ id: "review-item-1", decision: "refused", reason: "Title is unreadable" }]);
  });

  it("shows generic choice evidence and submits the selected option", async () => {
    const choiceLine: ProductionLine = { ...line, definition: { stages: [
      { id: "report", kind: "operation", contract: operation.contract, operation: operation.operation },
      { id: "select", kind: "gate:choice", options_template: "{{stages.report.output.ranked}}" },
    ] } };
    const selected = { option_id: "option-1", name: "Option A", score: 81, attributes: ["source one", "source two"] };
    run = { ...sampleRun(), current_stage: "select", stages: [
      { stage_id: "report", status: "done", attempts: 1, started_at: 1_790_000_000, ended_at: 1_790_000_010, duration_s: 10, output_ref: "", output: { unserved: ["Regional soups"] }, progress: {}, error: "" },
      { stage_id: "select", status: "waiting", attempts: 0, started_at: 1_790_000_010, ended_at: null, duration_s: null, output_ref: "", output: { items: [selected] }, progress: {}, error: "" },
    ] };
    vi.mocked(productionLinesApi.lines).mockResolvedValue({ items: [choiceLine] });
    vi.mocked(productionLinesApi.runs).mockResolvedValue({ items: [run] });
    await mount("/reviews");
    await act(async () => { buttonContaining("Review sample").click(); });
    await settle();
    for (const evidence of ["81", "option-1", "source one, source two", "Not served yet", "Regional soups"]) expect(host.textContent).toContain(evidence);
    await act(async () => { host.querySelector<HTMLInputElement>("input[name='production-choice']")!.click(); });
    await act(async () => { buttonContaining("Continue").click(); });
    await settle();
    expect(productionLinesApi.continue).toHaveBeenCalledWith("run-42", { choice: selected });
  });
});
