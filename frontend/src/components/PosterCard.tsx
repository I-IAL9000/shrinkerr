interface PosterCardProps {
  title: string;
  year?: string | null;
  posterUrl?: string | null;
  fileCount: number;
  totalSize: number;
  isSelected: boolean;
  onSelect: (shiftKey?: boolean) => void;
  onClick: () => void;
  isExpanded: boolean;
  mediaType?: string | null;
}

function formatSize(bytes: number): string {
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

export default function PosterCard({
  title, year, posterUrl, fileCount, totalSize,
  isSelected, onSelect, onClick, isExpanded, mediaType,
}: PosterCardProps) {
  return (
    <div
      className={`poster-card ${isSelected ? "selected" : ""} ${isExpanded ? "expanded" : ""}`}
      onClick={onClick}
    >
      <div className="poster-img-wrap">
        {posterUrl ? (
          <img
            src={posterUrl}
            alt={title}
            className="poster-img"
            loading="lazy"
            onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; (e.target as HTMLImageElement).nextElementSibling?.classList.remove("hidden"); }}
          />
        ) : null}
        <div className={`poster-placeholder ${posterUrl ? "hidden" : ""}`}>
          <span className="poster-placeholder-text">{title.charAt(0)}</span>
        </div>

        {/* Checkbox overlay — no background box */}
        <div
          className="poster-checkbox"
          onClick={(e) => { e.stopPropagation(); onSelect(e.shiftKey); }}
        >
          <input
            type="checkbox"
            checked={isSelected}
            readOnly
            style={{ accentColor: "var(--accent)", width: 16, height: 16, cursor: "pointer" }}
          />
        </div>

        {mediaType && (
          <span style={{
            position: "absolute", top: 6, right: 6,
            fontSize: 9, fontWeight: 700, letterSpacing: 0.5,
            padding: "2px 5px", borderRadius: 3,
            background: mediaType === "tv" ? "rgba(64, 206, 255, 0.85)" : "rgba(145, 53, 255, 0.85)",
            color: "white",
          }}>
            {mediaType === "tv" ? "TV" : "MOVIE"}
          </span>
        )}

        {isExpanded && (
          <div className="poster-expand-arrow">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M12 16l-6-6h12z"/></svg>
          </div>
        )}
      </div>

      <div className="poster-info">
        <div className="poster-title" title={title}>{title}</div>
        {year && <div className="poster-year">{year}</div>}
        <div className="poster-meta">
          <span>{fileCount} file{fileCount !== 1 ? "s" : ""}</span>
          <span>&middot;</span>
          <span>{formatSize(totalSize)}</span>
        </div>
      </div>
    </div>
  );
}
