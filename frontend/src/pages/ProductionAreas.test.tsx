/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProductionLine, ProductionRun, ProductionStageItems } from "../api/productionLinesClient";

const mocks = vi.hoisted(() => ({
  lines: vi.fn(), runs: vi.fn(), operations: vi.fn(), run: vi.fn(), start: vi.fn(), updateLine: vi.fn(), continue: vi.fn(), cancel: vi.fn(), retry: vi.fn(), stageItems: vi.fn(), retryFailedItems: vi.fn(), reviewItems: vi.fn(), submitReview: vi.fn(), listIntegrations: vi.fn(), navigate: vi.fn(), toast: vi.fn(),
}));
vi.mock("../api/productionLinesClient", () => ({ productionLinesApi: {
  lines: mocks.lines, runs: mocks.runs, operations: mocks.operations, run: mocks.run, start: mocks.start, updateLine: mocks.updateLine, continue: mocks.continue, cancel: mocks.cancel, retry: mocks.retry, stageItems: mocks.stageItems, retryFailedItems: mocks.retryFailedItems, reviewItems: mocks.reviewItems, submitReview: mocks.submitReview,
} }));
vi.mock("../api/integrationManagementClient", () => ({ integrationManagementApi: { list: mocks.listIntegrations } }));
vi.mock("../router", () => ({ navigate: mocks.navigate }));
vi.mock("../context/ProjectContext", () => ({ useProject: () => ({ projectId: "project-1", projects: [{ project_id: "project-1", name: "Default Project" }] }) }));
vi.mock("../components/ui", async (importOriginal) => ({ ...(await importOriginal<typeof import("../components/ui")>()), useToast: () => ({ addToast: mocks.toast }) }));

const now = Math.floor(Date.now() / 1000);
const image = { media_id: "image-1", name: "Preview.png", content_type: "image/png", kind: "preview" as const, url: "https://media.example.test/preview.png" };
const line: ProductionLine = { id: "line-demo", name: "Demo line", project_id: "project-1", enabled: true, updated_at: now, definition: { stages: [
  { id: "draft", kind: "operation", contract: "lynewright.demo.writer", operation: "write" },
  { id: "review", kind: "gate:review", label: "Review draft" },
] } };
function makeRun(id: string, status: string = "waiting", kind: "review" | "choice" = "review"): ProductionRun {
  const choices = [{ id: "approve", label: "Approve", description: "Continue the line." }, { id: "revise", label: "Revise", description: "Return to editing." }];
  return { id, line_id: line.id, project_id: "project-1", status, current_stage: status === "waiting" ? "review" : "draft", context: { vars: {} }, created_at: now - 120, updated_at: now - 30,
    stages: [{ stage_id: "draft", status: "done", attempts: 1, started_at: now - 120, ended_at: now - 60, duration_s: 60, output_ref: "", output: { text: "A short draft." }, progress: {}, error: "" },
      { stage_id: "review", status: status === "waiting" ? "waiting" : "done", attempts: 1, started_at: now - 30, ended_at: null, duration_s: null, output_ref: "", output: kind === "choice" ? { items: choices } : { items: [{ id: "draft-item", title: "Draft title", text: "A short review text.", description: "A sample to review.", label: "Draft item", caption: "A sample to review.", media_previews: [image] }] }, progress: {}, error: "" }],
    timeline: [{ id: 1, type: "run.started", stage_id: "", detail: {}, actor_id: "admin@example.test", created_at: now - 120 }] };
}
const waitingRun = makeRun("run-waiting");
const choiceRun = makeRun("run-choice", "waiting", "choice");
const itemLine: ProductionLine = { ...line, definition: { stages: [
  { id: "batch", kind: "for_each", label: "Repeat operation" },
  { id: "shape", kind: "map", label: "Shape output" },
] } };
const itemRun: ProductionRun = { ...makeRun("run-items", "done_with_errors"), current_stage: "", stages: [
  { stage_id: "batch", status: "done_with_errors", attempts: 1, started_at: now - 120, ended_at: now - 20, duration_s: 100, output_ref: "", output: { total: 3, succeeded: 1, failed: 1, skipped: 1, results_ref: "run-items:batch" }, progress: {}, error: "" },
] };
const stageItems: ProductionStageItems = { total: 3, succeeded: 1, failed: 1, skipped: 1, results_ref: "run-items:batch", items: [
  { item_index: 0, status: "succeeded", input: { value: "first" }, output: { value: "Result" }, error: "", attempts: 1, started_at: now - 120, ended_at: now - 80 },
  { item_index: 1, status: "failed", input: { value: "second" }, output: null, error: "Temporary failure", attempts: 1, started_at: now - 90, ended_at: now - 50 },
] };
const integrations = { items: [{ integration: { id: "lynewright.integration.demo-writer", name: "Demo Writer" }, availability: { runner_status: "connected" }, management: { sections: [] } }] } as never;

