import { useState, useEffect } from "react";
import { searchTMDB, overridePoster } from "../api";
import type { TMDBSearchResult } from "../api";

interface Props {
  folderPath: string;
  currentTitle: string;
  currentYear?: string | null;
  onClose: () => void;
  onFixed: () => void;  // called after a successful override — parent should refresh
}

export default function PosterFixModal({ folderPath, currentTitle, currentYear, onClose, onFixed }: Props) {
  const [query, setQuery] = useState(currentTitle);
  const [year, setYear] = useState(currentYear || "");
  const [results, setResults] = useState<TMDBSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState<number | null>(null);

  const doSearch = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await searchTMDB(query, year || undefined);
      setResults(res.results);
      if (res.results.length === 0) setError("No matches found on TMDB");
    } catch (e: any) {
      setError(e?.message || "Search failed");
    } finally {
      setLoading(false);
    }
  };

  // Auto-search on open
  useEffect(() => {
    doSearch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const apply = async (r: TMDBSearchResult) => {
    setApplying(r.tmdb_id);
    setError(null);
    try {
      await overridePoster(folderPath, r.tmdb_id, r.media_type);
      onFixed();
    } catch (e: any) {
      setError(e?.message || "Failed to apply");
      setApplying(null);
    }
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000,
        display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8,
          width: "100%", maxWidth: 720, maxHeight: "90vh", overflow: "hidden",
          display: "flex", flexDirection: "column",
        }}
      >
        {/* Header */}
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>Fix poster match</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, maxWidth: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {folderPath}
            </div>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 20 }}>×</button>
        </div>

        {/* Search bar */}
        <div style={{ padding: 14, borderBottom: "1px solid var(--border)", display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") doSearch(); }}
            placeholder="Title"
            style={{
              flex: 1, padding: "6px 10px", fontSize: 13,
              background: "var(--bg-primary)", border: "1px solid var(--border)",
              borderRadius: 4, color: "var(--text-primary)",
            }}
          />
          <input
            type="text"
            value={year}
            onChange={e => setYear(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") doSearch(); }}
            placeholder="Year"
            style={{
              width: 80, padding: "6px 10px", fontSize: 13,
              background: "var(--bg-primary)", border: "1px solid var(--border)",
              borderRadius: 4, color: "var(--text-primary)",
            }}
          />
          <button className="btn btn-primary" onClick={doSearch} disabled={loading}>
            {loading ? "..." : "Search"}
          </button>
        </div>

        {/* Results */}
        <div style={{ padding: 14, overflow: "auto", flex: 1 }}>
          {error && (
            <div style={{ padding: 12, background: "rgba(239,68,68,0.1)", color: "var(--danger)", borderRadius: 4, fontSize: 12, marginBottom: 10 }}>
              {error}
            </div>
          )}
          {loading && results.length === 0 ? (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 20, color: "var(--text-muted)", fontSize: 12 }}>
              <div className="spinner" style={{ width: 14, height: 14 }} />
              Searching TMDB...
            </div>
          ) : (
            <div style={{
              display: "grid", gap: 10,
              gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))",
            }}>
              {results.map(r => (
                <button
                  key={`${r.media_type}-${r.tmdb_id}`}
                  onClick={() => apply(r)}
                  disabled={applying !== null}
                  style={{
                    background: "var(--bg-primary)", border: "1px solid var(--border)",
                    borderRadius: 6, padding: 6, textAlign: "left", cursor: applying ? "wait" : "pointer",
                    display: "flex", flexDirection: "column", gap: 4,
                    opacity: applying !== null && applying !== r.tmdb_id ? 0.4 : 1,
                  }}
                >
                  <div style={{ position: "relative", aspectRatio: "2 / 3", background: "var(--bg-tertiary)", borderRadius: 3, overflow: "hidden" }}>
                    {r.poster_url ? (
                      <img src={r.poster_url} alt={r.title} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                    ) : (
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", width: "100%", height: "100%", color: "var(--text-muted)", fontSize: 24 }}>
                        {r.title.charAt(0)}
                      </div>
                    )}
                    <span style={{
                      position: "absolute", top: 4, right: 4,
                      fontSize: 8, fontWeight: 700, padding: "2px 4px", borderRadius: 3,
                      background: r.media_type === "tv" ? "#0D54E4" : "rgba(145, 53, 255, 0.85)",
                      color: "white",
                    }}>{r.media_type === "tv" ? "TV" : "MOVIE"}</span>
                    {applying === r.tmdb_id && (
                      <div style={{ position: "absolute", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <div className="spinner" style={{ width: 20, height: 20 }} />
                      </div>
                    )}
                  </div>
                  <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={r.title}>
                    {r.title}
                  </div>
                  <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
                    {r.year || "—"}{r.rating ? ` · ⭐ ${r.rating}` : ""}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
