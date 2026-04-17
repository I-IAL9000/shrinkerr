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
  onEditClick?: () => void;
}

function formatSize(bytes: number): string {
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

export default function PosterCard({
  title, year, posterUrl, fileCount, totalSize,
  isSelected, onSelect, onClick, isExpanded, mediaType, onEditClick,
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
            background: mediaType === "tv" ? "#0D54E4" : "rgba(145, 53, 255, 0.85)",
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

        {/* Edit (fix TMDB match) button — bottom-left, visible on hover */}
        {onEditClick && (
          <button
            className="poster-edit-btn"
            title="Fix TMDB match"
            onClick={(e) => { e.stopPropagation(); onEditClick(); }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
          </button>
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
