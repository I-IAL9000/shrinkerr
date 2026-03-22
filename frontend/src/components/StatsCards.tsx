interface StatsCardsProps {
  filesToConvert: number;
  audioCleanup: number;
  estimatedSavingsGB: number;
  totalScannedGB: number;
}

export default function StatsCards({ filesToConvert, audioCleanup, estimatedSavingsGB, totalScannedGB }: StatsCardsProps) {
  return (
    <div className="stats-grid">
      <div className="stat-card">
        <div className="stat-value">{filesToConvert}</div>
        <div className="stat-label">Files to convert</div>
      </div>
      <div className="stat-card">
        <div className="stat-value">{audioCleanup}</div>
        <div className="stat-label">Audio cleanup</div>
      </div>
      <div className="stat-card">
        <div className="stat-value success">~{estimatedSavingsGB.toFixed(0)} GB</div>
        <div className="stat-label">Est. savings</div>
      </div>
      <div className="stat-card">
        <div className="stat-value" style={{ color: "white" }}>{totalScannedGB.toFixed(1)} TB</div>
        <div className="stat-label">Total scanned</div>
      </div>
    </div>
  );
}
