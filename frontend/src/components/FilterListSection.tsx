import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getFacetValues, type FacetValue } from "../api";
import { listToken, parseListToken, type FilterSection } from "../filterCatalog";
import { facetValueLabel } from "../facetLabels";
import { fmtNum } from "../fmt";

const SHOWN = 8;  // values before "Show all"

/**
 * A list in a filter menu (v0.10.0): a search box, the values ticked or
 * excluded, then the most common values under the current filter, each a
 * checkbox (Alt-click excludes), its count and ⊘. "Show all" and the search
 * read every value from the server; folders are searched, not listed.
 */
export default function FilterListSection({ section, values, lastValues, active, filter, onToggle }: {
  section: FilterSection;
  values: FacetValue[] | null | undefined;  // the top values (null: counting)
  lastValues?: FacetValue[];  // the last ones shown, dimmed while a new filter's load
  active: string[];
  filter: string;
  onToggle: (key: string, exclude: boolean) => void;
}) {
  const { t } = useTranslation(["scanner", "settingsMedia"]);
  const facet = section.facet as string;
  const isFolder = facet === "folder";
  const [query, setQuery] = useState("");
  const [all, setAll] = useState<FacetValue[] | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [found, setFound] = useState<FacetValue[] | null>(null);  // the last folder search's
  const searching = query !== "";

  // Every value, for "Show all" and the search box (not folders).
  useEffect(() => {
    if (isFolder || (!expanded && !searching)) return;
    const ctrl = new AbortController();
    getFacetValues(facet, filter, "", ctrl.signal).then(r => setAll(r.values)).catch(() => {});
    return () => ctrl.abort();
  }, [facet, filter, isFolder, expanded, searching]);

  // Folders: the server searches, once the typing pauses.
  useEffect(() => {
    const q = query.trim();
    if (!isFolder || !q) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      getFacetValues(facet, filter, q, ctrl.signal).then(r => setFound(r.values)).catch(() => {});
    }, 250);
    return () => { window.clearTimeout(timer); ctrl.abort(); };
  }, [facet, filter, isFolder, query]);

  const selected = active.flatMap(token => {
    const excluded = token.startsWith("!");
    const parsed = parseListToken(excluded ? token.slice(1) : token);
    return parsed && parsed.facet === facet ? [{ value: parsed.value, excluded }] : [];
  });
  const searched = query.trim() ? found : null;  // the box emptied: none
  const stale = values == null;
  const top = values ?? lastValues;
  const label = (v: FacetValue) => facetValueLabel(facet, v.value, t, v.label);
  const pool = isFolder ? (searched ?? []) : (searching || expanded) && all ? all : (top ?? []);
  const needle = query.trim().toLocaleLowerCase();
  let rest = pool.filter(v => !selected.some(s => s.value === v.value));
  if (needle && !isFolder) rest = rest.filter(v => label(v).toLocaleLowerCase().includes(needle));
  if (facet === "alang" || facet === "slang") {
    rest = [...rest.filter(v => v.value === "und"), ...rest.filter(v => v.value !== "und")];
  }
  const shown = expanded || needle ? rest : rest.slice(0, SHOWN);

  // A ticked value's count and label: the top values always carry it (at 0
  // when no file has it); a folder's only if a search found it.
  const known = (value: string) => [...(top ?? []), ...(all ?? []), ...(searched ?? [])].find(v => v.value === value);
  const row = (value: string, count: number | undefined, excluded: boolean, included: boolean, display: string) => {
    const key = listToken(facet, value);
    const empty = count === 0 && !included && !excluded;
    return (
      <div key={key} className={`filter-option${excluded ? " excluded" : ""}${empty ? " empty" : ""}`}>
        <label className="filter-option-main">
          <input type="checkbox" checked={included} disabled={empty}
            onChange={e => onToggle(key, (e.nativeEvent as MouseEvent).altKey === true)} />
          <span className="filter-option-label" title={isFolder ? value : undefined}>{display}</span>
          {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
          <span className={`filter-option-count${stale ? " stale" : ""}`}>
            {count == null ? (top == null ? "…" : "") : fmtNum(count)}
          </span>
        </label>
        <button type="button" className="filter-option-exclude" aria-pressed={excluded} disabled={empty}
          aria-label={t("scanner:filterBar.exclude", { label: display })}
          title={t("scanner:filterBar.exclude", { label: display })}
          onClick={() => onToggle(key, true)}>
          ⊘
        </button>
      </div>
    );
  };

  return (
    <div className="filter-list">
      <input type="search" className="filter-list-search" value={query} onChange={e => setQuery(e.target.value)}
        placeholder={t(isFolder ? "scanner:facets.searchFolders" : "scanner:facets.search")}
        aria-label={t(isFolder ? "scanner:facets.searchFolders" : "scanner:facets.search")} />
      {/* One list, ticked values first: a value ticked moves up as the same
          row, so the focus stays on it. Long lists scroll below the search. */}
      <div className="filter-list-values">
        {[
          ...selected.map(s => {
            const v = known(s.value);
            return row(s.value, v?.count, s.excluded, !s.excluded, facetValueLabel(facet, s.value, t, v?.label));
          }),
          ...shown.map(v => row(v.value, v.count, false, false, label(v))),
        ]}
      </div>
      {top == null && !isFolder && <p className="filter-section-hint">{t("scanner:filterBar.counting")}</p>}
      {needle && shown.length === 0 && (isFolder ? searched : all) && (
        <p className="filter-section-hint">{t("scanner:facets.noMatches")}</p>
      )}
      {!isFolder && !needle && (expanded || rest.length > SHOWN) && (
        <button type="button" className="filter-list-more" onClick={() => setExpanded(x => !x)}>
          {t(expanded ? "scanner:facets.showFewer" : "scanner:facets.showAll")}
        </button>
      )}
    </div>
  );
}
