export type MetricView = { value: number | null; unit: string; status?: string };

export function formatMetric(metric: MetricView | null | undefined): string {
  if (!metric || metric.value === null || metric.status === "invalid") return "Not available";
  if (metric.status === "measurement_unknown") return "Measurement availability unknown";
  const digits = metric.unit === "%" ? 2 : metric.unit === "questions" ? 0 : 2;
  const number = new Intl.NumberFormat("en", { maximumFractionDigits: digits, minimumFractionDigits: metric.unit === "%" ? 2 : 0 }).format(metric.value);
  return metric.unit === "raw" ? `${number} (raw)` : `${number} ${metric.unit}`;
}

export function terminalOutcome(row: { transport_ok?: unknown; parse_ok?: unknown; correct?: unknown }): string {
  const booleanValue = (value: unknown): boolean | null => {
    if (typeof value === "boolean") return value;
    if (typeof value === "string") {
      if (["true", "1", "yes"].includes(value.trim().toLowerCase())) return true;
      if (["false", "0", "no"].includes(value.trim().toLowerCase())) return false;
    }
    return null;
  };
  if (booleanValue(row.transport_ok) === false) return "transport_failed";
  if (booleanValue(row.parse_ok) === false) return "parse_failed";
  if (booleanValue(row.correct) === true) return "correct";
  if (booleanValue(row.correct) === false) return "wrong";
  return "unknown";
}

export function formatDate(epochMs: number | null | undefined, timezone = "Asia/Hong_Kong"): string {
  if (epochMs === null || epochMs === undefined || !Number.isFinite(epochMs)) return "Unknown";
  return new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short", timeZone: timezone }).format(new Date(epochMs));
}
