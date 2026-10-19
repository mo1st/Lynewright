import type { IntegrationProvenance } from "../../types";
import { CheckCircle2, PackageCheck, ShieldAlert } from "lucide-react";
import { Badge } from "../ui";

type Props = {
  provenance?: IntegrationProvenance | null;
  installPolicy?: "catalogue-only" | "confirm" | "open";
};

export default function IntegrationProvenanceDetails({ provenance, installPolicy = "confirm" }: Props) {
  if (!provenance) {
    return <section className="callout callout--warning" aria-label="Integration provenance">
      <strong>Provenance is not recorded</strong>
      <p>This integration needs a provenance record before its source can be verified.</p>
    </section>;
  }

  const devCatalogueKey = provenance.kind === "catalogue" && Boolean(provenance.signing_key_id?.startsWith("dev:"));
  const label = provenance.kind === "catalogue"
    ? devCatalogueKey ? "Catalogue (dev key)" : "Catalogue"
    : provenance.kind === "outside" ? provenance.status === "awaiting_confirmation" ? "Outside · Awaiting confirmation" : "Outside" : "Bundled";
  const tone = provenance.kind === "catalogue" ? devCatalogueKey ? "danger" : "success" : provenance.kind === "outside" ? "warning" : "info";
  const StatusIcon = provenance.kind === "catalogue" ? devCatalogueKey ? ShieldAlert : CheckCircle2 : provenance.kind === "outside" ? ShieldAlert : PackageCheck;
  const blockedByPolicy = provenance.kind === "outside" && installPolicy === "catalogue-only";

  return <section className="integration-provenance" aria-label="Integration provenance">
    <p><Badge tone={tone}><StatusIcon size={14} aria-hidden="true" />{label}</Badge></p>
    {provenance.source ? <p>Source: {provenance.source}</p> : null}
    {provenance.sha256 ? <p>SHA-256: <code>{provenance.sha256}</code></p> : null}
    {provenance.image_digest ? <p>Image digest: <code>{provenance.image_digest}</code></p> : null}
    {provenance.catalogue_entry_id ? <p>Catalogue entry: {provenance.catalogue_entry_id} {provenance.entry_version}</p> : null}
    {provenance.catalogue_serial != null ? <p>Catalogue serial: {provenance.catalogue_serial}</p> : null}
    {provenance.signing_key_id ? <p>Signing key: <code>{provenance.signing_key_id}</code></p> : null}
    {provenance.review?.reviewer ? <p>Reviewed by {provenance.review.reviewer}{provenance.review.date ? ` on ${provenance.review.date}` : ""}</p> : null}
    {provenance.confirmed_by ? <p>Installed by {provenance.confirmed_by}</p> : null}
    {provenance.status === "awaiting_confirmation" ? <div className="callout callout--warning" role="status"><strong>Awaiting confirmation</strong><p>This package remains disabled until an administrator reviews its install summary.</p></div> : null}
    {blockedByPolicy ? <div className="callout callout--danger" role="status"><strong>Not allowed by policy</strong><p>This outside integration may keep running while enabled, but catalogue-only policy prevents enabling it or re-enabling it after it is disabled.</p></div> : null}
    {provenance.status === "catalogue_drift" ? <div className="callout callout--warning" role="alert">
      <strong>Catalogue drift</strong>
      <p>{provenance.drift_reason || "The installed artifact no longer matches the signed catalogue."}</p>
    </div> : null}
  </section>;
}
