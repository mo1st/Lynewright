import { useEffect, useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationInboundHandler } from "../../types";

type Props = { integrationId: string };
export default function WebhooksSection({ integrationId }: Props) {
  const [handlers, setHandlers] = useState<IntegrationInboundHandler[]>([]);
  useEffect(() => { let active = true; void integrationManagementApi.webhooks(integrationId).then((response) => { if (active) setHandlers(response.items); }).catch(() => { if (active) setHandlers([]); }); return () => { active = false; }; }, [integrationId]);
  return <section className="integration-subsection"><h4>Inbound webhooks &amp; callbacks</h4>{handlers.length ? <div className="table-wrap"><table className="responsive-record-table"><thead><tr><th scope="col">Handler</th><th scope="col">Endpoint</th><th scope="col">Verification</th></tr></thead><tbody>{handlers.map((handler) => <tr key={handler.id}><td data-label="Handler">{handler.handler_id}<small className="muted-text">{handler.kind}</small></td><td data-label="Endpoint" className="mono-text"><span className="text-truncate" title={handler.endpoint}>{handler.endpoint}</span></td><td data-label="Verification">{handler.verification_mode}{handler.timestamp_header ? ` — timestamp ${handler.timestamp_header}` : ""}</td></tr>)}</tbody></table></div> : <p className="muted-text">No inbound handlers are registered for this integration.</p>}</section>;
}
