import i18n from "i18next";
import { initReactI18next } from "react-i18next";

// UI localization (v0.9.131). Strings live in locales/<lang>/<namespace>.json —
// one namespace per screen so they stay small and self-contained. Every file
// under locales/ is picked up automatically; adding a language is just adding a
// folder plus an entry in LANGUAGES below. English is the default and the
// fallback, so a missing key in any other language shows the English text
// rather than a raw key.

export const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "es", label: "Español" },
] as const;

export type LanguageCode = (typeof LANGUAGES)[number]["code"];

const STORAGE_KEY = "shrinkerr_language";
const DEFAULT_LANGUAGE: LanguageCode = "en";

const modules = import.meta.glob<{ default: Record<string, unknown> }>(
  "./locales/*/*.json",
  { eager: true },
);

const resources: Record<string, Record<string, Record<string, unknown>>> = {};
for (const [path, mod] of Object.entries(modules)) {
  const m = path.match(/\.\/locales\/([^/]+)\/([^/]+)\.json$/);
  if (!m) continue;
  const [, lang, ns] = m;
  (resources[lang] ??= {})[ns] = mod.default;
}

function storedLanguage(): LanguageCode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v && LANGUAGES.some((l) => l.code === v)) return v as LanguageCode;
  } catch {
    /* storage unavailable — fall back to the default */
  }
  return DEFAULT_LANGUAGE;
}

i18n.use(initReactI18next).init({
  resources,
  lng: storedLanguage(),
  fallbackLng: DEFAULT_LANGUAGE,
  defaultNS: "common",
  ns: Object.keys(resources[DEFAULT_LANGUAGE] ?? { common: {} }),
  interpolation: { escapeValue: false }, // React already escapes
});

document.documentElement.lang = i18n.language;

export function setLanguage(code: LanguageCode) {
  i18n.changeLanguage(code);
  document.documentElement.lang = code;
  try {
    localStorage.setItem(STORAGE_KEY, code);
  } catch {
    /* non-fatal */
  }
}

export default i18n;
