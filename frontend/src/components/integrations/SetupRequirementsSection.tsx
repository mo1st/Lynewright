import { Button } from "../ui";
import { useEffect, useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary } from "../../types";
import type { Mutate } from "./sectionTypes";

type Requirement = IntegrationManagementSummary["setup_requirements"][number];
type Props = { integrationId: string; summary: Requirement[]; runtime?: string; busy: boolean; mutate: Mutate };

export default function SetupRequirementsSection({ integrationId, summary, runtime, busy, mutate }: Props) {
  const [requirements, setRequirements] = useState<Requirement[]>(summary);
  useEffect(() => { let active = true; void integrationManagementApi.setupRequirements(integrationId).then((response) => { if (active) setRequirements(response.items as Requirement[]); }).catch(() => { if (active) setRequirements(summary); }); return () => { active = false; }; }, [integrationId, summary]);
  const pending = requirements.filter((item) => item.state !== "VERIFIED");
  if (!pending.length && !["mcp", "openapi"].includes(runtime ?? "")) return null;
  return <section className="integration-subsection" aria-label="Setup order"><h4>Setup order</h4>{["mcp", "openapi"].includes(runtime ?? "") ? <p className="muted-text">Approve the integration, probe a read-only operation, declare the mapping, then rediscover resources.</p> : null}{pending.length ? <div><h5>Verify setup</h5>{pending.map((requirement) => <p key={requirement.id}>{requirement.requirement_type} for binding {requirement.binding_id}<Button variant="secondary" type="button" disabled={busy} onClick={() => void mutate(`setup:${requirement.id}`, () => integrationManagementApi.verifySetup(integrationId, requirement.binding_id, requirement.id), "Setup requirement verification completed.")}>Verify</Button></p>)}</div> : null}</section>;
}
