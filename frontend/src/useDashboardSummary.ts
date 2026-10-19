import { useEffect, useState } from "react";
import { operatorApi } from "./api/operatorClient";
import { integrationManagementApi } from "./api/integrationManagementClient";
import { productionLinesApi, type ProductionLine, type ProductionRun } from "./api/productionLinesClient";
import type { IntegrationManagementSummary, OperatorApproval } from "./types";

export type DashboardSummary = {
  lines: ProductionLine[];
  runs: ProductionRun[];
  approvals: OperatorApproval[];
  integrations: IntegrationManagementSummary[];
  loading: boolean;
  linesError: string;
  runsError: string;
  approvalsError: string;
  integrationsError: string;
};

const EMPTY: DashboardSummary = {
  lines: [], runs: [], approvals: [], integrations: [], loading: true,
  linesError: "", runsError: "", approvalsError: "", integrationsError: "",
};

export function useDashboardSummary(projectId: string, projectLoading: boolean): DashboardSummary {
  const [summary, setSummary] = useState(EMPTY);
  useEffect(() => {
    if (projectLoading) { setSummary(EMPTY); return; }
    if (!projectId) { setSummary({ ...EMPTY, loading: false }); return; }
    let active = true;
    setSummary(EMPTY);
    void Promise.allSettled([
      productionLinesApi.lines(projectId),
      productionLinesApi.runs(projectId),
      operatorApi.listApprovals("pending", projectId),
      integrationManagementApi.list(),
    ]).then(([linesResult, runsResult, approvalsResult, integrationsResult]) => {
      if (!active) return;
      const errorText = (result: PromiseSettledResult<unknown>, fallback: string) => result.status === "rejected" ? result.reason instanceof Error ? result.reason.message : fallback : "";
      setSummary({
        lines: linesResult.status === "fulfilled" ? linesResult.value.items : [],
        runs: runsResult.status === "fulfilled" ? runsResult.value.items : [],
        approvals: approvalsResult.status === "fulfilled" ? approvalsResult.value : [],
        integrations: integrationsResult.status === "fulfilled" ? integrationsResult.value.items : [],
        loading: false,
        linesError: errorText(linesResult, "Lines are unavailable."),
        runsError: errorText(runsResult, "Runs are unavailable."),
        approvalsError: errorText(approvalsResult, "Approvals are unavailable."),
        integrationsError: errorText(integrationsResult, "Integration status is unavailable."),
      });
    });
    return () => { active = false; };
  }, [projectId, projectLoading]);
  return summary;
}
