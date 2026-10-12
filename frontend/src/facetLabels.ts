// A list value's label in the filter bar (v0.10.0): a language's name, a
// codec's usual spelling, a band's words; years, countries and networks as
// they are; a media folder's path from the server.
type T = (key: string, options?: Record<string, unknown>) => string;

const CODECS: Record<string, string> = {
  aac: "AAC", ac3: "AC-3", eac3: "E-AC-3", dts: "DTS", truehd: "TrueHD", flac: "FLAC", opus: "Opus",
  mp3: "MP3", mp2: "MP2", vorbis: "Vorbis", alac: "ALAC",
};

// The server names a language by its ISO 639-2 B code ("fre"); the language
// list calls most of those "French (alt)" (or lacks them: "wel") and names
// the T code ("fra") plainly. Chinese is the other way round ("chi" plain).
const NAMED_AS: Record<string, string> = {
  alb: "sqi", arm: "hye", baq: "eus", bur: "mya", cze: "ces", dut: "nld", fre: "fra", geo: "kat", ger: "deu",
  gre: "ell", ice: "isl", mac: "mkd", mao: "mri", may: "msa", per: "fas", rum: "ron", slo: "slk", tib: "bod", wel: "cym",
};

// The other way, as the server keys a language (scan_filters.fold_lang): a
// T code by its B code ("fra" -> "fre"; Chinese's "zho" -> "chi").
const B_CODES: Record<string, string> = {
  ...Object.fromEntries(Object.entries(NAMED_AS).map(([b, tCode]) => [tCode, b])),
  zho: "chi",
};

/** A language code as the lists key it: "fra" -> "fre", none -> "und". The
 *  server also folds two-letter codes ("fr"); those stay as they are here. */
export function languageKey(code: string): string {
  return code ? B_CODES[code] ?? code : "und";
}

export function facetValueLabel(facet: string, value: string, t: T, label?: string): string {
  switch (facet) {
    case "alang":
    case "slang":
      return value === "und"
        ? t("scanner:facets.unknownLanguage")
        : t(`settingsMedia:languages.${NAMED_AS[value] ?? value}`, { defaultValue: value });
    case "acodec":
      return CODECS[value] ?? (value.startsWith("pcm") ? "PCM" : value.toUpperCase());
    case "achan":
      return t(`scanner:facets.channels.${value}`, { defaultValue: value });
    case "genre":
      return value.replace(/(^|\s|-)\p{L}/gu, c => c.toUpperCase());
    case "rating":
      return t(`scanner:facets.rating.${value}`, { defaultValue: value });
    case "showstatus":
      return t(`scanner:facets.status.${value}`, { defaultValue: value });
    case "library":
      return label ?? value;
    default:
      return value;
  }
}
