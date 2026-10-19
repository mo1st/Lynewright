import { Button } from "../ui";
import { useCallback, useEffect, useState } from "react";
import { ApiError, describeApiError } from "../../api/client";
import { operatorApi } from "../../api/operatorClient";
import { useProject } from "../../context/ProjectContext";
import type { OperatorApproval } from "../../types";
import { Badge } from "../ui";
import { RunStateBadge, formatWhen } from "./shared";

export function OperatorApprovalsPanel() {
  const { projectId, loading: projectLoading } = useProject();
  const [approvals, setApprovals] = useState<OperatorApproval[] | null>(null);
  const [showResolved, setShowResolved] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [noteDrafts, setNoteDrafts] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    if (projectLoading || !projectId) {
      setApprovals([]);
      setLoadError("");
      return;
    }
    try {
      // status=pending is the operator-facing default; resolved history is a
      // secondary view so an empty pending list never looks like a fault.
      const pending = await operatorApi.listApprovals("pending", projectId);
      if (showResolved) {
        const all = await operatorApi.listApprovals(undefined, projectId);
        setApprovals(mergeResolved(pending, all));
      } else {
        setApprovals(pending);
      }
      setLoadError("");
    } catch (caught) {
      setApprovals(null);
      setLoadError(describeApiError(caught, "Could not load approvals."));
    }
  }, [projectId, projectLoading, showResolved]);

  useEffect(() => { void load(); }, [load]);

  async function resolve(approval: OperatorApproval, decision: "approved" | "denied") {
    setBusyId(approval.id); setActionError("");
    try {
      await operatorApi.resolveApproval(approval.id, decision, noteDrafts[approval.id]?.trim() ?? "");
      // Dashboard approval must have the same durable wake-up semantics as
      // Telegram. The resolve endpoint records the human decision; this
      // shared resume endpoint advances the waiting run exactly once.
      if (decision === "approved") {
        // Resuming may include model fallback and a provider job boundary.
        // Do not hold the approval button in "Working…" until that whole run
        // finishes. The durable approval is already resolved; the worker/API
        // will continue the run and the follow-up refresh will reveal its
        // terminal state or any newly requested approval.
        void operatorApi.resumeRun(approval.run_id)
          .then(() => void load())
          .catch((caught) => {
            // Telegram or a worker may have won the wake-up race after the
            // approval was resolved. A 409 here means the run is already
            // resumed/terminal, not that the approval failed.
            if (caught instanceof ApiError && caught.status === 409) return;
            setActionError(describeApiError(caught, "Approval was recorded, but the run could not be resumed."));
            void load();
          });
      }
      await load();
    } catch (caught) {
      setActionError(describeApiError(caught, "Could not resolve this approval."));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h3>{showResolved ? "Approvals" : "Pending approvals"}</h3>
          <p>Actions the Operator will not take without you. Approving here resolves the same approval Telegram shows.</p>
        </div>
        <div className="inline-actions">
          <label className="checkbox-row" htmlFor="operator-approvals-show-resolved">
            <input id="operator-approvals-show-resolved" name="operator_show_resolved" type="checkbox" checked={showResolved}
              onChange={(event) => setShowResolved(event.target.checked)} />
            Show resolved
          </label>
          <Button variant="secondary" type="button" onClick={() => void load()}>Refresh</Button>
        </div>
      </div>

      {loadError ? <p className="error-message" role="alert">{loadError}</p> : null}
      {actionError ? <p className="error-message" role="alert">{actionError}</p> : null}
      {approvals === null && !loadError ? <p className="muted-text">Loading approvals…</p> : null}

      {approvals !== null && approvals.length === 0 && !showResolved ? (
        <p className="muted-text" role="status">No pending approvals. The Operator is not waiting on you.</p>
      ) : null}
      {approvals !== null && approvals.length === 0 && showResolved ? (
        <p className="muted-text" role="status">No approvals recorded yet.</p>
      ) : null}

      {approvals !== null && approvals.length > 0 ? (
        <ol className="settings-provider-chain" aria-label="Operator approvals">
          {approvals.map((approval) => (
            <li className="settings-provider-chain-item operator-list-item operator-approval-item" key={approval.id}>
              <div className="settings-provider-copy">
                <div className="settings-provider-title">
                  <strong><code>{approval.action}</code></strong>
                  <RunStateBadge value={approval.status} />
                </div>
                {approval.summary ? <p style={{ margin: "4px 0" }}>{approval.summary}</p> : null}
                <div className="settings-provider-meta">
                  <span title={approval.run_id}>run {approval.run_id.slice(0, 14)}…</span>
                  <span aria-hidden="true">·</span>
                  <span>{formatWhen(approval.created_at)}</span>
                  {approval.resolved_at
                    ? <span>· resolved {formatWhen(approval.resolved_at)}{approval.resolved_by ? ` by ${approval.resolved_by}` : ""}</span>
                    : null}
                </div>
                {approval.status !== "pending" && approval.resolution_note
                  ? <p className="muted-text">Note: {approval.resolution_note}</p>
                  : null}
              </div>
              {approval.status === "pending" ? (
                <div className="settings-form" style={{ minWidth: 220 }}>
                  <label className="alert-field" htmlFor={`operator-resolution-note-${approval.id}`}>
                    <span>Resolution note (optional)</span>
                    <input id={`operator-resolution-note-${approval.id}`} name={`operator_resolution_note_${approval.id}`} autoComplete="off" placeholder="Why you approved or denied this"
                      value={noteDrafts[approval.id] ?? ""}
                      onChange={(event) => setNoteDrafts((current) => ({ ...current, [approval.id]: event.target.value }))} />
                  </label>
                  <div className="inline-actions">
                    <Button variant="secondary" type="button" disabled={busyId !== null} onClick={() => void resolve(approval, "approved")}>
                      {busyId === approval.id ? "Working…" : "Approve"}
                    </Button>
                    <Button variant="secondary" type="button" disabled={busyId !== null} onClick={() => void resolve(approval, "denied")}>Deny</Button>
                  </div>
                </div>
              ) : (
                <Badge tone={approval.status === "approved" ? "positive" : approval.status === "cancelled" ? "neutral" : "critical"}>
                  {approval.status === "approved" ? "Approved" : approval.status === "cancelled" ? "Cancelled" : "Denied"}
                </Badge>
              )}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function mergeResolved(pending: OperatorApproval[], all: OperatorApproval[]): OperatorApproval[] {
  const seen = new Set(pending.map((item) => item.id));
  const resolved = all.filter((item) => item.status !== "pending" && !seen.has(item.id));
  return [...pending, ...resolved];
}
