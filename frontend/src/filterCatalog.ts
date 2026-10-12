/**
 * The Scanner's filter bar (v0.10.0): quick pills, categories, their sections
 * and options, from filterCatalog.json. A backend test checks it against the
 * server's filters (backend/tests/test_filter_catalog.py): each section is one
 * server group, so ticking more in a section shows more.
 */
import catalog from "./filterCatalog.json";
import { decodeAdvanced } from "./advancedSearch";

type T = (key: string, options?: Record<string, unknown>) => string;

export interface FilterOption { key: string; label?: string; labelKey?: string }
export interface FilterSection { key: string; labelKey: string; hintKey?: string; options: FilterOption[] }
export interface FilterCategory { key: string; labelKey: string; sections: FilterSection[] }

export const QUICK_FILTERS: string[] = catalog.quick;
export const CATEGORIES = catalog.categories as FilterCategory[];

interface Place { option: FilterOption; section?: FilterSection; category?: FilterCategory }
const PLACES = new Map<string, Place>();
for (const category of CATEGORIES) {
  for (const section of category.sections) {
    for (const option of section.options) PLACES.set(option.key, { option, section, category });
  }
}
for (const option of catalog.hidden as FilterOption[]) PLACES.set(option.key, { option });

// Older names of the same filter (identical on the server): read as the new
// one, so it widens with its section and its chip says where it is.
const ALIASES: Record<string, string> = { large_files: "size_large" };

/** Filter tokens from an address, a saved view or an older version. */
export function normalizeFilters(tokens: string[]): string[] {
  const out = tokens.map(token => {
    const excluded = token.startsWith("!");
    const key = excluded ? token.slice(1) : token;
    return key in ALIASES ? (excluded ? "!" : "") + ALIASES[key] : token;
  });
  return [...new Set(out)];
}

/** An option's label; an unknown id (an old bookmark) as itself. */
export function optionLabel(key: string, t: T): string {
  const option = PLACES.get(key)?.option;
  if (!option) return key.replace(/_/g, " ");
  return option.labelKey ? t(option.labelKey) : option.label ?? key;
}

/** A filter-string token as a chip: "Codec: x264", "Codec: not x264", "Advanced: 2 conditions".
 *  No section prefix when the label already names it ("Audio cleanup", not "Audio: Audio cleanup"). */
export function chipLabel(token: string, t: T): string {
  if (token.startsWith("adv:")) {
    return t("scanner:filters.advancedLabel", { count: decodeAdvanced(token)?.p.length ?? 0 });
  }
  const excluded = token.startsWith("!");
  const key = excluded ? token.slice(1) : token;
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
    for (const option of section.options) {
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
