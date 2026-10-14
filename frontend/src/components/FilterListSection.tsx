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
 * "Show all" and the search read every value from the server. A searched
 * list (actors) asks the server for its top values and searches there.
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
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  // The server's answer, with the filter it answers: another filter's isn't this one's.
  const [all, setAll] = useState<{ filter: string; values: FacetValue[] | null } | null>(null);
  const q = query.trim();
  const searching = q !== "";
  const searched = section.searched === true;
  const asked = searched ? q : "";  // a searched list's search is the server's
  const allKey = `${filter}\n${asked}`;  // what `all` answers

  const selected = active.flatMap(token => {
    const excluded = token.startsWith("!");
    const parsed = parseListToken(excluded ? token.slice(1) : token);
    return parsed && parsed.facet === facet ? [{ value: parsed.value, excluded }] : [];
  });
  // Values ticked, unticked or excluded here stay at the top until the menu
  // closes: one unticked fell past the first 8 (or out of a search) and the
  // focus with it.
  const [kept, setKept] = useState(() => selected.map(s => s.value));

  // Every value, for "Show all" and the search box; a searched list's top
  // values or matches, asked for once typing pauses.
  useEffect(() => {
    if (!searched && !expanded && !searching) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      getFacetValues(facet, filter, ctrl.signal, asked)
        .then(r => setAll({ filter: allKey, values: r.values })).catch(() => {});
    }, searched && searching ? 250 : 0);
    return () => { window.clearTimeout(timer); ctrl.abort(); };
  }, [facet, filter, expanded, searching, version, searched, asked, allKey]);

  // A searched list's last answer stands in, dimmed, while the next loads.
  const top = searched ? all?.values ?? null : values ?? lastValues;
  const allNow = all?.filter === allKey ? all.values : null;
  const needle = q.toLocaleLowerCase();
  const full = expanded || searching || searched;
  const pool = (full ? allNow : null) ?? top ?? [];
  const fresh = full ? allNow !== null : values != null;  // the rows answer this filter: not dimmed

  const group = [...kept, ...selected.map(s => s.value).filter(v => !kept.includes(v))];
  let rest = pool.filter(v => !group.includes(v.value));
  const label = (v: FacetValue) => facetValueLabel(facet, v.value, t, v.label);
  if (needle) rest = rest.filter(v => label(v).toLocaleLowerCase().includes(needle));
  const arranged = arrange(facet, rest, full);
  const shown = full ? arranged : arranged.slice(0, SHOWN);
  const more = !searched && !searching && (expanded || arrange(facet, rest, true).length > shown.length);

  // A search box over a long list (once shown, it stays while the menu's open).
  const [searchable, setSearchable] = useState(false);
  if (!searchable && (searched || searching || (allNow ?? top ?? []).length > SHOWN)) setSearchable(true);
  const searchLabel = t("scanner:facets.searchIn", { section: t(section.chipLabelKey ?? section.labelKey) });

  // A ticked value's count and label: the top values always carry it (at 0
  // when no file has it).
  const known = (value: string) =>
    [allNow, values, pool, lastValues].flatMap(list => list ?? []).find(v => v.value === value);
  const toggle = (value: string, exclude: boolean) => {
    setKept(k => (k.includes(value) ? k : [...k, value]));
    onToggle(listToken(facet, value), exclude);
  };
  const row = (value: string, count: number | undefined, excluded: boolean, included: boolean, display: string) => {
    const key = listToken(facet, value);
    // A row kept on screen after unticking stays usable, though it has no
    // files now: disabled under the focus, the keyboard lost its place.
    const empty = count === 0 && !included && !excluded && !kept.includes(value);
    return (
      <div key={key} className={`filter-option${excluded ? " excluded" : ""}${empty ? " empty" : ""}`}>
        <label className="filter-option-main">
          <input type="checkbox" checked={included} disabled={empty}
            onChange={e => toggle(value, (e.nativeEvent as MouseEvent).altKey === true)} />
          {/* The whole name when it's cut short ("United States of A…"). */}
          <span className="filter-option-label" title={display}>{display}</span>
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

  const counting = top == null;
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
      {searching && shown.length === 0 && allNow && (
        <p className="filter-section-hint">{t("scanner:facets.noMatches")}</p>
      )}
      {top?.length === 0 && group.length === 0 && !searching && (
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