describe("U2 production areas", () => {
  let host: HTMLDivElement; let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
    window.history.replaceState({}, "", "/lines");
    mocks.lines.mockResolvedValue({ items: [line] }); mocks.runs.mockResolvedValue({ items: [waitingRun] }); mocks.operations.mockResolvedValue({ items: [{ operation_id: "op-1", integration_id: "lynewright.integration.demo-writer", kind: "native", contract: "lynewright.demo.writer", operation: "write", description: "Write a draft", schema: { type: "object", properties: {} } }] });
    mocks.run.mockResolvedValue(waitingRun); mocks.start.mockResolvedValue(waitingRun); mocks.updateLine.mockImplementation(async (_id: string, value: Partial<ProductionLine>) => ({ ...line, ...value }));
    mocks.continue.mockResolvedValue({ ...waitingRun, status: "done" }); mocks.cancel.mockResolvedValue({ ...waitingRun, status: "cancelled" }); mocks.retry.mockResolvedValue(waitingRun); mocks.stageItems.mockResolvedValue(stageItems); mocks.retryFailedItems.mockResolvedValue({ ...itemRun, status: "running", current_stage: "batch" }); mocks.reviewItems.mockResolvedValue({ items: [] }); mocks.submitReview.mockResolvedValue({ result: {}, run: { ...waitingRun, status: "done" } }); mocks.listIntegrations.mockResolvedValue(integrations);
    mocks.navigate.mockReset(); mocks.toast.mockReset();
  });
  afterEach(() => { act(() => root.unmount()); document.body.innerHTML = ""; vi.useRealTimers(); vi.restoreAllMocks(); });
  async function render(node: React.ReactNode) { await act(async () => { root.render(node); await Promise.resolve(); await Promise.resolve(); }); }
  function click(element: Element | null) { if (!element) throw new Error("Missing click target"); act(() => element.dispatchEvent(new MouseEvent("click", { bubbles: true }))); }
  function change(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null, value: string) { if (!element) throw new Error("Missing field"); act(() => { const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value")?.set; setter?.call(element, value); element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", { bubbles: true })); }); }

  it("shows the lines table and opens line detail from a row click", async () => {
    const { LinesPage } = await import("./ProductionAreas"); await render(<LinesPage projectId="project-1" projectName="Default Project" />);
    expect(host.querySelector('[aria-label="Production lines"]')).not.toBeNull(); expect(host.textContent).toContain("Demo line"); expect(host.textContent).toContain("Review draft");
    click(host.querySelector("tbody tr")); expect(mocks.navigate).toHaveBeenCalledWith("/lines/line-demo");
  });

  it("shows line Runs and Settings tabs, hides Trends, and starts from the dialog", async () => {
    window.history.replaceState({}, "", "/lines/line-demo");
    const { LineDetailPage } = await import("./ProductionAreas"); await render(<LineDetailPage lineId="line-demo" projectId="project-1" projectName="Default Project" />);
    expect(host.textContent).toContain("Demo Writer"); expect(host.textContent).toContain("Runs"); expect(host.textContent).not.toContain("Trends");
    click(Array.from(host.querySelectorAll('[role="tab"]')).find((tab) => tab.textContent === "Settings") ?? null);
    expect(host.querySelector('[aria-label="Line settings"]')).not.toBeNull(); expect(mocks.navigate).toHaveBeenCalledWith("/lines/line-demo?tab=settings");
    click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("Start run")) ?? null);
    expect(host.querySelector('[role="dialog"]')).not.toBeNull();
    change(host.querySelector("#line-var-name-0"), "topic"); change(host.querySelector("#line-var-value-0"), "U2 walkthrough");
    click(Array.from(host.querySelectorAll('[role="dialog"] button')).find((button) => button.textContent?.includes("Start run")) ?? null);
    await act(async () => { await Promise.resolve(); });
    expect(mocks.start).toHaveBeenCalledWith("line-demo", "project-1", { topic: "U2 walkthrough" });
  });

  it("saves the per-line assistant review policy in line Settings", async () => {
    window.history.replaceState({}, "", "/lines/line-demo");
    const { LineDetailPage } = await import("./ProductionAreas"); await render(<LineDetailPage lineId="line-demo" projectId="project-1" projectName="Default Project" />);
    click(Array.from(host.querySelectorAll('[role="tab"]')).find((tab) => tab.textContent === "Settings") ?? null);
    change(host.querySelector<HTMLSelectElement>("#line-review-access"), "dashboard_or_assistant");
    await act(async () => { await Promise.resolve(); });
    expect(mocks.updateLine).toHaveBeenCalledWith("line-demo", { definition: expect.objectContaining({ review_by: "dashboard_or_assistant" }) });
  });

  it("marks per-item stages with ×N and gives map stages a shape icon", async () => {
    mocks.lines.mockResolvedValue({ items: [itemLine] });
    window.history.replaceState({}, "", "/lines/line-demo");
    const { LineDetailPage } = await import("./ProductionAreas"); await render(<LineDetailPage lineId="line-demo" projectId="project-1" projectName="Default Project" />);
    expect(host.querySelector('[data-stage-kind="for_each"]')?.textContent).toContain("×N");
    expect(host.querySelector('[data-stage-kind="map"] [data-stage-icon="shape"] svg')).not.toBeNull();
  });

  it("filters runs and pauses active polling when the tab is hidden", async () => {
    vi.useFakeTimers();
    let hidden = false; vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
    const { RunsPage } = await import("./ProductionAreas"); await render(<RunsPage projectId="project-1" projectName="Default Project" />);
    expect(host.querySelector('[aria-label="Production runs"]')).not.toBeNull();
    change(host.querySelector("#run-status-filter"), "waiting"); expect(mocks.navigate).toHaveBeenCalledWith("/runs?status=waiting");
    const initialCalls = mocks.runs.mock.calls.length;
    hidden = true; act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => { await vi.advanceTimersByTimeAsync(5100); });
    expect(mocks.runs.mock.calls.length).toBe(initialCalls);
  });

  it("expands a stage output with formatted JSON in run detail", async () => {
    window.history.replaceState({}, "", "/runs/run-waiting"); mocks.run.mockResolvedValue(waitingRun);
    const { RunDetailPage } = await import("./ProductionAreas"); await render(<RunDetailPage runId="run-waiting" projectId="project-1" projectName="Default Project" />);
    expect(host.textContent).toContain("Timeline"); expect(host.textContent).toContain("Run id");
    const firstStage = host.querySelector("button[aria-expanded]"); if (firstStage?.getAttribute("aria-expanded") !== "true") click(firstStage);
    const json = Array.from(host.querySelectorAll("details")).find((item) => item.textContent?.includes("View formatted JSON")); expect(json?.querySelector("pre")?.textContent).toContain("A short draft");
    click(host.querySelector('[role="tab"][aria-selected="false"]')); expect(host.querySelector('[aria-label="Run output"]')).not.toBeNull();
  });

  it("shows item status, input, output links, progress, filtering, and failed-item retry", async () => {
    window.history.replaceState({}, "", "/runs/run-items?tab=items");
    mocks.run.mockResolvedValue(itemRun); mocks.lines.mockResolvedValue({ items: [itemLine] });
    const { RunDetailPage } = await import("./ProductionAreas"); await render(<RunDetailPage runId="run-items" projectId="project-1" projectName="Default Project" />);
    expect(host.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe("Items");
    expect(host.querySelector('progress[aria-label="Repeat operation item progress"]')?.getAttribute("max")).toBe("3");
    expect(host.querySelector('[aria-label="Items for Repeat operation"]')).not.toBeNull();
    expect(host.textContent).toContain("Temporary failure"); expect(host.querySelector('a[href="#item-output-0"]')?.textContent).toBe("View output");
    change(host.querySelector("#items-status-filter"), "failed");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mocks.stageItems).toHaveBeenLastCalledWith("run-items", "batch", "failed");
    click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Retry failed items") ?? null);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mocks.retryFailedItems).toHaveBeenCalledWith("run-items", "batch");
  });

  it("renders media_refs in an expanded run stage as an image preview", async () => {
    const mediaRun: ProductionRun = { ...waitingRun, stages: waitingRun.stages.map((stage) => stage.stage_id === "draft" ? { ...stage, output: { media_refs: [{ url: "https://media.example.test/generated.png", name: "Generated PNG", content_type: "image/png" }] } } : stage) };
    mocks.run.mockResolvedValue(mediaRun);
    const { RunDetailPage } = await import("./ProductionAreas"); await render(<RunDetailPage runId="run-waiting" projectId="project-1" projectName="Default Project" />);
    expect(host.querySelector('img[src="https://media.example.test/generated.png"]')?.getAttribute("alt")).toBe("Generated PNG");
  });

  it("keeps expanded stage content inside its card and opens media thumbnails in a preview dialog", async () => {
    const mediaRun: ProductionRun = { ...waitingRun, stages: waitingRun.stages.map((stage) => stage.stage_id === "draft" ? { ...stage, output: { media_refs: [{ url: "https://media.example.test/generated.png", name: "Generated PNG", content_type: "image/png" }] } } : stage) };
    mocks.run.mockResolvedValue(mediaRun);
    const { RunDetailPage } = await import("./ProductionAreas"); await render(<RunDetailPage runId="run-waiting" projectId="project-1" projectName="Default Project" />);
    const card = host.querySelector("[data-stage-card]"); const content = card?.querySelector("[data-stage-expanded-content]");
    expect(card).not.toBeNull(); expect(content).not.toBeNull(); expect(card?.contains(content ?? null)).toBe(true);
    click(host.querySelector('button[aria-label="Preview Generated PNG"]'));
    expect(host.querySelector('[role="dialog"] img[src="https://media.example.test/generated.png"]')).not.toBeNull();
  });

  it("shows review media previews in the waiting stage on run detail", async () => {
    const { RunDetailPage } = await import("./ProductionAreas"); await render(<RunDetailPage runId="run-waiting" projectId="project-1" projectName="Default Project" />);
    const reviewStage = Array.from(host.querySelectorAll("button[aria-expanded]")).find((button) => button.textContent?.includes("review")); click(reviewStage ?? null);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    expect(host.querySelector('img[src="https://media.example.test/preview.png"]')).not.toBeNull();
  });

  it("uses a two-pane review queue, shows item content, and collects a reason inline before rejection", async () => {
    mocks.runs.mockResolvedValue({ items: [waitingRun, makeRun("run-waiting-2")] });
    const { ReviewsPage } = await import("./ProductionAreas"); await render(<ReviewsPage projectId="project-1" projectName="Default Project" />);
    expect(host.querySelector('[aria-label="Waiting review queue"]')).not.toBeNull(); expect(host.querySelector('[aria-label="Focused review"]')).not.toBeNull(); expect(host.querySelector('img[src="https://media.example.test/preview.png"]')).not.toBeNull();
    expect(host.textContent).toContain("Draft title"); expect(host.textContent).toContain("A short review text."); expect(host.textContent).toContain("A sample to review."); expect(host.textContent).toContain("Waiting since"); expect(host.textContent).toContain("Run");
    expect(host.querySelector("#review-reject-reason")).toBeNull();
    const reject = Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.trim() === "Reject"); click(reject ?? null);
    expect(host.querySelector("#review-reject-reason")).not.toBeNull();
    change(host.querySelector("#review-reject-reason"), "Needs a correction");
    click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent?.includes("Confirm rejection")) ?? null);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mocks.continue).toHaveBeenCalledWith("run-waiting", { approved: false, items: [{ id: "draft-item", decision: "refused", reason: "Needs a correction" }] });
  });

  it("supports j/k and a confirmation before approval", async () => {
    mocks.runs.mockResolvedValue({ items: [waitingRun, makeRun("run-next")] });
    const { ReviewsPage } = await import("./ProductionAreas"); await render(<ReviewsPage projectId="project-1" projectName="Default Project" />);
    const queueButtons = host.querySelectorAll('[aria-label="Waiting review queue"] button'); expect(queueButtons).toHaveLength(2);
    const firstFocused = host.querySelector('[aria-label="Waiting review queue"] [aria-current="true"]');
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "j", bubbles: true })); });
    expect(host.querySelector('[aria-label="Waiting review queue"] [aria-current="true"]')).not.toBe(firstFocused);
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true })); });
    expect(host.querySelector('[role="dialog"]')?.textContent).toContain("Approve this review?");
    click(Array.from(host.querySelectorAll('[role="dialog"] button')).find((button) => button.textContent?.includes("Approve")) ?? null);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(mocks.continue).toHaveBeenCalled();
  });

  it("renders choice gates as radio cards", async () => {
    mocks.runs.mockResolvedValue({ items: [choiceRun] }); mocks.lines.mockResolvedValue({ items: [{ ...line, definition: { stages: [{ id: "review", kind: "gate:choice" }] } }] });
    const { ReviewsPage } = await import("./ProductionAreas"); await render(<ReviewsPage projectId="project-1" projectName="Default Project" />);
    expect(host.querySelector('[role="radiogroup"][aria-label="Choose an option"]')).not.toBeNull();
    click(host.querySelector('input[name="review-choice"]')?.parentElement ?? null); click(Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Continue") ?? null);
    await act(async () => { await Promise.resolve(); }); expect(mocks.continue).toHaveBeenCalledWith("run-choice", { choice: { id: "approve", label: "Approve", description: "Continue the line." } });
  });
});
