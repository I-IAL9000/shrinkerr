import i18n from "../i18n";
import { naturalCompare } from "./naturalCompare";
// ISO 639-2/B codes + English names for the manual track-language picker
// (v0.9.43). Curated common set — enough to cover typical libraries without a
// 500-entry dropdown. Codes match what mkvpropedit / ffmpeg write.
const _LANGUAGES: { code: string; iso1: string; name: string }[] = [
  { code: "eng", iso1: "en", name: "English" },
  { code: "spa", iso1: "es", name: "Spanish" },
  { code: "fre", iso1: "fr", name: "French" },
  { code: "ger", iso1: "de", name: "German" },
  { code: "ita", iso1: "it", name: "Italian" },
  { code: "por", iso1: "pt", name: "Portuguese" },
  { code: "dut", iso1: "nl", name: "Dutch" },
  { code: "rus", iso1: "ru", name: "Russian" },
  { code: "pol", iso1: "pl", name: "Polish" },
  { code: "swe", iso1: "sv", name: "Swedish" },
  { code: "nor", iso1: "no", name: "Norwegian" },
  { code: "dan", iso1: "da", name: "Danish" },
  { code: "fin", iso1: "fi", name: "Finnish" },
  { code: "ice", iso1: "is", name: "Icelandic" },
  { code: "cze", iso1: "cs", name: "Czech" },
  { code: "slo", iso1: "sk", name: "Slovak" },
  { code: "hun", iso1: "hu", name: "Hungarian" },
  { code: "rum", iso1: "ro", name: "Romanian" },
  { code: "gre", iso1: "el", name: "Greek" },
  { code: "tur", iso1: "tr", name: "Turkish" },
  { code: "ukr", iso1: "uk", name: "Ukrainian" },
  { code: "bul", iso1: "bg", name: "Bulgarian" },
  { code: "hrv", iso1: "hr", name: "Croatian" },
  { code: "srp", iso1: "sr", name: "Serbian" },
  { code: "slv", iso1: "sl", name: "Slovenian" },
  { code: "cat", iso1: "ca", name: "Catalan" },
  { code: "gle", iso1: "ga", name: "Irish" },
  { code: "wel", iso1: "cy", name: "Welsh" },
  { code: "jpn", iso1: "ja", name: "Japanese" },
  { code: "chi", iso1: "zh", name: "Chinese" },
  { code: "kor", iso1: "ko", name: "Korean" },
  { code: "hin", iso1: "hi", name: "Hindi" },
  { code: "ara", iso1: "ar", name: "Arabic" },
  { code: "heb", iso1: "he", name: "Hebrew" },
  { code: "tha", iso1: "th", name: "Thai" },
  { code: "vie", iso1: "vi", name: "Vietnamese" },
  { code: "ind", iso1: "id", name: "Indonesian" },
  { code: "may", iso1: "ms", name: "Malay" },
  { code: "tgl", iso1: "tl", name: "Tagalog" },
  { code: "per", iso1: "fa", name: "Persian" },
  { code: "ben", iso1: "bn", name: "Bengali" },
  { code: "tam", iso1: "ta", name: "Tamil" },
  { code: "tel", iso1: "te", name: "Telugu" },
  { code: "urd", iso1: "ur", name: "Urdu" },
  { code: "afr", iso1: "af", name: "Afrikaans" },
  { code: "est", iso1: "et", name: "Estonian" },
  { code: "lav", iso1: "lv", name: "Latvian" },
  { code: "lit", iso1: "lt", name: "Lithuanian" },
  { code: "mul", iso1: "mul", name: "Multiple languages" },
  { code: "und", iso1: "und", name: "Undetermined" },
];

/**
 * The picker's languages named in the interface language (FE#35, v0.10.0:
 * always English), sorted by that name. Falls back to the English name where
 * the browser has no translation.
 */
export function languageOptions(): { code: string; name: string }[] {
  let names: Intl.DisplayNames | null = null;
  try {
    names = new Intl.DisplayNames([i18n.language || "en"], { type: "language" });
  } catch {
    names = null;
  }
  const label = (l: { code: string; iso1: string; name: string }) => {
    // Browsers name "und" "root" and leave "mul" untranslated.
    if (l.code === "und" || l.code === "mul") return i18n.t(`common:languages.${l.code}`);
    const n = names?.of(l.iso1);
    return n && n !== l.iso1 ? n.charAt(0).toLocaleUpperCase(i18n.language) + n.slice(1) : l.name;
  };
  return _LANGUAGES.map(l => ({ code: l.code, name: label(l) })).sort((a, b) => naturalCompare(a.name, b.name));
}
