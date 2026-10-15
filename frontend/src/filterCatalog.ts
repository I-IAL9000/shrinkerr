/**
 * The Scanner's filter bar (v0.10.0): quick pills, categories, their sections
 * and options, from filterCatalog.json. A backend test checks it against the
 * server's filters (backend/tests/test_filter_catalog.py): each section is one
 * server group, so ticking more in a section shows more.
 */
import catalog from "./filterCatalog.json";
import { decodeAdvanced } from "./advancedSearch";
import { facetValueLabel, languageKey } from "./facetLabels";

type T = (key: string, options?: Record<string, unknown>) => string;

export interface FilterOption { key: string; label?: string; labelKey?: string }
// prefix: its options don't read alone ("7 days", "Other"): a category's
// button names them with the section, as their chips do.
// facet: a list whose values come from the server (alang:fre), instead of
// options; chipLabelKey: its name on a chip when the menu's ("Language")
// doesn't say which ("Audio language").
export interface FilterSection {
  key: string; labelKey: string; hintKey?: string; prefix?: boolean;
  options?: FilterOption[]; facet?: string; chipLabelKey?: string;
  searched?: boolean;  // a long list (actors): the server searches it, top values first
}
export interface FilterCategory { key: string; labelKey: string; sections: FilterSection[] }

export const QUICK_FILTERS: string[] = catalog.quick;
export const CATEGORIES = catalog.categories as FilterCategory[];

interface Place { option: FilterOption; section?: FilterSection; category?: FilterCategory }
const PLACES = new Map<string, Place>();
const LISTS = new Map<string, { section: FilterSection; category: FilterCategory }>();
for (const category of CATEGORIES) {
  for (const section of category.sections) {
    for (const option of section.options ?? []) PLACES.set(option.key, { option, section, category });
    if (section.facet) LISTS.set(section.facet, { section, category });
  }
}
for (const option of catalog.hidden as FilterOption[]) PLACES.set(option.key, { option });

// Older names of the same filter (identical on the server): read as the new
// one, so it widens with its section and its chip says where it is.
const ALIASES: Record<string, string> = Object.fromEntries(
  (catalog.hidden as (FilterOption & { alias?: string })[]).filter(h => h.alias).map(h => [h.key, h.alias as string]),
);

/** Filter tokens from an address, a saved view or an older version. A list
 *  value as the server reads it ("alang:FRA" is "alang:fre"), so its list's
 *  row is the one ticked, not a second chip beside it. */
export function normalizeFilters(tokens: string[]): string[] {
  const out = tokens.flatMap(token => {
    const excluded = token.startsWith("!");
    const key = excluded ? token.slice(1) : token;
    if (key in ALIASES) return [(excluded ? "!" : "") + ALIASES[key]];
    const list = parseListToken(key);
    if (!list) return [token];
    const value = listValue(list.facet, list.value);
    return value ? [(excluded ? "!" : "") + listToken(list.facet, value)] : [];
  });
  // A filter and its exclusion both ("alang:fre,!alang:fre", from a link):
  // the last one, where the first stood — one row, one chip.
  const last = new Map<string, string>();
  for (const token of out) last.set(token.replace(/^!/, ""), token);
  return [...last.values()];
}

// A list's value as the server reads it (scan_filters.facet_filter): a
// track's field trimmed of spaces with its ASCII letters lower case, a
// language by its B code, a channel band, Science Fiction as Sci-Fi
// (scan_filters.genre_parts); null for one it can't have.
export function listValue(facet: string, value: string): string | null {
  if (!value) return null;
  if (facet === "achan") return ["1", "2", "6", "8"].includes(value) ? value : null;
  if (facet === "genre") return value.trim().toLowerCase() === "science fiction" ? "sci-fi" : value;
  if (facet !== "alang" && facet !== "slang" && facet !== "acodec") return value;
  const text = value.replace(/^ +| +$/g, "").replace(/[A-Z]/g, c => c.toLowerCase());
  return facet === "acodec" ? text || null : languageKey(text);
}

