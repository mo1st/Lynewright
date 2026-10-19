import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { PageState } from "../components/ui/PageState";
import { PageHeader } from "../components/ui/Layout";
import TypedMediaPreview from "../components/production/TypedMediaPreview";
import {
  productionLinesApi,
  type ProductionLine,
  type ProductionOperation,
  type ProductionReviewItem,
  type ProductionRun,
  type ProductionStage,
} from "../api/productionLinesClient";
import { navigate } from "../router";

type View = "lines" | "runs" | "review";
type Draft = Pick<ProductionLine, "id" | "name" | "project_id" | "enabled" | "definition">;
type RunVariable = { key: string; value: string; kind: "text" | "number" | "boolean" };

const STAGE_KINDS = ["operation", "stepped_operation", "async_operation", "gate:choice", "gate:review", "condition", "notify"];
const ACTIVE_STATUSES = new Set(["queued", "running", "waiting"]);

function routeFor(view: View, values: { runId?: string; lineId?: string } = {}) {
  const path = view === "review" ? "/reviews" : `/${view}`;
  const params = new URLSearchParams();
  if (values.runId) params.set("run_id", values.runId);
  if (values.lineId) params.set("line_id", values.lineId);
  const query = params.toString();
  return `${path}${query ? `?${query}` : ""}`;
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function MediaPreviews({ value }: { value: unknown }) {
  const items = Array.isArray(value) ? value : [];
  return <>{items.map((raw, index) => {
    const media = asObject(raw);
    const url = String(media.url || "");
    if (!url) return null;
    const label = String(media.name || `Media ${index + 1}`);
    return <div className="production-media-preview" key={String(media.media_id || index)}>
      <TypedMediaPreview kind={String(media.kind || "download")} label={label} mime={String(media.content_type || "")} value={url} />
    </div>;
  })}</>;
}

function lineDraft(line?: ProductionLine, projectId = ""): Draft {
  return line ? {
    id: line.id,
    name: line.name,
    project_id: line.project_id,
    enabled: line.enabled,
    definition: JSON.parse(JSON.stringify(line.definition)) as ProductionLine["definition"],
  } : {
    id: "",
    name: "",
    project_id: projectId,
    enabled: true,
    definition: { stages: [] },
  };
}

function statusTone(status: string): "neutral" | "positive" | "attention" | "critical" | "active" {
  if (["done", "completed", "enabled"].includes(status)) return "positive";
  if (["waiting", "queued", "running", "active"].includes(status)) return "active";
  if (["failed", "cancelled", "disabled"].includes(status)) return "critical";
  if (["skipped"].includes(status)) return "attention";
  return "neutral";
}

function readable(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "Available";
}

function dateLabel(seconds: number | null | undefined): string {
  return typeof seconds === "number" ? new Date(seconds * 1000).toLocaleString() : "Not started";
}

function elapsedLabel(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  if (safe < 60) return `${safe}s`;
  if (safe < 3600) return `${Math.floor(safe / 60)}m ${safe % 60}s`;
  return `${Math.floor(safe / 3600)}h ${Math.floor((safe % 3600) / 60)}m`;
}

function runLabel(run: ProductionRun, line?: ProductionLine): string {
  const vars = asObject(run.context.vars);
  const title = vars.title ?? vars.name ?? vars.label;
  return typeof title === "string" && title.trim() ? title : line?.name || "Untitled run";
}

function latestOutputRef(run: ProductionRun): string {
  const stage = [...run.stages].reverse().find((item) => item.output_ref);
  return stage?.output_ref || "";
}

function defaultStage(kind: string, index: number, operation?: ProductionOperation): ProductionStage {
  const id = `stage-${index + 1}`;
  if (["operation", "stepped_operation", "async_operation"].includes(kind)) {
    return { id, kind, operation_id: operation?.operation_id || "", contract: operation?.contract || "", operation: operation?.operation || "", args_template: {} };
  }
  if (kind === "gate:choice") return { id, kind, options_template: [] };
  if (kind === "gate:review") return { id, kind, items_template: [] };
  if (kind === "condition") return { id, kind, when: { path: "run.vars.value", truthy: true, action: "skip" } };
  if (kind === "notify") return { id, kind, message_template: "" };
  return { id, kind };
}

function stageOperation(stage: ProductionStage, operations: ProductionOperation[]): ProductionOperation | undefined {
  return operations.find((item) => item.operation_id === stage.operation_id)
    || operations.find((item) => item.contract === stage.contract && item.operation === stage.operation);
}

function schemaAtPath(schema: unknown, path: string[]): Record<string, unknown> | undefined {
  let current = asObject(schema);
  for (const part of path) {
    if (/^\d+$/.test(part) && current.items) current = asObject(current.items);
    else current = asObject(asObject(current.properties)[part]);
    if (!Object.keys(current).length) return undefined;
  }
  return current;
}

function sourceItemSchema(
  template: unknown,
  stages: ProductionStage[],
  operations: ProductionOperation[],
): Record<string, unknown> | undefined {
  if (typeof template !== "string") return undefined;
  const reference = template.match(/^\{\{\s*(stages\..+?)\s*\}\}$/)?.[1];
  if (!reference) return undefined;
  const source = [...stages].sort((left, right) => right.id.length - left.id.length).find((item) =>
    reference === `stages.${item.id}.items` || reference.startsWith(`stages.${item.id}.output`),
  );
  if (!source) return undefined;

  let schema: Record<string, unknown> | undefined;
  if (source.kind === "for_each" && reference === `stages.${source.id}.items`) {
    return stageOperation(source, operations)?.output_schema;
  } else if (source.kind === "map") {
    const outputPath = reference.slice(`stages.${source.id}.output`.length).replace(/^\./, "").split(".").filter(Boolean);
    if (outputPath[0] !== "items") return undefined;
    const mappedProperties: Record<string, unknown> = {};
    const fields = asObject(source.fields);
    const sourceSchema = sourceItemSchema(source.items, stages.filter((item) => item.id !== source.id), operations);
    for (const [name, value] of Object.entries(fields)) {
      if (typeof value === "string") {
        const itemPath = value.match(/^\{\{\s*item(?:\.([\w.-]+))?\s*\}\}$/)?.[1];
        if (itemPath) mappedProperties[name] = schemaAtPath(sourceSchema, itemPath.split("."));
      } else {
        const type = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
        mappedProperties[name] = { type };
      }
    }
    const output = { type: "object", properties: { items: { type: "array", items: { type: "object", properties: mappedProperties } } } };
    schema = schemaAtPath(output, outputPath);
  } else {
    schema = stageOperation(source, operations)?.output_schema;
    const outputPath = reference.slice(`stages.${source.id}.output`.length).replace(/^\./, "").split(".").filter(Boolean);
    schema = schemaAtPath(schema, outputPath);
  }
  if (schema?.type === "array") return asObject(schema.items);
  return undefined;
}

function itemSchemaWarnings(stage: ProductionStage, stages: ProductionStage[], operations: ProductionOperation[]): string[] {
  if (stage.kind !== "for_each") return [];
  const itemSchema = sourceItemSchema(stage.items, stages, operations);
  const properties = asObject(itemSchema?.properties);
  if (!itemSchema || (!Object.keys(properties).length && itemSchema.additionalProperties !== false)) return [];
  const operation = stageOperation(stage, operations);
  if (!operation?.schema) return [];
  const args = asObject(stage.input ?? stage.args_template);
  const warnings: string[] = [];
  for (const [argument, value] of Object.entries(args)) {
    const template = typeof value === "string" ? value.match(/^\{\{\s*item\.([\w.-]+)\s*\}\}$/)?.[1] : undefined;
    if (template && !schemaAtPath(itemSchema, template.split("."))) {
      warnings.push(`Source output does not declare item field “${template}” used by input “${argument}”.`);
    }
  }
  return warnings;
}

function OperationArguments({
  operation,
  stage,
  onChange,
}: {
  operation?: ProductionOperation;
  stage: ProductionStage;
  onChange: (stage: ProductionStage) => void;
}) {
  const schema = operation?.schema;
  const args = asObject(stage.args_template);
  const fields = Object.entries(schema?.properties || {});
  if (!operation) return <p className="production-muted">Choose a declared operation to configure its inputs.</p>;
  if (!fields.length) return <p className="production-muted">This operation has no declared inputs.</p>;
  const required = new Set(schema?.required || []);
  const writeValue = (key: string, value: unknown, empty: boolean) => {
    const next = { ...args };
    if (empty) delete next[key];
    else next[key] = value;
    onChange({ ...stage, args_template: next });
  };

  return <div className="production-argument-grid">
    {fields.map(([key, rawSchema]) => {
      const field = asObject(rawSchema);
      const value = args[key];
      const label = `${readable(field.title) || key}${required.has(key) ? " (required)" : ""}`;
      const type = Array.isArray(field.type) ? String(field.type[0]) : String(field.type || "string");
      const options = Array.isArray(field.enum) ? field.enum : null;
      if (options) return <label key={key}>{label}<select aria-label={label} value={value == null ? "" : String(value)} onChange={(event) => {
        const selected = options.find((item) => String(item) === event.target.value);
        writeValue(key, selected, event.target.value === "");
      }}><option value="">Choose…</option>{options.map((item) => <option key={String(item)} value={String(item)}>{String(item)}</option>)}</select></label>;
      if (type === "boolean") return <label className="production-check-field" key={key}><input type="checkbox" checked={Boolean(value)} onChange={(event) => writeValue(key, event.target.checked, false)} />{label}</label>;
      if (type === "object" || type === "array") return <details className="production-advanced-field" key={key}><summary>{label} · Advanced</summary><textarea aria-label={`${label} JSON`} rows={4} value={value == null ? "" : JSON.stringify(value, null, 2)} placeholder={type === "array" ? "[]" : "{}"} onChange={(event) => {
        if (!event.target.value.trim()) return writeValue(key, undefined, true);
        try { writeValue(key, JSON.parse(event.target.value), false); } catch { /* keep the last valid value until the JSON parses */ }
      }} /></details>;
      return <label key={key}>{label}<input
        aria-label={label}
        type={type === "integer" || type === "number" ? "number" : "text"}
        step={type === "integer" ? 1 : "any"}
        value={value == null ? "" : String(value)}
        placeholder={type === "string" ? "Value or {{run.vars.field}}" : ""}
        onChange={(event) => {
          const input = event.target.value;
          if (!input.trim()) return writeValue(key, undefined, true);
          const next = type === "integer" || type === "number" ? Number(input) : input;
          writeValue(key, next, false);
        }}
      /></label>;
    })}
  </div>;
}

function AdvancedDefinition({ draft, onChange }: { draft: Draft; onChange: (next: Draft) => void }) {
  const [text, setText] = useState(() => JSON.stringify(draft.definition, null, 2));
  const [error, setError] = useState("");
  useEffect(() => {
    setText(JSON.stringify(draft.definition, null, 2));
    setError("");
  }, [draft.definition]);
  return <details className="production-advanced-field">
    <summary>Advanced line definition</summary>
    <label>Definition JSON<textarea rows={12} value={text} onChange={(event) => {
      setText(event.target.value);
      try {
        const parsed = JSON.parse(event.target.value);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || !Array.isArray(parsed.stages)) throw new Error("Definition needs a stages array.");
        setError("");
        onChange({ ...draft, definition: parsed });
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Enter valid definition JSON.");
      }
    }} /><small>Use this for fields that the schema guided builder does not expose.</small></label>
    {error ? <p className="production-error" role="alert">{error}</p> : null}
  </details>;
}

