import { useCallback, useEffect, useState } from "react";
import { operatorApi } from "../../api/operatorClient";
import { useProject } from "../../context/ProjectContext";
import type { OperatorProfile } from "../../types";
import { OperatorSettingsPanel } from "./OperatorSettingsPanel";
import { OperatorRunsPanel } from "./OperatorRunsPanel";
import { OperatorApprovalsPanel } from "./OperatorApprovalsPanel";
import { OperatorAutomationPanel } from "./OperatorAutomationPanel";
import { TelegramOperatorPanel } from "./TelegramOperatorPanel";

/**
 * The whole minimum Operator surface, stacked in one settings section
 * (plan §11/§12): profile setup → manual runs + history → pending approvals →
 * automations → Telegram activation. Profile state is owned here because both
 * the settings panel and the run launcher read it.
 */
export function OperatorSection({ view = "all" }: { view?: "all" | "operator" }) {
  const { projectId, loading: projectLoading } = useProject();
  const [profiles, setProfiles] = useState<OperatorProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  const loadProfiles = useCallback(async () => {
    if (projectLoading || !projectId) {
      setProfiles([]);
      setLoadError("");
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      setProfiles(await operatorApi.listProfiles(projectId));
      setLoadError("");
    } catch (caught) {
      setProfiles([]);
      setLoadError(caught instanceof Error ? caught.message : "Could not load Operator profiles.");
    } finally {
      setLoading(false);
    }
  }, [projectId, projectLoading]);

  useEffect(() => { void loadProfiles(); }, [loadProfiles]);

  return (
    <div className="page-stack">
      <header className="settings-section-intro">
        <h2>Internal Operator</h2>
        <p>Set up your BYOK operator agent, launch objectives, review what it did, approve sensitive actions, and automate the routine.{view === "all" ? " Telegram is the away-from-desk channel." : ""}</p>
      </header>
      {loadError ? <p className="error-message" role="alert">{loadError}</p> : null}
      <OperatorSettingsPanel
        profiles={profiles}
        loading={loading}
        loadError={loadError}
        onRefresh={loadProfiles}
      />
      <OperatorRunsPanel profiles={profiles} profilesLoading={loading} />
      <OperatorApprovalsPanel />
      <OperatorAutomationPanel />
      {/* Telegram is its own settings section now: it is a delivery channel with
          its own credentials and its own failure modes, and stacking it under
          the run history meant the only way to reach it was to scroll past four
          unrelated panels. `view="all"` keeps the original single-page form for
          any caller that still wants it. */}
      {view === "all" ? <TelegramOperatorPanel /> : null}
    </div>
  );
}
