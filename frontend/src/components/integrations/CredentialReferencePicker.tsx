import { useId, useMemo, useState } from "react";
import type { CredentialMetadata } from "../../api/client";

type Props = {
  id: string;
  selected: string[];
  options: CredentialMetadata[];
  onChange: (values: string[]) => void;
  label?: string;
};

/**
 * A small, native searchable credential selector. It only handles references;
 * secret material remains inside the credential manager's write-only form.
 */
export default function CredentialReferencePicker({ id, selected, options, onChange, label = "Stored credentials" }: Props) {
  const searchId = useId();
  const [search, setSearch] = useState("");
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const filtered = useMemo(
    () => options.filter((credential) => `${credential.name} ${credential.kind || "token"}`.toLocaleLowerCase().includes(normalizedSearch)),
    [normalizedSearch, options],
  );

  return <div className="credential-reference-picker">
    <label htmlFor={searchId}>{label}
      <input id={searchId} name={`${id}-search`} type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search stored credentials" autoComplete="off" />
    </label>
    {filtered.length ? <select id={id} name={id} autoComplete="off" multiple value={selected} aria-label={label} onChange={(event) => onChange(Array.from(event.target.selectedOptions, (option) => option.value))}>
      {filtered.map((credential) => <option key={credential.name} value={credential.name}>{credential.name} · {credential.kind || "token"}</option>)}
    </select> : <p className="muted-text">No matching stored credentials. Use Add credential to create one.</p>}
    {selected.length ? <small className="muted-text">Selected: {selected.join(", ")}</small> : <small className="muted-text">Choose one or more stored references.</small>}
    <a className="credential-reference-picker__add" href="#credential-management">Add credential</a>
  </div>;
}