function LineEditor({
  draft,
  operations,
  busy,
  onDraftChange,
  onSave,
  onClose,
}: {
  draft: Draft;
  operations: ProductionOperation[];
  busy: boolean;
  onDraftChange: (draft: Draft) => void;
  onSave: () => void;
  onClose: () => void;
}) {
  const changeStage = (index: number, update: (stage: ProductionStage) => ProductionStage) => {
    const stages = [...draft.definition.stages];
    stages[index] = update(stages[index]);
    onDraftChange({ ...draft, definition: { ...draft.definition, stages } });
  };
  const moveStage = (index: number, offset: number) => {
    const stages = [...draft.definition.stages];
    const target = index + offset;
    if (target < 0 || target >= stages.length) return;
    [stages[index], stages[target]] = [stages[target], stages[index]];
    onDraftChange({ ...draft, definition: { ...draft.definition, stages } });
  };
  const addStage = () => {
    const kind = "operation";
    onDraftChange({ ...draft, definition: { ...draft.definition, stages: [...draft.definition.stages, defaultStage(kind, draft.definition.stages.length, operations[0])] } });
  };

  return <section className="production-editor" aria-labelledby="production-editor-title">
    <div className="production-section-heading"><p className="production-eyebrow">Line builder</p><Button variant="ghost" onClick={onClose}>Close</Button></div>
    <div className="production-form-grid">
      <label>Line id<input value={draft.id} disabled={Boolean(draft.id)} onChange={(event) => onDraftChange({ ...draft, id: event.target.value })} placeholder="team.process" required /></label>
      <label>Name<input value={draft.name} onChange={(event) => onDraftChange({ ...draft, name: event.target.value })} required /></label>
      <label className="production-check-field"><input type="checkbox" checked={draft.enabled} onChange={(event) => onDraftChange({ ...draft, enabled: event.target.checked })} />Enabled</label>
    </div>
    <div className="production-section-heading production-stages-heading"><div><h3>Stages</h3><p>Compose the line from declared operations and generic gates.</p></div><Button variant="secondary" onClick={addStage}>Add stage</Button></div>
    {!draft.definition.stages.length ? <p className="production-muted">Add a stage to define what this line runs.</p> : null}
    <div className="production-stage-editor-list">{draft.definition.stages.map((stage, index) => {
      const selectedOperation = operations.find((item) => item.operation_id === stage.operation_id)
        || operations.find((item) => item.contract === stage.contract && item.operation === stage.operation);
      return <article className="production-stage-editor" key={`${stage.id}-${index}`}>
        <div className="production-section-heading"><h4>Stage {index + 1}</h4><div className="production-inline-actions">
          <Button variant="ghost" aria-label={`Move stage ${index + 1} up`} disabled={index === 0} onClick={() => moveStage(index, -1)}><ChevronUp size={16} aria-hidden="true" /></Button>
          <Button variant="ghost" aria-label={`Move stage ${index + 1} down`} disabled={index === draft.definition.stages.length - 1} onClick={() => moveStage(index, 1)}><ChevronDown size={16} aria-hidden="true" /></Button>
          <Button variant="ghost" onClick={() => onDraftChange({ ...draft, definition: { ...draft.definition, stages: draft.definition.stages.filter((_, itemIndex) => itemIndex !== index) } })}>Remove</Button>
        </div></div>
        <div className="production-form-grid">
          <label>Stage name<input value={String(stage.label ?? "")} onChange={(event) => changeStage(index, (current) => ({ ...current, label: event.target.value }))} placeholder={stage.id || `Stage ${index + 1}`} /></label>
          <label>Stage id<input value={stage.id} onChange={(event) => changeStage(index, (current) => ({ ...current, id: event.target.value }))} required /></label>
          <label>Stage type<select value={stage.kind} onChange={(event) => changeStage(index, () => defaultStage(event.target.value, index, operations[0]))}>{STAGE_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}</select></label>
        </div>
        {["operation", "stepped_operation", "async_operation"].includes(stage.kind) ? <>
          <label>Declared operation<select aria-label={`Operation for ${stage.id}`} value={selectedOperation?.operation_id || ""} onChange={(event) => {
            const operation = operations.find((item) => item.operation_id === event.target.value);
            changeStage(index, (current) => ({ ...current, operation_id: operation?.operation_id || "", contract: operation?.contract || "", operation: operation?.operation || "", args_template: {} }));
          }}><option value="">Choose an operation…</option>{operations.map((item) => <option key={item.operation_id} value={item.operation_id}>{item.kind.toUpperCase()} · {item.contract} · {item.operation}</option>)}</select></label>
          {selectedOperation ? <Badge tone="neutral">{selectedOperation.kind.toUpperCase()}</Badge> : null}
          {selectedOperation?.description ? <p className="production-muted">{selectedOperation.description}</p> : null}
          <OperationArguments operation={selectedOperation} stage={stage} onChange={(next) => changeStage(index, () => next)} />
        </> : null}
        {stage.kind === "notify" ? <label>Message<textarea rows={3} value={String(stage.message_template ?? "")} onChange={(event) => changeStage(index, (current) => ({ ...current, message_template: event.target.value }))} placeholder="Message or {{run.vars.field}}" /></label> : null}
        {stage.kind === "condition" ? <div className="production-form-grid"><label>Context path<input value={String(asObject(stage.when).path ?? "")} onChange={(event) => changeStage(index, (current) => ({ ...current, when: { ...asObject(current.when), path: event.target.value } }))} placeholder="run.vars.ready" /></label><label>When unmatched<select value={String(asObject(stage.when).action ?? "skip")} onChange={(event) => changeStage(index, (current) => ({ ...current, when: { ...asObject(current.when), action: event.target.value } }))}><option value="skip">Skip this stage</option><option value="stop">Stop the line</option></select></label></div> : null}
        {["gate:choice", "gate:review"].includes(stage.kind) ? <p className="production-muted">{stage.kind === "gate:choice" ? "The choice gate reads its options from the line context." : "The review gate reads its items from the line context."} Configure the template in Advanced line definition when needed.</p> : null}
        {itemSchemaWarnings(stage, draft.definition.stages.slice(0, index), operations).map((warning) => <p className="production-muted" role="status" key={warning}><Badge tone="attention">Schema warning</Badge> {warning}</p>)}
      </article>;
    })}</div>
    <AdvancedDefinition draft={draft} onChange={onDraftChange} />
    <div className="production-inline-actions"><Button onClick={onSave} busy={busy} disabled={!draft.id.trim() || !draft.name.trim() || !draft.definition.stages.length}>Save line</Button><Button variant="secondary" onClick={onClose}>Cancel</Button></div>
  </section>;
}

