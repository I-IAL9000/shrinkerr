import i18n from "./i18n";
/** Format a number with commas: 113498 → "113,498" */
export function fmtNum(n: number | null | undefined): string {
  if (n == null) return "0";
  return n.toLocaleString();
}

/**
 * Date + time in the interface language (v0.9.132). Previously dates used the
 * browser's locale, so a Spanish UI on an en-US browser still showed
 * "4/22/2026, 8:06:15 PM". Falls back to the raw value if it isn't a date.
 */
export function fmtDateTime(value: string | number | Date | null | undefined): string {
  if (value == null || value === "") return "";
  const d = value instanceof Date ? value : new Date(value);
  if (isNaN(d.getTime())) return String(value);
  return d.toLocaleString(i18n.language || undefined);
}
