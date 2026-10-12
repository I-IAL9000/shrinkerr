import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getFacetValues, type FacetValue } from "../api";
import { listToken, parseListToken, type FilterSection } from "../filterCatalog";
import { facetValueLabel } from "../facetLabels";
import { fmtNum } from "../fmt";

const SHOWN = 8;  // values before "Show all"; a search box above more than this

// Lists with an order of their own, read in it rather than by count.
const ORDER: Record<string, string[]> = {
  rating: ["8", "7", "6", "5", "lt5", "none"],
  achan: ["1", "2", "6", "8"],
};

/** A list's values as shown: rating and channels in their own order; years
 *  as decades, newest first, then (`full`: "Show all", a search) the single
 *  years, newest first; Unknown first in a language list; the others the
 *  most first, as the server sends them. */
function arrange(facet: string, values: FacetValue[], full: boolean): FacetValue[] {
  const order = ORDER[facet];
  if (order) {
    const rank = (v: FacetValue) => (order.includes(v.value) ? order.indexOf(v.value) : order.length);
    return [...values].sort((a, b) => rank(a) - rank(b));
  }
  if (facet === "year") {
    const newest = (a: FacetValue, b: FacetValue) => b.value.localeCompare(a.value);  // "2020s", "2010s"…
    const decades = values.filter(v => v.value.endsWith("s")).sort(newest);
    return full ? [...decades, ...values.filter(v => !v.value.endsWith("s")).sort(newest)] : decades;
  }
  if (facet === "alang" || facet === "slang") {
    return [...values.filter(v => v.value === "und"), ...values.filter(v => v.value !== "und")];
  }
  return values;
}

/**
 * A list in a filter menu (v0.10.0): the values ticked or excluded, then
 * the most common values under the current filter, each a checkbox
 * (Alt-click excludes), its count and ⊘; a search box over a long list.
 * "Show all" and the search read every value from the server; folders are
 * searched, not listed.
 */
