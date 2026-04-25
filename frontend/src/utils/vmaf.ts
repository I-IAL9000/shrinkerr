/**
 * Canonical VMAF tier table for the whole app.
 *
 * One place to edit — every component (FilterBar chips, JobListItem inline
 * score, EstimateModal test result, FileDetail history tab, DashboardPage
 * donut, EventTimeline activity entries) goes through these helpers so the
 * cuts and colours can never drift apart again.
 *
 * Tiers (matches FilterBar's labels, which were the user-visible ground
 * truth before v0.3.32):
 *   - Excellent (93+)   → green  (var(--success))
 *   - Good      (87–93) → amber  (var(--warning))
 *   - Poor      (<87)   → red    (var(--danger))
 *
 * The previous "Fair (80–87)" tier is folded into Poor — most encodes that
 * land in the 80s have visible artifacts, and the 4-tier label rarely
 * matched the 3-cut color thresholds anyway, which produced confusing
 * mismatches like "Good 89 → green chip but amber color".
 *
 * Backend mirror lives in backend/queue.py + backend/test_encode.py +
 * backend/routes/stats.py. Edit both halves together.
 */

export type VmafTier = "excellent" | "good" | "poor";

export const VMAF_EXCELLENT_MIN = 93;
export const VMAF_GOOD_MIN = 87;

export function vmafTier(score: number): VmafTier {
  if (score >= VMAF_EXCELLENT_MIN) return "excellent";
  if (score >= VMAF_GOOD_MIN) return "good";
  return "poor";
}

const TIER_LABEL: Record<VmafTier, string> = {
  excellent: "Excellent",
  good: "Good",
  poor: "Poor",
};

const TIER_RANGE: Record<VmafTier, string> = {
  excellent: "93+",
  good: "87–93",
  poor: "<87",
};

const TIER_COLOR: Record<VmafTier, string> = {
  excellent: "var(--success)",
  good: "var(--warning)",
  poor: "var(--danger)",
};

/** Display label, e.g. "Excellent". */
export function vmafLabel(score: number): string {
  return TIER_LABEL[vmafTier(score)];
}

/** Display range hint, e.g. "93+", "87–93", "<87". */
export function vmafRange(tier: VmafTier): string {
  return TIER_RANGE[tier];
}

/** Combined "Excellent (93+)" form for legend / chip text. */
export function vmafLabelWithRange(tier: VmafTier): string {
  return `${TIER_LABEL[tier]} (${TIER_RANGE[tier]})`;
}

/** CSS variable reference — pass to `style={{ color }}` directly. */
export function vmafColor(score: number): string {
  return TIER_COLOR[vmafTier(score)];
}

/** Colour for a tier (use when you already have the tier — e.g. dashboard donut). */
export function tierColor(tier: VmafTier): string {
  return TIER_COLOR[tier];
}

/** Tinted background (≈20% of the tier colour) — use for pill/chip backgrounds.
 *  Uses `color-mix()` so the tint follows the active theme's CSS variable. */
export function vmafTintBg(score: number): string {
  return `color-mix(in srgb, ${vmafColor(score)} 20%, transparent)`;
}