function RunCard({ run, line, stages, onOpen }: { run: ProductionRun; line?: ProductionLine; stages: ProductionStage[]; onOpen: () => void }) {
  const stage = stages.find((item) => item.id === run.current_stage);
  const current = run.stages.find((item) => item.stage_id === run.current_stage);
  const progress = asObject(current?.progress);
  const stageName = String(stage?.label || stage?.id || run.current_stage || run.status);
  const progressLabel = readable(progress.label);
  const progressStep = typeof progress.step === "number" ? progress.step : null;
  const progressTotal = typeof progress.total === "number" ? progress.total : null;
  const elapsed = run.updated_at - run.created_at;
  return <Button variant="ghost" type="button" className="production-run-card" onClick={onOpen}>
    <span className="production-run-card__top"><strong>{runLabel(run, line)}</strong><Badge tone={statusTone(run.status)}>{run.status}</Badge></span>
    <span>{line?.name || run.line_id}</span>
    <span className="production-run-card__meta">{stageName} <span aria-hidden="true">·</span> {elapsedLabel(elapsed)}</span>
    {progressLabel ? <span className="production-progress">{progressLabel}{progressStep !== null && progressTotal !== null ? ` ${progressStep} / ${progressTotal}` : ""}</span> : null}
  </Button>;
}

