const moneyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2
});

const compactMoneyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1
});

const integerFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0
});

// Em-dash placeholder for absent values, matching the "no data" glyph used by
// the dashboard KPI cards and status labels (keeps the whole app consistent).
const EMPTY = "—";

export function formatMoney(value: number | null | undefined, compact = false) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return EMPTY;
  }
  return (compact ? compactMoneyFormatter : moneyFormatter).format(value);
}

export function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return EMPTY;
  }
  return `${(value * 100).toFixed(2)}%`;
}

export function formatInteger(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return EMPTY;
  }
  return integerFormatter.format(value);
}

export function formatMetricValue(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") {
    return EMPTY;
  }
  return typeof value === "number" ? formatInteger(value) : value;
}

export function formatDateTime(value: string | null | undefined) {
  if (!value) {
    return EMPTY;
  }
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(date);
}
