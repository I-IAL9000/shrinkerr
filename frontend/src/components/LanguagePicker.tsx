import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ALL_LANGUAGES } from "../languageCodes";

/** Chosen languages as removable chips, plus a search box to add one
 *  (the setup wizard; Settings has the same look). Codes are ISO 639-2. */
export default function LanguagePicker({ value, onChange, label }: {
  value: string[];
  onChange: (codes: string[]) => void;
  label: string;
}) {
  const { t } = useTranslation(["settingsMedia", "common"]);
  const [query, setQuery] = useState("");
  const name = (code: string) => t(`settingsMedia:languages.${code}`, { defaultValue: code });
  const q = query.trim().toLowerCase();
  const matches = q
    ? ALL_LANGUAGES.filter(code => !value.includes(code) && (code.includes(q) || name(code).toLowerCase().includes(q))).slice(0, 8)
    : [];
  const add = (code: string) => { onChange([...value, code]); setQuery(""); };

  return (
    <div className="language-picker">
      <div className="language-chips" aria-label={label} role="list">
        {value.map(code => (
          <span key={code} className="language-chip" role="listitem">
            {name(code)} ({code})
            <button type="button" aria-label={`${t("common:actions.remove")} ${name(code)}`}
              onClick={() => onChange(value.filter(c => c !== code))}>&times;</button>
          </span>
        ))}
      </div>
      <div style={{ position: "relative" }}>
        <input
          type="search"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && matches[0]) { e.preventDefault(); add(matches[0]); } }}
          placeholder={t("settingsMedia:audio.searchPlaceholder")}
          aria-label={label}
        />
        {matches.length > 0 && (
          <div className="language-picker-results" role="listbox" aria-label={label}>
            {matches.map(code => (
              <button key={code} type="button" role="option" aria-selected={false} onClick={() => add(code)}>
                <strong>{name(code)}</strong> <span>({code})</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
