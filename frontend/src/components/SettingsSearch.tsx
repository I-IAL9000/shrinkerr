import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { SETTINGS_SECTIONS, type SettingsSectionId } from "../settingsSections";

// Settings search (v0.10.0): finds a setting by its label or help text in
// any sub-page. The index is the Settings translations, by key prefix.
const SOURCES: [ns: string, prefix: string, section: SettingsSectionId][] = [
  ["settingsMedia", "directories", "media"],
  ["settingsRenaming", "", "media"],
  ["settingsSystem", "renaming", "media"],
  ["settingsMedia", "video", "video"],
  ["settingsMedia", "audio", "audio"],
  ["settingsMedia", "subtitles", "audio"],
  ["settingsSystem", "automation", "automation"],
  ["settingsSystem", "webhooks", "automation"],
  ["settingsSystem", "script", "automation"],
  ["settingsIntegrations", "rules", "automation"],
  ["settingsIntegrations", "conditions", "automation"],
  ["settingsIntegrations", "connections", "integrations"],
  ["settingsIntegrations", "tmdb", "integrations"],
  ["settingsIntegrations", "plex", "integrations"],
  ["settingsIntegrations", "jellyfin", "integrations"],
  ["settingsIntegrations", "emby", "integrations"],
  ["settingsIntegrations", "arr", "integrations"],
  ["settingsIntegrations", "downloaders", "integrations"],
  ["settingsSystem", "notifications", "notifications"],
  ["settingsSystem", "auth", "system"],
  ["settingsSystem", "backups", "system"],
  ["settingsSystem", "shortcuts", "system"],
  ["settingsSystem", "updates", "system"],
  ["settingsSystem", "support", "system"],
  ["settings", "ui", "system"],
];
// Messages, not settings.
const SKIP_KEY = /(toast|error|failed|confirm|placeholder|saving|saved|^save$|\.save$)/i;

interface Entry { section: SettingsSectionId; text: string }

/** Display text of a catalog string: tags and nesting out, placeholders as "…". */
export function plainText(s: string): string {
  return s.replace(/<\/?[\w-]+[^>]*>/g, "").replace(/\$t\([^)]*\)/g, "").replace(/\{\{[^}]+\}\}/g, "…").trim();
}

function leaves(node: unknown, path: string, out: [string, string][]) {
  if (typeof node === "string") out.push([path, node]);
  else if (node && typeof node === "object") {
    for (const [k, v] of Object.entries(node)) leaves(v, path ? `${path}.${k}` : k, out);
  }
}

export default function SettingsSearch({ onPick }: { onPick: (section: SettingsSectionId, text: string) => void }) {
  const { t, i18n } = useTranslation(["common", "nav"]);
  const [query, setQuery] = useState("");

  const index = useMemo(() => {
    const entries: Entry[] = [];
    const seen = new Set<string>();
    for (const [ns, prefix, section] of SOURCES) {
      const bundle = i18n.getResourceBundle(i18n.language, ns) || i18n.getResourceBundle("en", ns);
      const node = prefix ? bundle?.[prefix] : bundle;
      const found: [string, string][] = [];
      leaves(node, "", found);
      for (const [key, value] of found) {
        if (SKIP_KEY.test(key) || /_(one|other|many|zero|two|few)$/.test(key)) continue;
        const text = plainText(value);
        const id = section + text.toLowerCase();
        if (text.length < 3 || seen.has(id)) continue;
        seen.add(id);
        entries.push({ section, text });
      }
    }
    return entries;
  }, [i18n, i18n.language]);

  const q = query.trim().toLowerCase();
  // Names before help text: a match at the start, then the shortest.
  const rank = (e: Entry) => (e.text.toLowerCase().startsWith(q) ? 0 : 1);
  const results = q.length < 2 ? [] : index
    .filter(e => e.text.toLowerCase().includes(q))
    .sort((a, b) => rank(a) - rank(b) || a.text.length - b.text.length)
    .slice(0, 12);
  const sectionLabel = (id: SettingsSectionId) => t(SETTINGS_SECTIONS.find(s => s.id === id)!.labelKey);
  const pick = (e: Entry) => { setQuery(""); onPick(e.section, e.text); };

  return (
    <div className="settings-search">
      <input
        type="search"
        value={query}
        onChange={e => setQuery(e.target.value)}
        onKeyDown={e => {
          if (e.key === "Enter" && results[0]) { e.preventDefault(); pick(results[0]); }
          if (e.key === "Escape") setQuery("");
        }}
        placeholder={t("common:settingsSearch.placeholder")}
        aria-label={t("common:settingsSearch.placeholder")}
      />
      {q.length >= 2 && (
        <div className="settings-search-results" role="list">
          {results.length === 0 && <div className="settings-search-empty">{t("common:settingsSearch.none")}</div>}
          {results.map((r, i) => (
            <button key={i} type="button" role="listitem" onClick={() => pick(r)}>
              <span className="settings-search-text">{r.text}</span>
              <span className="settings-search-section">{sectionLabel(r.section)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
