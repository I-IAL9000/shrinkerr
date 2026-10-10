import i18n from "./i18n";
// Numbers follow the interface language, like dates (v0.10.0: they used the
// browser's locale, so a Spanish UI on an English browser showed "4.4 GB").
const locale = () => i18n.language || undefined;

/** Format a number with commas: 113498 → "113,498" */
export function fmtNum(n: number | null | undefined): string {
  if (n == null) return "0";
  return n.toLocaleString(locale());
}

/** A number with exactly `digits` decimals: 1.5 → "1.5" ("1,5" in Spanish). */
export function fmtDecimal(n: number | null | undefined, digits = 1): string {
  return (Number(n) || 0).toLocaleString(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

/**
 * A size in the largest unit that keeps it at least 1, with fewer decimals as
 * it grows: 512 B, 3.2 MB, 4.4 GB, 135 GB, 1.21 TB (1024 multiples). v0.10.0:
 * six copies disagreed — Monitor showed "0.0 TB" for 4.4 GB, a poster
 * "1536.0 GB", Activity "0.00 GB" for 3 MB.
 */
export function fmtBytes(bytes: number | null | undefined): string {
  const b = Number(bytes) || 0;
  let value = Math.abs(b);
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit++;
  }
  const digitsFor = (v: number, u: number) => (u === 0 || v >= 100 ? 0 : v >= 10 || u < 4 ? 1 : 2);
  let digits = digitsFor(value, unit);
  if (Number(value.toFixed(digits)) >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;  // 1023.97 KB rounds to "1024 KB": say "1.0 MB"
    unit++;
    digits = digitsFor(value, unit);
  }
  return `${b < 0 ? "-" : ""}${fmtDecimal(value, digits)} ${BYTE_UNITS[unit]}`;
}

/** A bitrate in bits per second: "12.3 Mb/s", "640 kb/s" (v0.10.0). */
export function fmtBitrate(bps: number | null | undefined): string {
  const b = Number(bps) || 0;
  if (b >= 1_000_000) return `${fmtDecimal(b / 1_000_000, 1)} Mb/s`;
  return `${fmtNum(Math.round(b / 1000))} kb/s`;
}

/**
 * A duration from whole seconds: "45s", "2m 5s", "1h 59m", "2d 3h".
 * v0.10.0: the old one rounded each part, giving "2m 60s" and "1h 60m".
 */
export function fmtDuration(seconds: number | null | undefined): string {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return i18n.t("common:duration.s", { s });
  if (s < 3600) return i18n.t("common:duration.ms", { m: Math.floor(s / 60), s: s % 60 });
  if (s < 86400) return i18n.t("common:duration.hm", { h: Math.floor(s / 3600), m: Math.floor((s % 3600) / 60) });
  return i18n.t("common:duration.dh", { d: Math.floor(s / 86400), h: Math.floor((s % 86400) / 3600) });
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
