interface FilterBarProps {
  activeFilters: string[];
  onFilterToggle: (filter: string) => void;
  newCount?: number;
  counts?: Record<string, number>;
}

const FILTERS: { key: string; label: string; group?: string }[] = [
  { key: "all", label: "All" },
  { key: "new", label: "New" },
  { key: "needs_conversion", label: "Needs conversion" },
  { key: "high_bitrate", label: "High bitrate" },
  { key: "low_bitrate", label: "Low bitrate" },
  { key: "sub_cleanup", label: "Subtitle cleanup" },
  { key: "ignored", label: "Ignored" },
  { key: "duplicates", label: "Duplicates" },
  { key: "corrupt", label: "Corrupt" },
  { key: "converted", label: "Converted" },
  { key: "queued", label: "Queued" },
  // Video group
  { key: "_video", label: "Video:", group: "divider" },
  { key: "x264", label: "x264" },
  { key: "x265", label: "x265" },
  { key: "av1", label: "AV1" },
  { key: "misc_codec", label: "Other codecs" },
  // Resolution group
  { key: "_res", label: "Res:", group: "divider" },
  { key: "res_4k", label: "4K" },
  { key: "res_1080p", label: "1080p" },
  { key: "res_720p", label: "720p" },
  { key: "res_sd", label: "SD" },
  // Size group
  { key: "_size", label: "Size:", group: "divider" },
  { key: "size_small", label: "Small (<5 GB)" },
  { key: "size_medium", label: "Medium (5-10 GB)" },
  { key: "size_large", label: "Large (>10 GB)" },
  // Audio group
  { key: "_audio", label: "Audio:", group: "divider" },
  { key: "audio_cleanup", label: "Audio cleanup" },
  { key: "lossless_audio", label: "Lossless audio" },
  { key: "lossy_audio", label: "Lossy audio" },
  // Plex group
  { key: "_plex", label: "Plex:", group: "divider" },
  { key: "plex_watched", label: "Watched" },
  { key: "plex_unwatched", label: "Unwatched" },
  // Type group
  { key: "_type", label: "Type:", group: "divider" },
  { key: "type_movie", label: "Movies" },
  { key: "type_tv", label: "TV Shows" },
  { key: "type_other", label: "Other" },
  // Source group
  { key: "_source", label: "Source:", group: "divider" },
  { key: "src_remux", label: "Remux" },
  { key: "src_bluray", label: "Blu-ray" },
  { key: "src_webdl", label: "WEB-DL" },
  { key: "src_hdtv", label: "HDTV" },
  { key: "src_dvd", label: "DVD" },
  // VMAF group
  { key: "_vmaf", label: "VMAF:", group: "divider" },
  { key: "vmaf_excellent", label: "Excellent (93+)" },
  { key: "vmaf_good", label: "Good (87-93)" },
  { key: "vmaf_poor", label: "Poor (<87)" },
];

export const FILTER_LABELS: Record<string, string> = {};
for (const f of FILTERS) {
  if (!f.group) FILTER_LABELS[f.key] = f.label;
}

export default function FilterBar({ activeFilters, onFilterToggle, newCount, counts }: FilterBarProps) {
  const isAll = activeFilters.length === 0 || (activeFilters.length === 1 && activeFilters[0] === "all");

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 24, alignItems: "center" }}>
      {FILTERS.map((f) => {
        if (f.key === "new" && (!newCount || newCount <= 0)) return null;
        if (f.group === "divider") {
          return (
            <span key={f.key} style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 4 }}>
              <span style={{ width: 1, height: 16, background: "var(--border)" }} />
              <span style={{ opacity: 0.4, fontSize: 12 }}>{f.label}</span>
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
            {f.label}
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
