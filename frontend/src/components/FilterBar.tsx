import { useTranslation } from "react-i18next";

interface FilterBarProps {
  activeFilters: string[];
  onFilterToggle: (filter: string) => void;
  newCount?: number;
  counts?: Record<string, number>;
}

// `labelKey` is a translation key resolved at render time; `label` is a
// literal kept for pure tech tokens (codecs, resolutions, source tags).
const FILTERS: { key: string; label?: string; labelKey?: string; group?: string }[] = [
  { key: "all", labelKey: "scanner:filters.all" },
  { key: "new", labelKey: "scanner:filters.new" },
  { key: "needs_conversion", labelKey: "scanner:filters.needsConversion" },
  { key: "disc_iso", labelKey: "scanner:filters.discIso" },
  { key: "high_bitrate", labelKey: "scanner:filters.highBitrate" },
  { key: "low_bitrate", labelKey: "scanner:filters.lowBitrate" },
  { key: "sub_cleanup", labelKey: "scanner:filters.subCleanup" },
  { key: "unknown_language", labelKey: "scanner:filters.unknownLanguage" },
  { key: "ignored", labelKey: "scanner:filters.ignored" },
  { key: "duplicates", labelKey: "scanner:filters.duplicates" },
  { key: "corrupt", labelKey: "scanner:filters.corrupt" },
  { key: "converted", labelKey: "scanner:filters.converted" },
  { key: "queued", labelKey: "scanner:filters.queued" },
  // Video group
  { key: "_video", labelKey: "scanner:filters.groups.video", group: "divider" },
  { key: "x264", label: "x264" },
  { key: "x265", label: "x265" },
  { key: "av1", label: "AV1" },
  { key: "misc_codec", labelKey: "scanner:filters.otherCodecs" },
  // Resolution group
  { key: "_res", labelKey: "scanner:filters.groups.resolution", group: "divider" },
  { key: "res_4k", label: "4K" },
  { key: "res_1080p", label: "1080p" },
  { key: "res_720p", label: "720p" },
  { key: "res_sd", label: "SD" },
  // Size group
  { key: "_size", labelKey: "scanner:filters.groups.size", group: "divider" },
  { key: "size_small", labelKey: "scanner:filters.sizeSmall" },
  { key: "size_medium", labelKey: "scanner:filters.sizeMedium" },
  { key: "size_large", labelKey: "scanner:filters.sizeLarge" },
  // Audio group
  { key: "_audio", labelKey: "scanner:filters.groups.audio", group: "divider" },
  { key: "audio_cleanup", labelKey: "scanner:filters.audioCleanup" },
  { key: "lossless_audio", labelKey: "scanner:filters.losslessAudio" },
  { key: "lossy_audio", labelKey: "scanner:filters.lossyAudio" },
  // Language group
  { key: "_lang", labelKey: "scanner:filters.groups.language", group: "divider" },
  { key: "dubbed", labelKey: "scanner:filters.dubbed" },
  { key: "not_api_matched", labelKey: "scanner:filters.notApiMatched" },
  // Plex group
  { key: "_plex", label: "Plex:", group: "divider" },
  { key: "plex_watched", labelKey: "scanner:filters.watched" },
  { key: "plex_unwatched", labelKey: "scanner:filters.unwatched" },
  // Type group
  { key: "_type", labelKey: "scanner:filters.groups.type", group: "divider" },
  { key: "type_movie", labelKey: "scanner:filters.movies" },
  { key: "type_tv", labelKey: "scanner:filters.tvShows" },
  { key: "type_other", labelKey: "scanner:filters.other" },
  // Source group
  { key: "_source", labelKey: "scanner:filters.groups.source", group: "divider" },
  { key: "src_remux", label: "Remux" },
  { key: "src_bluray", label: "Blu-ray" },
  { key: "src_webdl", label: "WEB-DL" },
  { key: "src_hdtv", label: "HDTV" },
  { key: "src_dvd", label: "DVD" },
  // VMAF group
  { key: "_vmaf", label: "VMAF:", group: "divider" },
  { key: "vmaf_excellent", labelKey: "scanner:filters.vmafExcellent" },
  { key: "vmaf_good", labelKey: "scanner:filters.vmafGood" },
  { key: "vmaf_poor", labelKey: "scanner:filters.vmafPoor" },
];

// Maps filter key → translation key (or literal tech-token label). Resolve
// with `filterLabel(key, t)` at render time.
const FILTER_LABELS: Record<string, { label?: string; labelKey?: string }> = {};
for (const f of FILTERS) {
  if (!f.group) FILTER_LABELS[f.key] = f;
}

export function filterLabel(key: string, t: (k: string) => string): string | undefined {
  const f = FILTER_LABELS[key];
  if (!f) return undefined;
  return f.labelKey ? t(f.labelKey) : f.label;
}

export default function FilterBar({ activeFilters, onFilterToggle, newCount, counts }: FilterBarProps) {
  const { t } = useTranslation(["scanner", "common"]);
  const isAll = activeFilters.length === 0 || (activeFilters.length === 1 && activeFilters[0] === "all");

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 24, alignItems: "center" }}>
      {FILTERS.map((f) => {
        if (f.key === "new" && (!newCount || newCount <= 0)) return null;
        if (f.group === "divider") {
          return (
            <span key={f.key} style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 4 }}>
              <span style={{ width: 1, height: 16, background: "var(--border)" }} />
              <span style={{ opacity: 0.4, fontSize: 12 }}>{f.labelKey ? t(f.labelKey) : f.label}</span>
            </span>
          );
        }
        const isActive = f.key === "all" ? isAll : activeFilters.includes(f.key);
        const count = f.key === "new" ? newCount : counts?.[f.key];
        return (
          <button
            key={f.key}
            className={`filter-pill ${isActive ? "active" : ""}`}
            onClick={() => onFilterToggle(f.key)}
            style={{ whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 5 }}
          >
            {f.labelKey ? t(f.labelKey) : f.label}
            {count != null && count > 0 && (
              <span style={{
                background: f.key === "new" ? "var(--accent)" : "rgba(104,96,254,0.3)",
                color: f.key === "new" ? "white" : "var(--text-secondary)",
                fontSize: 10, fontWeight: "bold",
                padding: "1px 6px", borderRadius: 8,
                display: "inline-flex", alignItems: "center", lineHeight: 1.4,
              }}>
                {count > 99999 ? `${(count / 1000).toFixed(0)}k` : count.toLocaleString()}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
