import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import FilterPanel from "./FilterPanel";
import { CATEGORIES, QUICK_FILTERS, chipLabel, optionLabel, selectedIn, type FilterCategory } from "../filterCatalog";
import { fmtNum } from "../fmt";

interface FilterBarProps {
  activeFilters: string[];
  counts?: Record<string, number>;
  matching: number | null;  // files the whole filter matches; null while counting
  onToggle: (key: string, exclude: boolean) => void;
  onRemove: (token: string) => void;
  onClear: () => void;
}

function shortCount(n: number): string {
  return n > 99999 ? `${(n / 1000).toFixed(0)}k` : fmtNum(n);
}

/**
 * The Scanner's filter bar (v0.10.0): quick pills, a menu per category (see
 * FilterPanel) and the active filters as chips. Within a section, ticking
 * more shows more; across sections, files must match all (the server's
 * scan_filters.py decides; filterCatalog.json lays it out).
 */
export default function FilterBar({ activeFilters, counts, matching, onToggle, onRemove, onClear }: FilterBarProps) {
  const { t } = useTranslation(["scanner"]);
  const [open, setOpen] = useState<string | null>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const chipButtons = useRef(new Map<string, HTMLButtonElement>());
  const clearRef = useRef<HTMLButtonElement>(null);
  const active = [...new Set(activeFilters.filter(f => f !== "all"))];
  // The last count shown for each option, kept on screen (dimmed) while a new
  // filter's counts load: the badges vanishing re-wrapped the whole bar.
  const lastCounts = useRef<Record<string, number>>({});
  for (const [key, n] of Object.entries(counts ?? {})) lastCounts.current[key] = n;

  const close = () => {
    const key = open;
    setOpen(null);
    if (key) buttons.current.get(key)?.focus();
  };

  const matchingText = matching == null
    ? t("scanner:filterBar.counting")
    : t("scanner:filterBar.matching", { count: matching, n: shortCount(matching) });

  const firstPill = () => barRef.current?.querySelector<HTMLButtonElement>(".filter-bar-row button:not(:disabled)");

  // Where the focus goes once the chips have re-rendered: the removed chip's
  // × (or Clear all) had it. In an effect, not a frame callback, which a
  // hidden or throttled page never runs.
  const pendingFocus = useRef<(() => HTMLElement | null | undefined) | null>(null);
  const counting = active.length > 0 && matching == null;
  useEffect(() => {
    const target = pendingFocus.current;
    if (!target) return;
    const el = target();
    // A quick pill greys out only when its new count arrives: wait for it.
    if (el || !counting) pendingFocus.current = null;
    el?.focus();
  });

  // On to the next chip's × (or the previous one's, or Clear all).
  const removeChip = (index: number) => {
    const near = [active[index + 1], active[index - 1]];
    pendingFocus.current = () =>
      near.map(token => (token ? chipButtons.current.get(token) : undefined)).find(el => el?.isConnected)
      ?? (clearRef.current?.isConnected ? clearRef.current : firstPill());
    onRemove(active[index]);
  };

  // Clear all goes with the chips: on to the first quick pill.
  const clearAll = () => {
    pendingFocus.current = firstPill;
    onClear();
  };

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) setOpen(null);
    };
    const esc = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const categoryButton = (category: FilterCategory) => {
    const selected = selectedIn(category, active);
    const hasFiles = category.sections.some(s => s.options.some(o => (counts?.[o.key] ?? 1) > 0));
    const dimmed = !hasFiles && selected.length === 0;
    const names = selected.map(s =>
      s.excluded ? t("scanner:filters.notLabel", { label: optionLabel(s.key, t) }) : optionLabel(s.key, t));
    const shown = names.slice(0, 2).join(", ")
      + (names.length > 2 ? " " + t("scanner:filterBar.more", { count: names.length - 2 }) : "");
    const title = t(category.labelKey);
    const isOpen = open === category.key;
    return (
      <div key={category.key} className="filter-category">
        <button
          ref={el => { if (el) buttons.current.set(category.key, el); }}
          className={`filter-pill filter-category-button${selected.length ? " active" : ""}${dimmed ? " dimmed" : ""}`}
          aria-haspopup="dialog" aria-expanded={isOpen}
          title={dimmed ? t("scanner:filterBar.noFiles") : undefined}
          onClick={() => setOpen(isOpen ? null : category.key)}>
          <span>{selected.length ? t("scanner:filterBar.summary", { category: title, selected: shown }) : title}</span>
          {dimmed && <span className="sr-only">{` — ${t("scanner:filterBar.noFiles")}`}</span>}
          <span aria-hidden="true" className="filter-category-caret">▾</span>
        </button>
        {isOpen && (
          <FilterPanel category={category} active={active} counts={counts} lastCounts={lastCounts.current}
            onToggle={onToggle} onClose={close} />
        )}
      </div>
    );
  };

  return (
    <div ref={barRef} className="filter-bar">
      <div className="filter-bar-row">
        {QUICK_FILTERS.map(key => {
          const included = active.includes(key);
          const excluded = active.includes("!" + key);
          const count = counts?.[key];
          const shown = count ?? lastCounts.current[key];
          const empty = count === 0 && !included && !excluded;
          return (
            <button key={key}
              className={`filter-pill${included ? " active" : ""}${excluded ? " excluded" : ""}${empty ? " dimmed" : ""}`}
              aria-pressed={included || excluded} disabled={empty}
              title={t("scanner:filters.excludeHint")}
              onClick={e => {
                // Turned off, it may grey out (no files) under the focus.
                const pill = e.currentTarget;
                pendingFocus.current = () => (pill.disabled ? firstPill() : null);
                onToggle(key, e.altKey);
              }}>
              <span className="filter-pill-label">{optionLabel(key, t)}</span>
              {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
              {shown != null && (
                <span className={`filter-pill-count${count == null ? " stale" : ""}`}>{shortCount(shown)}</span>
              )}
            </button>
          );
        })}
        <span className="filter-bar-divider" aria-hidden="true" />
        {CATEGORIES.map(categoryButton)}
      </div>
      {/* Mounted before the chips appear, so the first total is announced too;
          empty while counting, so each click is announced once. */}
      <span className="sr-only" aria-live="polite">{active.length > 0 && !counting ? matchingText : ""}</span>
      {active.length > 0 && (
        <div className="filter-chips">
          {active.map((token, index) => {
            const label = chipLabel(token, t);
            return (
              <span key={token} className={`filter-chip${token.startsWith("!") ? " excluded" : ""}`}>
                {label}
                <button type="button" className="filter-chip-remove"
                  ref={el => { if (el) chipButtons.current.set(token, el); else chipButtons.current.delete(token); }}
                  aria-label={t("scanner:filterBar.remove", { label })} onClick={() => removeChip(index)}>
                  ×
                </button>
              </span>
            );
          })}
          <span className="filter-chips-matching" aria-hidden="true">{matchingText}</span>
          <button type="button" ref={clearRef} className="filter-chips-clear" onClick={clearAll}>
            {t("scanner:filterBar.clearAll")}
          </button>
        </div>
      )}
    </div>
  );
}
