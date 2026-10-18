type TypedMediaPreviewProps = {
  kind: string;
  label: string;
  mime: string;
  value: string;
};

function isExternalUrl(value: string): boolean {
  try {
    return ["http:", "https:"].includes(new URL(value).protocol);
  } catch {
    return false;
  }
}

export default function TypedMediaPreview({ kind, label, mime, value }: TypedMediaPreviewProps) {
  const mediaType = mime || kind;
  if (!isExternalUrl(value)) return <span>{value || "Available"}</span>;
  if (kind === "download") return <a href={value} download>{`Download ${label}`}</a>;
  if (kind === "image" || mediaType.startsWith("image/")) return <img src={value} alt={label} loading="lazy" />;
  if (kind === "audio" || mediaType.startsWith("audio/")) return <audio controls preload="none" src={value}>{label}</audio>;
  if (kind === "video" || mediaType.startsWith("video/")) return <video controls preload="none" src={value}>{label}</video>;
  if (mediaType === "application/pdf") return <iframe title={label} src={value} loading="lazy" />;
  return <a href={value} target="_blank" rel="noreferrer">Open output</a>;
}