function RunDetail({
  run,
  line,
  operations = [],
  busy,
  onAction,
  onRunAction,
  onReviewSubmit,
}: {
  run: ProductionRun;
  line?: ProductionLine;
  operations?: ProductionOperation[];
  busy: boolean;
  onAction: (action: "retry" | "continue" | "cancel", value?: Record<string, unknown>) => void;
  onRunAction?: (stageId: string, actionIndex: number, inputs: Record<string, unknown>) => void;
  onReviewSubmit?: (items: Array<{ id: string; decision: "approved" | "refused"; reason: string }>) => void;
}) {
  const currentStage = line?.definition.stages.find((item) => item.id === run.current_stage);
  const currentRunStage = run.stages.find((item) => item.stage_id === run.current_stage);
  const gateItems = Array.isArray(currentRunStage?.output.items) ? currentRunStage.output.items : [];
  const [decisions, setDecisions] = useState<Record<number, { decision: string; reason: string }>>({});
  const [choice, setChoice] = useState("");
  const [advancedValue, setAdvancedValue] = useState("{}");
  const [selectedAction, setSelectedAction] = useState("");
  const [actionInputs, setActionInputs] = useState<Record<string, string>>({});
  const [operationReviewItems, setOperationReviewItems] = useState<ProductionReviewItem[]>([]);
  const [operationReviewError, setOperationReviewError] = useState("");
  const [operationDecisions, setOperationDecisions] = useState<Record<string, { decision: "approved" | "refused"; reason: string }>>({});
  const isReview = currentStage?.kind === "gate:review";
  const isChoice = currentStage?.kind === "gate:choice";
  const operationWaiting = asObject(currentRunStage?.output.waiting);
  const isOperationReview = run.status === "waiting" && operationWaiting.kind === "review";
  const canContinueReview = gateItems.every((_, index) => decisions[index]?.decision && (decisions[index].decision !== "refused" || decisions[index].reason.trim()));
  const retryable = run.stages.find((item) => item.status === "failed");
  // A step's own run actions only make sense once that
  // step has finished, and a failed run needs exactly one thing: Retry.
  const finishedStages = new Set(run.stages.filter((item) => item.status === "done").map((item) => item.stage_id));
  const actions = run.status === "failed" ? [] : (line?.definition.stages || []).flatMap((stage) => {
    if (!stage.contract || !stage.operation || !finishedStages.has(stage.id)) return [];
    const declared = operations.find((item) => item.contract === stage.contract && item.operation === stage.operation);
    return (declared?.run_actions || []).map((action, actionIndex) => ({ stageId: stage.id, action, actionIndex, key: `${stage.id}:${actionIndex}` }));
  });

  useEffect(() => {
    setDecisions({});
    setChoice("");
    setAdvancedValue("{}");
  }, [run.id, run.current_stage]);

  useEffect(() => {
    let current = true;
    setOperationReviewItems([]);
    setOperationReviewError("");
    setOperationDecisions({});
    if (!isOperationReview) return () => { current = false; };
    productionLinesApi.reviewItems(run.id).then((response) => {
      if (current) setOperationReviewItems(response.items);
    }).catch((error) => {
      if (current) setOperationReviewError(error instanceof Error ? error.message : "Review images could not be loaded.");
    });
    return () => { current = false; };
  }, [run.id, run.current_stage, isOperationReview]);

  return <section className="production-run-detail" aria-labelledby="production-run-detail-title">
    <div className="production-section-heading"><div><p className="production-eyebrow">Run detail</p><h2 id="production-run-detail-title">{runLabel(run, line)}</h2><p>{line?.name || run.line_id} · started {dateLabel(run.created_at)}</p></div><Badge tone={statusTone(run.status)}>{run.status}</Badge></div>
    <div className="production-detail-grid">
      <section><h3>Stage timeline</h3>{run.stages.length ? <ol className="production-timeline">{run.stages.map((item) => {
        const definition = line?.definition.stages.find((stage) => stage.id === item.stage_id);
        return <li key={item.stage_id}>
          <div className="production-section-heading"><strong>{String(definition?.label || definition?.id || item.stage_id)}</strong><Badge tone={statusTone(item.status)}>{item.status}</Badge></div>
          <p>{definition?.kind || "Stage"} · {item.attempts} attempt{item.attempts === 1 ? "" : "s"} · {elapsedLabel(item.duration_s ?? (item.ended_at ? item.ended_at - (item.started_at || item.ended_at) : Math.max(0, run.updated_at - (item.started_at || run.updated_at))))}</p>
          {readable(asObject(item.progress).label) ? <p className="production-progress">{readable(asObject(item.progress).label)}{typeof asObject(item.progress).step === "number" && typeof asObject(item.progress).total === "number" ? ` ${asObject(item.progress).step} / ${asObject(item.progress).total}` : ""}{readable(asObject(item.progress).detail) ? ` · ${readable(asObject(item.progress).detail)}` : ""}</p> : null}
          {item.error ? <p className="production-error">{item.error}</p> : null}
          {item.output_ref ? <p>Output reference: <code>{item.output_ref}</code></p> : null}
          <OutputSummary output={item.output} />
        </li>;
      })}</ol> : <p className="production-muted">No stages have started.</p>}</section>
      <section><h3>Run events</h3>{run.timeline.length ? <ol className="production-events">{run.timeline.map((event) => { const eventResult = asObject(event.detail.result); const eventProgress = asObject(eventResult.progress ?? event.detail.progress); return <li key={event.id}><strong>{event.type.replace(/[._]/g, " ")}</strong><time>{dateLabel(event.created_at)}</time>{typeof event.detail.message === "string" ? <p>{event.detail.message}</p> : null}{readable(eventProgress.label) ? <p className="production-progress">{readable(eventProgress.label)}{typeof eventProgress.step === "number" && typeof eventProgress.total === "number" ? ` ${eventProgress.step} / ${eventProgress.total}` : ""}{readable(eventProgress.detail) ? ` · ${readable(eventProgress.detail)}` : ""}</p> : null}{typeof event.detail.error === "string" ? <p className="production-error">{event.detail.error}</p> : null}</li>; })}</ol> : <p className="production-muted">No events recorded.</p>}</section>
    </div>
    {run.status === "waiting" && currentStage && !isOperationReview ? <section className="production-gate" aria-labelledby="production-gate-title"><h3 id="production-gate-title">{isReview ? "Review items" : isChoice ? "Choose an option" : "Continue run"}</h3>
      {isReview ? <>
        {gateItems.length ? <div className="production-review-list">{gateItems.map((raw, index) => {
          const item = asObject(raw);
          const decision = decisions[index];
          const label = readable(item.label ?? item.title ?? item.name ?? item.id) || `Item ${index + 1}`;
          const caption = readable(item.caption ?? item.description);
          return <article key={String(item.id ?? index)}><strong>{label}</strong><MediaPreviews value={item.media_previews} />{caption ? <p>{caption}</p> : null}{typeof item.ref === "string" ? <p className="production-reference">Reference: {item.ref}</p> : null}<div className="production-inline-actions"><Button variant={decision?.decision === "approved" ? "primary" : "secondary"} aria-pressed={decision?.decision === "approved"} onClick={() => setDecisions((current) => ({ ...current, [index]: { decision: "approved", reason: "" } }))}>Approve</Button><Button variant={decision?.decision === "refused" ? "danger" : "secondary"} aria-pressed={decision?.decision === "refused"} onClick={() => setDecisions((current) => ({ ...current, [index]: { decision: "refused", reason: current[index]?.reason || "" } }))}>Refuse</Button></div>{decision?.decision === "refused" ? <label>Reason<input required value={decision.reason} onChange={(event) => setDecisions((current) => ({ ...current, [index]: { ...current[index], reason: event.target.value } }))} /></label> : null}</article>;
        })}</div> : <p className="production-muted">This gate has no items. Continue to clear it.</p>}
        <Button disabled={!canContinueReview} busy={busy} onClick={() => onAction("continue", { approved: gateItems.every((_, index) => decisions[index]?.decision === "approved"), items: gateItems.map((raw, index) => ({ id: asObject(raw).id ?? index, decision: decisions[index]?.decision || "approved", reason: decisions[index]?.reason || "" })) })}>Submit review</Button>
      </> : isChoice ? <>
        {gateItems.length ? <div className="production-choice-list">{gateItems.map((raw, index) => { const item = asObject(raw); const id = readable(item.id ?? item.value ?? item.key) || String(index); const evidence = Object.entries(item).filter(([key, value]) => !["id", "value", "key", "label", "title", "name", "description"].includes(key) && value != null); return <label key={id} className="production-choice-item"><input type="radio" name="production-choice" value={index} checked={choice === String(index)} onChange={() => setChoice(String(index))} /><span><strong>{readable(item.label ?? item.title ?? item.name) || id}</strong>{item.description ? <small>{readable(item.description)}</small> : null}{evidence.length ? <small className="production-choice-evidence">{evidence.map(([key, value]) => <span key={key}><strong>{key.replace(/[_-]/g, " ")}: </strong>{Array.isArray(value) ? value.map((entry) => readable(entry) || JSON.stringify(entry)).join(", ") : typeof value === "object" ? JSON.stringify(value) : readable(value)}</span>)}</small> : null}</span></label>; })}</div> : <p className="production-muted">This gate has no options. Continue to clear it.</p>}
        {(() => { const report = asObject(run.stages.find((item) => asObject(item.output).unserved != null)?.output); const unserved = report.unserved ?? report.unserved_detail; return Array.isArray(unserved) && unserved.length ? <section className="production-not-served"><h4>Not served yet</h4><ul>{unserved.map((item, index) => <li key={index}>{typeof item === "object" ? JSON.stringify(item) : readable(item)}</li>)}</ul></section> : null; })()}
        <Button disabled={gateItems.length > 0 && !choice} busy={busy} onClick={() => { const index = Number(choice); const selected = gateItems[index]; onAction("continue", { choice: selected }); }}>Continue</Button>
      </> : <>
        <p className="production-muted">This stage is waiting for an input.</p><details className="production-advanced-field"><summary>Advanced continuation data</summary><label>Value JSON<textarea rows={5} value={advancedValue} onChange={(event) => setAdvancedValue(event.target.value)} /></label></details><Button busy={busy} onClick={() => { try { const parsed = JSON.parse(advancedValue); if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) onAction("continue", parsed); } catch { /* invalid input stays visible to fix */ } }}>Continue</Button>
      </>}
    </section> : null}
    {isOperationReview ? <section className="production-gate production-operation-review" aria-label="Operation review"><h3>Review items</h3><p className="production-muted">Review each item against the details it provides. Refusals need a reason so the integration can respond.</p>{operationReviewError ? <p role="alert" className="production-error">{operationReviewError}</p> : null}{operationReviewItems.length ? <div className="production-review-list">{operationReviewItems.map((item) => { const decision = operationDecisions[item.id]; return <article key={item.id}><strong>{item.label}</strong><MediaPreviews value={item.media_previews} /><p>{item.caption}</p><div className="production-inline-actions"><Button variant={decision?.decision === "approved" ? "primary" : "secondary"} aria-pressed={decision?.decision === "approved"} onClick={() => setOperationDecisions((current) => ({ ...current, [item.id]: { decision: "approved", reason: "" } }))}>Approve</Button><Button variant={decision?.decision === "refused" ? "danger" : "secondary"} aria-pressed={decision?.decision === "refused"} onClick={() => setOperationDecisions((current) => ({ ...current, [item.id]: { decision: "refused", reason: current[item.id]?.reason || "" } }))}>Refuse</Button></div>{decision?.decision === "refused" ? <label>Reason<input required value={decision.reason} onChange={(event) => setOperationDecisions((current) => ({ ...current, [item.id]: { ...current[item.id], reason: event.target.value } }))} /></label> : null}</article>; })}</div> : operationReviewError ? null : <p className="production-muted">All available items have already been reviewed. Refresh the run to continue.</p>}<Button disabled={!operationReviewItems.length || operationReviewItems.some((item) => !operationDecisions[item.id]?.decision || operationDecisions[item.id].decision === "refused" && !operationDecisions[item.id].reason.trim())} busy={busy} onClick={() => onReviewSubmit?.(operationReviewItems.map((item) => ({ id: item.id, decision: operationDecisions[item.id].decision, reason: operationDecisions[item.id].reason })))}>Submit review</Button></section> : null}
    {actions.length ? <section className="production-run-actions" aria-label="Integration run actions"><h3>Actions</h3>{actions.map(({ stageId, action, actionIndex, key }) => { const fields = [...(action.inputs || [])]; if (action.needs_reason && !fields.some((field) => field.name === "reason")) fields.unshift({ name: "reason", label: "Reason", required: true }); return <div className="production-run-action" key={key}><Button variant="secondary" onClick={() => { setSelectedAction(selectedAction === key ? "" : key); setActionInputs({}); }}>{action.label}</Button>{selectedAction === key ? <form onSubmit={(event) => { event.preventDefault(); const values = Object.fromEntries(fields.map((field) => [field.name, actionInputs[`${key}:${field.name}`] || ""])); onRunAction?.(stageId, actionIndex, values); setSelectedAction(""); }}><div className="production-inline-actions">{fields.map((field) => <label key={field.name}>{field.label || field.name}<input required={field.required || field.name === "reason" && action.needs_reason} value={actionInputs[`${key}:${field.name}`] || ""} onChange={(event) => setActionInputs((current) => ({ ...current, [`${key}:${field.name}`]: event.target.value }))} /></label>)}</div><Button type="submit" busy={busy}>Run {action.label}</Button></form> : null}</div>; })}</section> : null}
    <div className="production-inline-actions production-run-actions">{retryable ? <Button variant="secondary" busy={busy} onClick={() => onAction("retry", { stageId: retryable.stage_id })}>Retry failed step ({retryable.stage_id})</Button> : null}{ACTIVE_STATUSES.has(run.status) ? <Button variant="danger" busy={busy} onClick={() => onAction("cancel")}>Cancel run</Button> : null}{latestOutputRef(run) ? <p>Latest output reference: <code>{latestOutputRef(run)}</code></p> : null}</div>
  </section>;
}

