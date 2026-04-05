interface StatsCardsProps {
  filesToConvert: number;
  audioCleanup: number;
  ignoredCount?: number;
  estimatedSavingsGB: number;
  totalScannedGB: number;
  settingsLabel?: string;
}

export default function StatsCards({ filesToConvert, audioCleanup, ignoredCount, estimatedSavingsGB, totalScannedGB, settingsLabel }: StatsCardsProps) {
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
      {ignoredCount != null && ignoredCount > 0 && (
        <div className="stat-card">
          <div className="stat-value" style={{ color: "var(--text-secondary)" }}>{ignoredCount}</div>
          <div className="stat-label">Ignored</div>
        </div>
      )}
      <div className="stat-card">
        <div className="stat-value success">~{estimatedSavingsGB >= 1024 ? `${(estimatedSavingsGB / 1024).toFixed(1)} TB` : `${estimatedSavingsGB.toFixed(0)} GB`}</div>
        <div className="stat-label">Est. savings{settingsLabel ? ` (${settingsLabel})` : ""}</div>
      </div>
      <div className="stat-card">
        <div className="stat-value" style={{ color: "white" }}>{totalScannedGB >= 1024 ? `${(totalScannedGB / 1024).toFixed(1)} TB` : `${totalScannedGB.toFixed(1)} GB`}</div>
        <div className="stat-label">Total scanned</div>
      </div>
    </div>
  );
}
