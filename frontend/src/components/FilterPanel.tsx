import { useEffect, useRef, type CSSProperties, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { optionLabel, type FilterCategory } from "../filterCatalog";

interface FilterPanelProps {
  category: FilterCategory;
  active: string[];
  counts?: Record<string, number>;
  onToggle: (key: string, exclude: boolean) => void;
  onClose: () => void;
}

const FOCUSABLE = "input:not(:disabled), button:not(:disabled)";

/**
 * A category's options (v0.10.0): a checkbox (Alt-click excludes), the count
 * of files clicking it would give, and ⊘ to exclude. Options with no files
 * are greyed out unless they're on. Tab stays inside; Escape closes.
 */
export default function FilterPanel({ category, active, counts, onToggle, onClose }: FilterPanelProps) {
  const { t } = useTranslation(["scanner"]);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ref.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Tab" || !ref.current) return;
    const items = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const several = category.sections.length > 1;
  const style = { "--panel-width": `${Math.min(category.sections.length, 3) * 210 + 26}px` } as CSSProperties;
  return (
    <div ref={ref} className="filter-panel" role="dialog" style={style} onKeyDown={onKeyDown}
      aria-label={t("scanner:filterBar.panel", { category: t(category.labelKey) })}>
      <div className="filter-panel-sections">
        {category.sections.map(section => (
          <fieldset key={section.key} className="filter-section">
            <legend className={several ? "filter-section-title" : "sr-only"}
              title={section.hintKey ? t(section.hintKey) : undefined}>
              {t(section.labelKey)}
            </legend>
            {section.options.map(option => {
              const label = optionLabel(option.key, t);
              const included = active.includes(option.key);
              const excluded = active.includes("!" + option.key);
              const count = counts?.[option.key];
              const empty = count === 0 && !included && !excluded;
              return (
                <div key={option.key}
                  className={`filter-option${excluded ? " excluded" : ""}${empty ? " empty" : ""}`}>
                  <label className="filter-option-main">
                    <input type="checkbox" checked={included} disabled={empty}
                      onChange={e => onToggle(option.key, (e.nativeEvent as MouseEvent).altKey === true)} />
                    <span className="filter-option-label">{label}</span>
                    {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
                    <span className="filter-option-count">{count == null ? "…" : count.toLocaleString()}</span>
                  </label>
                  <button type="button" className="filter-option-exclude" aria-pressed={excluded} disabled={empty}
                    aria-label={t(excluded ? "scanner:filterBar.unexclude" : "scanner:filterBar.exclude", { label })}
                    title={t(excluded ? "scanner:filterBar.unexclude" : "scanner:filterBar.exclude", { label })}
                    onClick={() => onToggle(option.key, true)}>
                    ⊘
                  </button>
                </div>
              );
            })}
          </fieldset>
        ))}
      </div>
      <div className="filter-panel-rule">{t("scanner:filterBar.rule")}</div>
    </div>
  );
}