/** A list value's token parts, or null: "alang:fre" -> { facet: "alang", value: "fre" }. */
export function parseListToken(key: string): { facet: string; value: string } | null {
  const at = key.indexOf(":");
  if (at < 0 || key.startsWith("adv:")) return null;
  const facet = key.slice(0, at);
  if (!LISTS.has(facet)) return null;
  try {
    return { facet, value: decodeURIComponent(key.slice(at + 1)) };
  } catch {
    return { facet, value: key.slice(at + 1) };
  }
}

export function listToken(facet: string, value: string): string {
  return `${facet}:${encodeURIComponent(value)}`;
}

export function listSection(facet: string): FilterSection | undefined {
  return LISTS.get(facet)?.section;
}

/** An option's label; an unknown id (an old bookmark) as itself. `label`:
 *  a list value's own, from the server (a media folder's name). */
export function optionLabel(key: string, t: T, label?: string): string {
  const list = parseListToken(key);
  if (list) return facetValueLabel(list.facet, list.value, t, label);
  const option = PLACES.get(key)?.option;
  if (!option) return key.replace(/_/g, " ");
  return option.labelKey ? t(option.labelKey) : option.label ?? key;
}

/** A filter-string token as a chip: "Codec: x264", "Codec: not x264", "Advanced: 2 conditions".
 *  No section prefix when the label already names it ("Audio cleanup", not "Audio: Audio cleanup").
 *  `valueLabel`: a list value's own label, from the server (a media folder's name). */
export function chipLabel(token: string, t: T, valueLabel?: string): string {
  if (token.startsWith("adv:")) {
    return t("scanner:filters.advancedLabel", { count: decodeAdvanced(token)?.p.length ?? 0 });
  }
  const excluded = token.startsWith("!");
  const key = excluded ? token.slice(1) : token;
  const list = parseListToken(key);
  if (list) {
    // Always with the list's name: "French" alone doesn't say audio or subtitles.
    const section = LISTS.get(list.facet)!.section;
    return t(excluded ? "scanner:filterBar.chipNot" : "scanner:filterBar.chip", {
      section: t(section.chipLabelKey ?? section.labelKey),
      label: facetValueLabel(list.facet, list.value, t, valueLabel),
    });
  }
  const label = optionLabel(key, t);
  const section = PLACES.get(key)?.section;
  const sectionLabel = section ? t(section.labelKey) : "";
  if (!section || label.toLowerCase().includes(sectionLabel.toLowerCase())) {
    return excluded ? t("scanner:filters.notLabel", { label }) : label;
  }
  return t(excluded ? "scanner:filterBar.chipNot" : "scanner:filterBar.chip", { section: sectionLabel, label });
}

/** A category's options that are on, in its order: ticked or excluded. */
export function selectedIn(category: FilterCategory, active: string[]): { key: string; excluded: boolean }[] {
  const out: { key: string; excluded: boolean }[] = [];
  for (const section of category.sections) {
    if (section.facet) {
      // A list's values, in the order they were ticked.
      const prefix = `${section.facet}:`;
      for (const token of active) {
        if (token.startsWith(prefix)) out.push({ key: token, excluded: false });
        else if (token.startsWith("!" + prefix)) out.push({ key: token.slice(1), excluded: true });
      }
      continue;
    }
    for (const option of section.options ?? []) {
      if (active.includes(option.key)) out.push({ key: option.key, excluded: false });
      else if (active.includes("!" + option.key)) out.push({ key: option.key, excluded: true });
    }
  }
  return out;
}

/** Tick (exclude = false) or ⊘ (exclude = true) an option: on, or off if it
 *  already was. Ticking an excluded option includes it, and vice versa. */
export function toggleFilter(active: string[], key: string, exclude: boolean): string[] {
  const token = exclude ? "!" + key : key;
  const rest = active.filter(x => x !== "all" && x !== key && x !== "!" + key);
  const next = active.includes(token) ? rest : [...rest, token];
  return next.length ? next : ["all"];
}
