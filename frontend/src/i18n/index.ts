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

// English (the default and fallback) is built in; another language is
// downloaded when chosen (FE#30, v0.10.0: every language was in the app
// script).
type Catalog = { default: Record<string, unknown> };
const english = import.meta.glob<Catalog>("./locales/en/*.json", { eager: true });
const others = import.meta.glob<Catalog>(["./locales/*/*.json", "!./locales/en/*.json"]);

function parsePath(path: string): [string, string] | null {
  const m = path.match(/\.\/locales\/([^/]+)\/([^/]+)\.json$/);
  return m ? [m[1], m[2]] : null;
}

const resources: Record<string, Record<string, Record<string, unknown>>> = {};
for (const [path, mod] of Object.entries(english)) {
  const parsed = parsePath(path);
  if (parsed) (resources[parsed[0]] ??= {})[parsed[1]] = mod.default;
}

const loaded = new Set<string>([DEFAULT_LANGUAGE]);

async function loadLanguage(code: string): Promise<void> {
  if (loaded.has(code)) return;
  await Promise.all(
    Object.entries(others).map(async ([path, load]) => {
      const parsed = parsePath(path);
      if (!parsed || parsed[0] !== code) return;
      i18n.addResourceBundle(code, parsed[1], (await load()).default, true, true);
    }),
  );
  loaded.add(code);
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

const initialLanguage = storedLanguage();

i18n.use(initReactI18next).init({
  resources,
  lng: DEFAULT_LANGUAGE,
  fallbackLng: DEFAULT_LANGUAGE,
  defaultNS: "common",
  ns: Object.keys(resources[DEFAULT_LANGUAGE] ?? { common: {} }),
  interpolation: { escapeValue: false }, // React already escapes
});

/** Resolves once the stored language is ready; render after it. */
export const i18nReady: Promise<void> = (initialLanguage === DEFAULT_LANGUAGE
  ? Promise.resolve()
  : loadLanguage(initialLanguage).then(() => { i18n.changeLanguage(initialLanguage); })
).catch(() => { /* stay in English */ }).then(() => {
  document.documentElement.lang = i18n.language;
});

export async function setLanguage(code: LanguageCode) {
  await loadLanguage(code);
  await i18n.changeLanguage(code);
  document.documentElement.lang = code;
  try {
    localStorage.setItem(STORAGE_KEY, code);
  } catch {
    /* non-fatal */
  }
}

export default i18n;