function OutputSummary({ output }: { output: Record<string, unknown> }) {
  const typed = Array.isArray(output.outputs) ? output.outputs : [];
  const simple = Object.entries(output).filter(([key, value]) => key !== "outputs" && (typeof value === "string" || typeof value === "number" || typeof value === "boolean"));
  if (!typed.length && !simple.length && !Object.keys(output).length) return null;
  return <div className="production-output">
    {typed.map((raw, index) => {
      const item = asObject(raw);
      const label = readable(item.label ?? item.type) || `Output ${index + 1}`;
      const value = readable(item.value ?? item.ref);
      const kind = String(item.kind ?? item.type ?? "").toLowerCase();
      const mime = String(item.mime ?? item.content_type ?? "").toLowerCase();
      return <div className="production-typed-output" key={label + "-" + index}><strong>{label}</strong><TypedMediaPreview kind={kind} label={label} mime={mime} value={value} /></div>;
    })}
    {simple.map(([key, value]) => <p key={key}><strong>{key.replace(/[_-]/g, " ")}: </strong>{readable(value)}</p>)}
    <details><summary>Advanced output data</summary><pre>{JSON.stringify(output, null, 2)}</pre></details>
  </div>;
}

export default function Production({ projectId, projectName = "Selected project", projectLoading = false, view, runId, lineId }: { projectId: string; projectName?: string; projectLoading?: boolean; view: "lines" | "runs" | "reviews" | "review"; runId?: string; lineId?: string }) {
  const activeView: View = view === "reviews" ? "review" : view;
  const projectScoped = true;
  const selectedRunId = runId;
  const selectedLineId = lineId;
  const [lines, setLines] = useState<ProductionLine[]>([]);
  const [runs, setRuns] = useState<ProductionRun[]>([]);
  const [operations, setOperations] = useState<ProductionOperation[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [startingLineId, setStartingLineId] = useState("");
  const [startVars, setStartVars] = useState<RunVariable[]>([{ key: "", value: "", kind: "text" }]);
  const [selectedRun, setSelectedRun] = useState<ProductionRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [operationsError, setOperationsError] = useState("");
  const lineMap = useMemo(() => new Map(lines.map((line) => [line.id, line])), [lines]);

  const load = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    setError("");
    if (projectLoading) {
      setLines([]);
      setRuns([]);
      setLoading(true);
      return;
    }
    if (!projectId) {
      setLines([]);
      setRuns([]);
      setLoading(false);
      return;
    }
    try {
      const [lineResponse, runResponse] = await Promise.all([
        productionLinesApi.lines(projectId),
        productionLinesApi.runs(projectId),
      ]);
      setLines(lineResponse.items);
      setRuns(runResponse.items);
      void productionLinesApi.operations(projectId).then((response) => {
        setOperations(response.items);
        setOperationsError("");
      }).catch((caught) => setOperationsError(caught instanceof Error ? caught.message : "Operation list unavailable."));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Production data could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [projectId, projectLoading]);

  useEffect(() => { void load(true); }, [load]);
  useEffect(() => {
    if (!selectedRunId) { setSelectedRun(null); return; }
    const inList = runs.find((run) => run.id === selectedRunId);
    if (inList) { setSelectedRun(inList); return; }
    let current = true;
    productionLinesApi.run(selectedRunId).then((run) => { if (current) setSelectedRun(run); }).catch((caught) => {
      if (current) setError(caught instanceof Error ? caught.message : "Run could not be loaded.");
    });
    return () => { current = false; };
  }, [runs, selectedRunId]);
  useEffect(() => {
    if (!selectedLineId || selectedLineId === "new") {
      setDraft(selectedLineId === "new" ? lineDraft(undefined, projectId) : null);
      return;
    }
    const line = lines.find((item) => item.id === selectedLineId);
    if (line) setDraft(lineDraft(line, projectId));
  }, [lines, projectId, selectedLineId]);

  const mutate = async (operation: () => Promise<unknown>, message?: string) => {
    setBusy(true);
    setError("");
    try {
      await operation();
      await load();
      if (message) setError("");
      return true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The production action failed.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const todayStart = useMemo(() => {
    const date = new Date();
    date.setHours(0, 0, 0, 0);
    return date.getTime() / 1000;
  }, [runs]);
  const todayRuns = runs.filter((run) => run.created_at >= todayStart);
  const waitingRuns = runs.filter((run) => run.status === "waiting");
  const failedRuns = runs.filter((run) => run.status === "failed");
  const groupedRuns = useMemo(() => {
    const groups = new Map<string, ProductionRun[]>();
    for (const run of runs) {
      const key = run.current_stage || run.status;
      groups.set(key, [...(groups.get(key) || []), run]);
    }
    return groups;
  }, [runs]);
  const stagesInUse = useMemo(() => {
    const seen = new Map<string, ProductionStage>();
    for (const line of lines) for (const stage of line.definition.stages) if (!seen.has(stage.id)) seen.set(stage.id, stage);
    return [...seen.values()];
  }, [lines]);

  const openRun = (runId: string, nextView: View = activeView) => navigate(routeFor(nextView, { runId }));
  const openNewLine = () => navigate(routeFor("lines", { lineId: "new" }));
  const closeEditor = () => navigate(routeFor("lines"));
  const pageTitle = activeView === "review" ? "Reviews" : draft ? draft.id ? "Edit line" : "Create line" : activeView === "runs" ? "Runs" : "Lines";
  const pageDescription = activeView === "review"
    ? `${projectName} · Review line gates and operator decisions waiting for you.`
    : activeView === "runs"
      ? `${projectName} · Follow run progress, stages, and outputs.`
      : draft
        ? `${projectName} · Configure a reusable workflow from declared operations.`
        : `${projectName} · Reusable workflows assembled from declared operations and gates.`;

  const saveLine = async () => {
    if (!draft) return;
    const payload = { ...draft, project_id: draft.project_id || projectId };
    const ok = await mutate(() => draft.id && lines.some((line) => line.id === draft.id)
      ? productionLinesApi.updateLine(draft.id, payload)
      : productionLinesApi.createLine(payload));
    if (ok) closeEditor();
  };

  const runAction = async (action: "retry" | "continue" | "cancel", value?: Record<string, unknown>) => {
    if (!selectedRun) return;
    const ok = await mutate(() => action === "retry"
      ? productionLinesApi.retry(selectedRun.id, String(value?.stageId || ""))
      : action === "continue"
        ? productionLinesApi.continue(selectedRun.id, value || {})
        : productionLinesApi.cancel(selectedRun.id));
    if (!ok) return;
    const updated = await productionLinesApi.run(selectedRun.id).catch(() => null);
    if (updated) setSelectedRun(updated);
  };

  const runDeclaredAction = async (stageId: string, actionIndex: number, inputs: Record<string, unknown>) => {
    if (!selectedRun) return;
    const ok = await mutate(() => productionLinesApi.runAction(selectedRun.id, stageId, actionIndex, inputs));
    if (!ok) return;
    const updated = await productionLinesApi.run(selectedRun.id).catch(() => null);
    if (updated) setSelectedRun(updated);
  };

  const submitOperationReview = async (items: Array<{ id: string; decision: "approved" | "refused"; reason: string }>) => {
    if (!selectedRun) return;
    await mutate(async () => {
      const response = await productionLinesApi.submitReview(selectedRun.id, items);
      setSelectedRun(response.run);
    });
  };

  if (projectScoped && projectLoading) return <PageState kind="loading" message="Loading project scope…" compact />;
  if (projectScoped && !projectId) return <PageState kind="empty" title="Select a project" message="Choose a project in the workspace switcher to view its production lines and runs." compact />;
  if (projectScoped && loading) return <PageState kind="loading" message="Loading production lines and runs…" compact />;
  if (projectScoped && error && !lines.length && !runs.length) return <PageState kind="error" message={error} actionLabel="Retry" onAction={() => void load(true)} autoFocus />;

  return <div className="production-page">
    {error ? <p className="production-error" role="alert">{error}</p> : null}
    <PageHeader title={pageTitle} description={pageDescription}
      primaryAction={activeView === "lines" && !draft ? <Button onClick={openNewLine}>Create line</Button> : undefined}
      secondaryAction={activeView !== "lines" ? <Button variant="secondary" onClick={() => void load(true)}>Refresh</Button> : undefined} />

    {activeView === "lines" ? <section className="production-view" aria-label="Production lines">
      {draft ? <LineEditor draft={draft} operations={operations} busy={busy} onDraftChange={setDraft} onSave={() => void saveLine()} onClose={closeEditor} /> : null}
      {operationsError && draft ? <p className="production-error" role="status">The available operations is unavailable. Existing lines can still be reviewed.</p> : null}
      {!draft && lines.length ? <div className="production-line-grid">{lines.map((line) => {
        const lineRuns = runs.filter((run) => run.line_id === line.id);
        const today = lineRuns.filter((run) => run.created_at >= todayStart);
        const errors = lineRuns.filter((run) => run.status === "failed").length;
        const waiting = lineRuns.filter((run) => run.status === "waiting").length;
        return <article className="production-line-card" key={line.id}>
          <div className="production-section-heading"><div><h3>{line.name}</h3><code>{line.id}</code></div><Badge tone={statusTone(line.enabled ? "enabled" : "disabled")}>{line.enabled ? "Enabled" : "Disabled"}</Badge></div>
          <div className="production-line-stats"><span><strong>{today.length}</strong> runs today</span><span><strong>{line.definition.stages.length}</strong> stages</span><span><strong>{waiting}</strong> waiting</span><span><strong>{errors}</strong> errors</span></div>
          <div className="production-inline-actions"><Button onClick={() => { setStartingLineId(startingLineId === line.id ? "" : line.id); setStartVars([{ key: "", value: "", kind: "text" }]); }} disabled={!line.enabled}>{startingLineId === line.id ? "Close start form" : "Start run"}</Button><Button variant="secondary" onClick={() => navigate(routeFor("lines", { lineId: line.id }))}>Edit line</Button><Button variant="ghost" onClick={() => navigate(routeFor("runs"))}>View runs</Button></div>
          {startingLineId === line.id ? <form className="production-run-start" onSubmit={(event) => {
            event.preventDefault();
            const named = startVars.filter((item) => item.key.trim());
            if (new Set(named.map((item) => item.key.trim())).size !== named.length) { setError("Run input names must be unique."); return; }
            const vars = Object.fromEntries(named.map((item) => [item.key.trim(), item.kind === "number" ? Number(item.value) : item.kind === "boolean" ? item.value === "true" : item.value]));
            void mutate(() => productionLinesApi.start(line.id, projectId, vars)).then((ok) => { if (ok) setStartingLineId(""); });
          }}><h4>Run inputs</h4><p className="production-muted">Add values the line references through its run context.</p>{startVars.map((item, index) => <div className="production-run-variable" key={index}>
            <label>Input name<input value={item.key} onChange={(event) => setStartVars((current) => current.map((row, rowIndex) => rowIndex === index ? { ...row, key: event.target.value } : row))} placeholder="field_name" /></label>
            <label>Type<select value={item.kind} onChange={(event) => setStartVars((current) => current.map((row, rowIndex) => rowIndex === index ? { ...row, kind: event.target.value as RunVariable["kind"], value: event.target.value === "boolean" ? "true" : "" } : row))}><option value="text">Text</option><option value="number">Number</option><option value="boolean">True or false</option></select></label>
            {item.kind === "boolean" ? <label>Value<select value={item.value || "true"} onChange={(event) => setStartVars((current) => current.map((row, rowIndex) => rowIndex === index ? { ...row, value: event.target.value } : row))}><option value="true">True</option><option value="false">False</option></select></label>
              : <label>Value<input type={item.kind === "number" ? "number" : "text"} step={item.kind === "number" ? "any" : undefined} value={item.value} required={item.kind === "number"} onChange={(event) => setStartVars((current) => current.map((row, rowIndex) => rowIndex === index ? { ...row, value: event.target.value } : row))} /></label>}
          </div>)}<div className="production-inline-actions"><Button variant="ghost" onClick={() => setStartVars((current) => [...current, { key: "", value: "", kind: "text" }])}>Add input</Button><Button type="submit" busy={busy}>Run line</Button></div></form> : null}
        </article>;
      })}</div> : null}
      {!draft && !lines.length ? <PageState kind="empty" headingLevel={3} title="No lines yet" message="Create a line, choose declared operations, and arrange its stages here." actionLabel="Create line" onAction={openNewLine} compact /> : null}
    </section> : null}

    {activeView === "runs" ? <section className="production-view" aria-label="Production runs">
      <p className="production-muted">Select a run to inspect its timeline and outputs.</p>
      {runs.length ? <div className="production-board">{[...groupedRuns.entries()].map(([stageId, entries]) => { const stage = stagesInUse.find((item) => item.id === stageId); return <section className="production-board-column" key={stageId}><div className="production-section-heading"><h3>{String(stage?.label || stage?.id || stageId)}</h3><Badge>{entries.length}</Badge></div>{entries.slice(0, 30).map((run) => <RunCard key={run.id} run={run} line={lineMap.get(run.line_id)} stages={lineMap.get(run.line_id)?.definition.stages || []} onOpen={() => openRun(run.id, "runs")} />)}</section>; })}</div> : <PageState kind="empty" headingLevel={3} title="No runs yet" message="Start a line to see its stages and execution history here." compact />}
      {selectedRun && activeView === "runs" ? <><div className="production-section-heading production-detail-heading"><h2>Selected run</h2><Button variant="ghost" onClick={() => navigate(routeFor("runs"))}>Close detail</Button></div><RunDetail run={selectedRun} line={lineMap.get(selectedRun.line_id)} operations={operations} busy={busy} onAction={(action, value) => void runAction(action, value)} onRunAction={(stageId, index, inputs) => void runDeclaredAction(stageId, index, inputs)} onReviewSubmit={(items) => void submitOperationReview(items)} /></> : null}
    </section> : null}

    {activeView === "review" ? <section className="production-view" aria-label="Runs waiting for review">
      {waitingRuns.length || selectedRun ? <div className="production-review-layout"><div className="production-review-queue">{waitingRuns.length ? waitingRuns.map((run) => <RunCard key={run.id} run={run} line={lineMap.get(run.line_id)} stages={lineMap.get(run.line_id)?.definition.stages || []} onOpen={() => openRun(run.id, "review")} />) : <PageState kind="empty" headingLevel={3} title="Review submitted" message="This run is no longer waiting for a decision." compact />}</div>{selectedRun ? <RunDetail run={selectedRun} line={lineMap.get(selectedRun.line_id)} operations={operations} busy={busy} onAction={(action, value) => void runAction(action, value)} onRunAction={(stageId, index, inputs) => void runDeclaredAction(stageId, index, inputs)} onReviewSubmit={(items) => void submitOperationReview(items)} /> : <PageState kind="empty" headingLevel={3} title="Choose a run" message="Select a waiting run to inspect its items and submit a decision." compact />}</div> : <PageState kind="empty" headingLevel={3} title="Nothing needs review" message="Waiting runs will appear here when a line reaches a review or choice gate." compact />}
    </section> : null}

  </div>;
}