export default function FilterListSection({ section, values, lastValues, version, active, filter, onToggle }: {
  section: FilterSection;
  values: FacetValue[] | null | undefined;  // the top values (null: counting)
  lastValues?: FacetValue[];  // the last ones shown, dimmed while a new filter's load
  version: number;  // new top values (a refresh during a scan too)
  active: string[];
  filter: string;
  onToggle: (key: string, exclude: boolean) => void;
}) {
  const { t } = useTranslation(["scanner", "settingsMedia"]);
  const facet = section.facet as string;
  const isFolder = facet === "folder";
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  // The server's answers, with what they answer: another filter's (or an
  // earlier search's) aren't this one's.
  const [all, setAll] = useState<{ filter: string; values: FacetValue[] | null } | null>(null);
  const [found, setFound] = useState<{ filter: string; q: string; values: FacetValue[] | null } | null>(null);
  const q = query.trim();
  const searching = q !== "";

  const selected = active.flatMap(token => {
    const excluded = token.startsWith("!");
    const parsed = parseListToken(excluded ? token.slice(1) : token);
    return parsed && parsed.facet === facet ? [{ value: parsed.value, excluded }] : [];
  });
  // Values ticked, unticked or excluded here stay at the top until the menu
  // closes: one unticked fell past the first 8 (or out of a search) and the
  // focus with it.
  const [kept, setKept] = useState(() => selected.map(s => s.value));

  // Every value, for "Show all" and the search box (not folders).
  useEffect(() => {
    if (isFolder || (!expanded && !searching)) return;
    const ctrl = new AbortController();
    getFacetValues(facet, filter, "", ctrl.signal).then(r => setAll({ filter, values: r.values })).catch(() => {});
    return () => ctrl.abort();
  }, [facet, filter, isFolder, expanded, searching, version]);

  // Folders: the server searches, once the typing pauses.
  useEffect(() => {
    if (!isFolder || !q) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      getFacetValues(facet, filter, q, ctrl.signal).then(r => setFound({ filter, q, values: r.values })).catch(() => {});
    }, 250);
    return () => { window.clearTimeout(timer); ctrl.abort(); };
  }, [facet, filter, isFolder, q, version]);

  const top = values ?? lastValues;
  const allNow = all?.filter === filter ? all.values : null;
  const hits = found?.filter === filter && found.q === q ? found : null;  // this search's answer
  const needle = q.toLocaleLowerCase();
  const full = expanded || searching;
  let pool: FacetValue[];
  let fresh: boolean;  // the rows answer this filter (and this search): not dimmed
  if (isFolder) {
    // While a search runs, the last one's folders that still match, dimmed.
    pool = hits ? hits.values ?? [] : searching
      ? (found?.values ?? []).filter(v => v.value.toLocaleLowerCase().includes(needle)) : [];
    fresh = !searching || hits !== null;
  } else {
    pool = (full ? allNow : null) ?? top ?? [];
    fresh = full ? allNow !== null : values != null;
  }

  const group = [...kept, ...selected.map(s => s.value).filter(v => !kept.includes(v))];
  let rest = pool.filter(v => !group.includes(v.value));
  const label = (v: FacetValue) => facetValueLabel(facet, v.value, t, v.label);
  if (needle && !isFolder) rest = rest.filter(v => label(v).toLocaleLowerCase().includes(needle));
  const arranged = arrange(facet, rest, full);
  const shown = full ? arranged : arranged.slice(0, SHOWN);
  const more = !isFolder && !searching && (expanded || arrange(facet, rest, true).length > shown.length);

  // A search box over a long list (once shown, it stays while the menu's open).
  const [searchable, setSearchable] = useState(false);
  if (!searchable && (isFolder || searching || (allNow ?? top ?? []).length > SHOWN)) setSearchable(true);
  const sectionName = t(section.chipLabelKey ?? section.labelKey);
  const searchLabel = isFolder ? t("scanner:facets.searchFolders") : t("scanner:facets.searchIn", { section: sectionName });

  // A ticked value's count and label: the top values always carry it (at 0
  // when no file has it); a folder's only if a search found it.
  const known = (value: string) =>
    [allNow, values, hits?.values, pool, lastValues].flatMap(list => list ?? []).find(v => v.value === value);
  const toggle = (value: string, exclude: boolean) => {
    setKept(k => (k.includes(value) ? k : [...k, value]));
    onToggle(listToken(facet, value), exclude);
  };
  const row = (value: string, count: number | undefined, excluded: boolean, included: boolean, display: string) => {
    const key = listToken(facet, value);
    const empty = count === 0 && !included && !excluded;
    return (
      <div key={key} className={`filter-option${excluded ? " excluded" : ""}${empty ? " empty" : ""}`}>
        <label className="filter-option-main">
          <input type="checkbox" checked={included} disabled={empty}
            onChange={e => toggle(value, (e.nativeEvent as MouseEvent).altKey === true)} />
          {/* The whole name when it's cut short ("United States of A…"); a folder's path. */}
          <span className="filter-option-label" title={isFolder ? value : display}>{display}</span>
          {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
          <span className={`filter-option-count${fresh ? "" : " stale"}`}>
            {count == null ? (top == null ? "…" : "") : fmtNum(count)}
          </span>
        </label>
        <button type="button" className="filter-option-exclude" aria-pressed={excluded} disabled={empty}
          aria-label={t("scanner:filterBar.exclude", { label: display })}
          title={t("scanner:filterBar.exclude", { label: display })}
          onClick={() => toggle(value, true)}>
          ⊘
        </button>
      </div>
    );
  };

  const counting = isFolder ? hits !== null && hits.values === null : top == null;
  return (
    <div className="filter-list">
      {searchable && (
        <input type="search" className="filter-list-search" value={query} onChange={e => setQuery(e.target.value)}
          placeholder={searchLabel} aria-label={searchLabel} />
      )}
      {/* One list, ticked values first: a value ticked moves up as the same
          row, so the focus stays on it. Long lists scroll below the search. */}
      <div className="filter-list-values">
        {[
          ...group.map(value => {
            const state = selected.find(s => s.value === value);
            const v = known(value);
            return row(value, v?.count, state?.excluded === true, state?.excluded === false,
              facetValueLabel(facet, value, t, v?.label));
          }),
          ...shown.map(v => row(v.value, v.count, false, false, label(v))),
        ]}
      </div>
      {counting && <p className="filter-section-hint">{t("scanner:filterBar.counting")}</p>}
      {isFolder && searching && !hits && pool.length === 0 && (
        <p className="filter-section-hint">{t("scanner:facets.searching")}</p>
      )}
      {searching && shown.length === 0 && (isFolder ? hits?.values : allNow) && (
        <p className="filter-section-hint">{t("scanner:facets.noMatches")}</p>
      )}
      {!isFolder && top?.length === 0 && group.length === 0 && !searching && (
        <p className="filter-section-hint">{t("scanner:facets.none")}</p>
      )}
      {more && (
        <button type="button" className="filter-list-more" onClick={() => setExpanded(x => !x)}>
          {t(expanded ? "scanner:facets.showFewer" : "scanner:facets.showAll")}
        </button>
      )}
    </div>
  );
}
