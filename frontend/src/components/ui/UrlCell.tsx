function getDomain(value: string) {
  try { return new URL(value).hostname.replace(/^www\./, ""); } catch { return value; }
}

export function UrlCell({ url, maxLength = 34 }: { url?: string | null; maxLength?: number }) {
  if (!url) return <span className="muted-text">—</span>;
  const display = url.length > maxLength ? `${url.slice(0, maxLength)}…` : url;
  return <div className="url-cell" title={url}><span className="url-domain">{getDomain(url)}</span><a href={url} target="_blank" rel="noreferrer" className="url-open-link">Open</a><span className="url-full muted-text">{display}</span></div>;
}
