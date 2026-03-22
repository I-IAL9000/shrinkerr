interface FilterBarProps {
  activeFilter: string;
  onFilterChange: (filter: string) => void;
  onAddToQueue: () => void;
}

const FILTERS = ["all", "needs_conversion", "audio_cleanup", "optimized"];
const LABELS: Record<string, string> = {
  all: "All",
  needs_conversion: "Needs conversion",
  audio_cleanup: "Audio cleanup",
  optimized: "Already optimized",
};

export default function FilterBar({ activeFilter, onFilterChange, onAddToQueue }: FilterBarProps) {
  return (
    <div className="filter-bar">
      <span style={{ opacity: 0.5, fontSize: 12 }}>Filter:</span>
      {FILTERS.map((f) => (
        <button
          key={f}
          className={`filter-pill ${activeFilter === f ? "active" : ""}`}
          onClick={() => onFilterChange(f)}
        >
          {LABELS[f]}
        </button>
      ))}
      <div style={{ flex: 1 }} />
      <button className="btn btn-primary" onClick={onAddToQueue}>
        Add selected to queue
      </button>
    </div>
  );
}
