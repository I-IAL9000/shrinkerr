import { useEffect, useId, useLayoutEffect, useRef, type CSSProperties, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { optionLabel, type FilterCategory } from "../filterCatalog";
import { fmtNum } from "../fmt";

interface FilterPanelProps {
  category: FilterCategory;
  active: string[];
  counts?: Record<string, number>;
  lastCounts?: Record<string, number>;  // the last counts shown, while a new filter's load
  onToggle: (key: string, exclude: boolean) => void;
  onClose: () => void;
}

const FOCUSABLE = "input:not(:disabled), button:not(:disabled)";

/**
 * A category's options (v0.10.0): a checkbox (Alt-click excludes), the count
 * of files clicking it would give, and ⊘ to exclude. Options with no files
 * are greyed out unless they're on. Tab stays inside; Escape closes.
 */
export default function FilterPanel({ category, active, counts, lastCounts, onToggle, onClose }: FilterPanelProps) {
  const { t } = useTranslation(["scanner"]);
  const ref = useRef<HTMLDivElement>(null);
  const fitRef = useRef<() => void>(() => {});
  const id = useId();

  useLayoutEffect(() => {
    const panel = ref.current;
    if (!panel) return;
    // Keep it inside the bar: a category near the end of the bar would open
    // past its right edge, so shift it left, but never past the bar's left
    // edge; and no wider than the bar (a three-column panel is, beside the
    // sidebar in a small window). On phones it spans the row of buttons.
    let opened: number | null = null;  // phones: where it opened, below the row's top
    const fit = () => {
      panel.style.left = panel.style.maxWidth = panel.style.top = "";
      const bar = panel.closest(".filter-bar")?.getBoundingClientRect();
      if (!bar) return;
      if (panel.getBoundingClientRect().width > bar.width + 0.5) panel.style.maxWidth = `${bar.width}px`;
      const rect = panel.getBoundingClientRect();
      const over = rect.right - bar.right;
      const room = rect.left - bar.left;
      if (over > 0.5 && room > 0.5) panel.style.left = `${-Math.min(over, room)}px`;
      // Phones: the row re-wraps as a ticked category's summary and the
      // counts change, and moved the panel under the finger. It stays where
      // it opened, over the row's last line if the row grew.
      // (The offset in the row, not the screen: the page scrolls inside.)
      if (panel.parentElement && getComputedStyle(panel.parentElement).position === "static") {
        if (opened === null) opened = panel.offsetTop;
        else if (Math.abs(panel.offsetTop - opened) > 0.5) panel.style.top = `${opened}px`;
      }
    };
    fitRef.current = fit;
    fit();
    // On a phone the bar is tall and the panel opens below the fold.
    panel.scrollIntoView({ block: "nearest" });
    // Into the panel: its first option, or the panel itself when every option
    // is greyed out (a screen reader still announces it).
    (panel.querySelector<HTMLElement>(FOCUSABLE) ?? panel).focus({ preventScroll: true });
    // Again when the bar's layout changes under it (it re-wraps as counts
    // change), not only when the window does.
    const resized = () => { opened = null; fit(); };  // a turned phone: open it afresh
    window.addEventListener("resize", resized);
    const row = panel.closest(".filter-bar")?.querySelector(".filter-bar-row");
    const watch = row && typeof ResizeObserver !== "undefined" ? new ResizeObserver(fit) : null;
    if (row) watch?.observe(row);
    return () => {
      window.removeEventListener("resize", resized);
      watch?.disconnect();
    };
  }, []);

  // After each render too: a tick changes its category's summary and the
  // counts, which re-wrap the row (before the paint; the observer above
  // waits for a frame, which a hidden page never draws).
  useLayoutEffect(() => fitRef.current());

  // Untick an option whose count then drops to 0 and it's disabled under the
  // focus, which falls to the page: keep it in the panel.
  useEffect(() => {
    const here = document.activeElement as HTMLButtonElement | null;
    if (!here || here === document.body || here.disabled) ref.current?.focus({ preventScroll: true });
  });

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Tab" || !ref.current) return;
    const items = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) {
      e.preventDefault();  // the panel itself has the focus: stay in it
      return;
    }
    const first = items[0], last = items[items.length - 1];
    const here = document.activeElement;
    if (e.shiftKey && (here === first || here === ref.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && here === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const several = category.sections.length > 1;
  const style = { "--panel-width": `${Math.min(category.sections.length, 3) * 210 + 26}px` } as CSSProperties;
  return (
    <div ref={ref} className="filter-panel" role="dialog" tabIndex={-1} style={style} onKeyDown={onKeyDown}
      aria-label={t("scanner:filterBar.panel", { category: t(category.labelKey) })}>
      <div className="filter-panel-sections">
        {category.sections.map(section => {
          const hintId = section.hintKey ? `${id}-${section.key}-hint` : undefined;
          return (
            <fieldset key={section.key} className="filter-section" aria-describedby={hintId}>
              <legend className={several ? "filter-section-title" : "sr-only"}>{t(section.labelKey)}</legend>
              {section.hintKey && <p id={hintId} className="filter-section-hint">{t(section.hintKey)}</p>}
              {section.options.map(option => {
                const label = optionLabel(option.key, t);
                const included = active.includes(option.key);
                const excluded = active.includes("!" + option.key);
                const count = counts?.[option.key];
                const shown = count ?? lastCounts?.[option.key];  // dimmed while the new count loads
                const empty = count === 0 && !included && !excluded;
                return (
                  <div key={option.key}
                    className={`filter-option${excluded ? " excluded" : ""}${empty ? " empty" : ""}`}>
                    <label className="filter-option-main">
                      <input type="checkbox" checked={included} disabled={empty}
                        onChange={e => onToggle(option.key, (e.nativeEvent as MouseEvent).altKey === true)} />
                      <span className="filter-option-label">{label}</span>
                      {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
                      <span className={`filter-option-count${count == null ? " stale" : ""}`}>
                      {shown == null ? "…" : fmtNum(shown)}
                    </span>
                    </label>
                    <button type="button" className="filter-option-exclude" aria-pressed={excluded} disabled={empty}
                      aria-label={t("scanner:filterBar.exclude", { label })}
                      title={t("scanner:filterBar.exclude", { label })}
                      onClick={() => onToggle(option.key, true)}>
                      ⊘
                    </button>
                  </div>
                );
              })}
            </fieldset>
          );
        })}
      </div>
      <div className="filter-panel-rule">{t("scanner:filterBar.rule")}</div>
    </div>
  );
}
