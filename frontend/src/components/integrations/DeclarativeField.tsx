import type { IntegrationSettingSchemaField } from "../../types";

type Props = {
  field: IntegrationSettingSchemaField;
  id: string;
  value: unknown;
  disabled?: boolean;
  onChange: (value: unknown) => void;
};

function stringValue(value: unknown) {
  return typeof value === "string" ? value : "";
}

/** Shared renderer for bounded manifest/settings fields. */
export default function DeclarativeField({ field, id, value, disabled = false, onChange }: Props) {
  if (field.type === "boolean") {
    return <label className="checkbox-row" htmlFor={id}>
      <input id={id} name={field.key} type="checkbox" autoComplete="off" checked={Boolean(value)} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
      {field.label}
      {field.description ? <small className="muted-text">{field.description}</small> : null}
    </label>;
  }
  if (field.type === "enum") {
    return <label htmlFor={id}>{field.label}
      <select id={id} name={field.key} required={field.required} disabled={disabled} value={stringValue(value)} onChange={(event) => onChange(event.target.value)}>
        {!field.required ? <option value="">Choose an option</option> : null}
        {(field.options ?? []).map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
      {field.description ? <small className="muted-text">{field.description}</small> : null}
    </label>;
  }
  if (field.type === "number") {
    return <label htmlFor={id}>{field.label}
      <input id={id} name={field.key} type="number" autoComplete="off" required={field.required} min={field.min} max={field.max} disabled={disabled} value={typeof value === "number" ? value : ""} onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))} />
      {field.description ? <small className="muted-text">{field.description}</small> : null}
    </label>;
  }
  if (field.type === "list") {
    return <label htmlFor={id}>{field.label}
      <input id={id} name={field.key} autoComplete="off" required={field.required} disabled={disabled} value={Array.isArray(value) ? value.join(", ") : ""} onChange={(event) => onChange(event.target.value.split(",").map((item) => item.trim()).filter(Boolean))} />
      <small className="muted-text">{field.description ?? `Up to ${field.max_items ?? 20} values.`}</small>
    </label>;
  }
  return <label htmlFor={id}>{field.label}
    <input id={id} name={field.key} type="text" autoComplete="off" required={field.required} disabled={disabled} value={stringValue(value)} onChange={(event) => onChange(event.target.value)} />
    {field.description ? <small className="muted-text">{field.description}</small> : null}
  </label>;
}

export function fieldValue(field: IntegrationSettingSchemaField, values: Record<string, unknown>) {
  return values[field.key] ?? field.default ?? (field.type === "boolean" ? false : field.type === "list" ? [] : "");
}
