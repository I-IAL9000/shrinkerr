import { useState, useEffect } from "react";
import { getNodes, removeNode, cancelNodeJob, resetNode, updateNodeSettings } from "../api";
import type { WorkerNode } from "../types";
import NodeSettingsModal from "../components/NodeSettingsModal";

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

function fmtRelative(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso).getTime();
  const diff = (Date.now() - d) / 1000;
  if (diff < 0) return "just now";
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

const STATUS_DOT: Record<string, { color: string; label: string }> = {
  online: { color: "var(--success)", label: "Online" },
  working: { color: "#ffa94d", label: "Working" },
  offline: { color: "var(--text-muted)", label: "Offline" },
  error: { color: "var(--danger)", label: "Suspended" },
  paused: { color: "var(--warning)", label: "Paused" },
};

export default function NodesPage() {
  const [nodes, setNodes] = useState<WorkerNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [queuePaused, setQueuePaused] = useState(false);
  const [settingsNode, setSettingsNode] = useState<WorkerNode | null>(null);

  const refresh = () => {
    getNodes().then((d: any) => {
      setNodes(d.nodes);
      setQueuePaused(!d.queue_running || d.queue_paused);
    }).catch(() => {}).finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <h1 style={{ color: "var(--text-primary)", fontSize: 22 }}>Worker Nodes</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {queuePaused && (
            <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: 12, background: "rgba(245,158,11,0.15)", color: "var(--warning)", fontWeight: 600 }}>
              Queue paused
            </span>
          )}
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {nodes.filter(n => n.status !== "offline").length} of {nodes.length} online
          </span>
        </div>
      </div>

      {loading ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 20 }}>
          <div className="spinner" style={{ width: 16, height: 16 }} />
          <span style={{ color: "var(--text-muted)" }}>Loading nodes...</span>
        </div>
      ) : nodes.length === 0 ? (
        <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>
          <p style={{ fontSize: 14, marginBottom: 12 }}>No worker nodes registered yet.</p>
          <p style={{ fontSize: 12, opacity: 0.7 }}>
            To add a remote worker, run the same Docker image with <code style={{ color: "var(--accent)" }}>SHRINKERR_MODE=worker</code>
          </p>
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 16 }}>
          {nodes.map(node => (
            <NodeCard
              key={node.id}
              node={node}
              onRefresh={refresh}
              onOpenSettings={() => setSettingsNode(node)}
            />
          ))}
        </div>
      )}

      {settingsNode && (
        <NodeSettingsModal
          node={settingsNode}
          onClose={() => setSettingsNode(null)}
          onSaved={() => { setSettingsNode(null); refresh(); }}
        />
      )}

      {/* Setup instructions */}
      <div style={{ marginTop: 32, padding: 16, background: "var(--bg-card)", borderRadius: 6, border: "1px solid var(--border)" }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 8 }}>Add a remote worker</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.8 }}>
          Run the same Shrinkerr Docker image on another machine with these environment variables:
        </div>
        <pre style={{
          fontSize: 11, padding: 12, marginTop: 8, borderRadius: 4,
          background: "var(--bg-primary)", border: "1px solid var(--border)",
          color: "var(--text-secondary)", overflow: "auto", lineHeight: 1.6,
        }}>
{`# GPU worker (NVIDIA + NVENC):
docker run -d \\
  -e SHRINKERR_MODE=worker \\
  -e SERVER_URL=http://${window.location.hostname}:${window.location.port || "6680"} \\
  -e API_KEY=<your-api-key> \\
  -v /path/to/media:/media:rw \\
  --runtime=nvidia \\
  --gpus all \\
  ghcr.io/i-ial9000/shrinkerr:nvenc

# CPU-only worker (no GPU, any architecture):
docker run -d \\
  -e SHRINKERR_MODE=worker \\
  -e SERVER_URL=http://${window.location.hostname}:${window.location.port || "6680"} \\
  -e API_KEY=<your-api-key> \\
  -e CAPABILITIES=libx265 \\
  -v /path/to/media:/media:rw \\
  ghcr.io/i-ial9000/shrinkerr:latest`}
        </pre>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>
          The worker will auto-detect GPU capabilities and appear here within 30 seconds.
          Media directories must be mounted at the same paths (or configure PATH_MAPPINGS).
        </div>
      </div>
    </div>
  );
}

function NodeCard({ node, onRefresh, onOpenSettings }: {
  node: WorkerNode;
  onRefresh: () => void;
  onOpenSettings: () => void;
}) {
  // Show "Paused" state when the node is paused (overrides normal status)
  const effectiveStatus = node.paused && node.status !== "offline" && node.status !== "error"
    ? "paused" : node.status;
  const st = STATUS_DOT[effectiveStatus] || STATUS_DOT.offline;
  const isLocal = node.id === "local";

  const togglePause = async () => {
    await updateNodeSettings(node.id, { paused: !node.paused });
    onRefresh();
  };

  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8,
      padding: 16, display: "flex", flexDirection: "column", gap: 10,
      borderLeft: `3px solid ${st.color}`,
    }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: st.color, display: "inline-block", flexShrink: 0 }} />
          <span style={{ fontWeight: 600, color: "var(--text-primary)", fontSize: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{node.name}</span>
          {isLocal && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "var(--accent-bg)", color: "var(--accent)", fontWeight: 600, flexShrink: 0 }}>THIS SERVER</span>}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
          <span style={{ fontSize: 11, color: st.color }}>{st.label}</span>
          {/* Pause / Resume */}
          <button
            onClick={togglePause}
            title={node.paused ? "Resume node" : "Pause node"}
            style={{ background: "none", border: "none", cursor: "pointer", padding: 2, color: "var(--text-muted)", display: "flex", alignItems: "center" }}
          >
            {node.paused ? (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="6" y="4" width="4" height="16" />
                <rect x="14" y="4" width="4" height="16" />
              </svg>
            )}
          </button>
          {/* Settings */}
          <button
            onClick={onOpenSettings}
            title="Node settings"
            style={{ background: "none", border: "none", cursor: "pointer", padding: 2, color: "var(--text-muted)", display: "flex", alignItems: "center" }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
        </div>
      </div>

      {/* Capabilities + affinity badge */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {node.capabilities.map(cap => (
          <span key={cap} style={{
            fontSize: 10, padding: "2px 8px", borderRadius: 12, fontWeight: 600,
            background: cap === "nvenc" ? "rgba(16,185,129,0.15)" : "rgba(104,96,254,0.15)",
            color: cap === "nvenc" ? "var(--success)" : "var(--accent)",
          }}>
            {cap === "nvenc" ? "NVENC (GPU)" : "CPU (x265)"}
          </span>
        ))}
        {node.job_affinity && node.job_affinity !== "any" && (
          <span style={{
            fontSize: 10, padding: "2px 8px", borderRadius: 12, fontWeight: 600,
            background: "rgba(245,158,11,0.15)", color: "var(--warning)",
          }}>
            {node.job_affinity === "cpu_only" ? "CPU jobs only" : "NVENC jobs only"}
          </span>
        )}
      </div>

      {/* Error banner */}
      {node.status === "error" && (
        <div style={{
          padding: "8px 10px", background: "rgba(239,68,68,0.1)", borderRadius: 4,
          border: "1px solid rgba(239,68,68,0.2)", display: "flex", alignItems: "center",
          justifyContent: "space-between", gap: 8,
        }}>
          <span style={{ fontSize: 11, color: "var(--danger)", fontWeight: 600 }}>
            Suspended after {node.consecutive_failures} consecutive failures
          </span>
          <button
            className="btn btn-secondary"
            style={{ fontSize: 10, padding: "3px 10px", color: "var(--accent)", flexShrink: 0 }}
            onClick={async () => { await resetNode(node.id); onRefresh(); }}
          >Reset</button>
        </div>
      )}

      {/* Current job (if working) */}
      {node.status === "working" && node.current_job_file && (
        <div style={{ padding: "8px 10px", background: "var(--bg-primary)", borderRadius: 4, border: "1px solid var(--border)" }}>
          <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {node.current_job_file}
          </div>
          <div className="progress-bar-track" style={{ height: 4 }}>
            <div className="progress-bar-fill" style={{ width: `${node.current_job_progress || 0}%`, height: 4 }} />
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>
            <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{(node.current_job_progress || 0).toFixed(0)}%</span>
            {!isLocal && (
              <button
                onClick={async () => { await cancelNodeJob(node.id); onRefresh(); }}
                style={{ fontSize: 10, color: "var(--danger)", background: "none", border: "none", cursor: "pointer", padding: 0 }}
              >Cancel</button>
            )}
          </div>
        </div>
      )}

      {/* Stats */}
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: "var(--text-muted)" }}>
        <span>Completed: <strong style={{ color: "var(--text-secondary)" }}>{node.jobs_completed.toLocaleString()}</strong></span>
        <span>Saved: <strong style={{ color: "var(--success)" }}>{formatBytes(node.total_space_saved)}</strong></span>
        <span>Parallel: <strong style={{ color: "var(--text-secondary)" }}>{node.max_jobs}</strong></span>
      </div>

      {/* Info grid */}
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 12px", fontSize: 11, color: "var(--text-muted)" }}>
        {node.hostname && <><span>Host:</span><span style={{ color: "var(--text-secondary)" }}>{node.hostname}</span></>}
        {node.gpu_name && <><span>GPU:</span><span style={{ color: "var(--text-secondary)" }}>{node.gpu_name}</span></>}
        {node.ffmpeg_version && <><span>ffmpeg:</span><span style={{ color: "var(--text-secondary)" }}>{node.ffmpeg_version}</span></>}
        <span>Heartbeat:</span><span style={{ color: "var(--text-secondary)" }}>{fmtRelative(node.last_heartbeat)}</span>
      </div>

      {/* Path mappings (collapsible) */}
      {node.path_mappings && node.path_mappings.length > 0 && (
        <details style={{ fontSize: 11, color: "var(--text-muted)" }}>
          <summary style={{ cursor: "pointer", color: "var(--text-secondary)" }}>Path mappings ({node.path_mappings.length})</summary>
          <div style={{ paddingTop: 4 }}>
            {node.path_mappings.map((m, i) => (
              <div key={i} style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>
                {m.server} &rarr; {m.worker}
              </div>
            ))}
          </div>
        </details>
      )}

      {/* Actions */}
      {!isLocal && (
        <button
          className="btn btn-secondary"
          style={{ fontSize: 11, padding: "4px 10px", alignSelf: "flex-start", color: "var(--danger)" }}
          onClick={async () => {
            if (confirm(`Remove node "${node.name}"?${node.status === "working" ? " Its current job will be released back to the queue." : ""}`)) {
              await removeNode(node.id);
              onRefresh();
            }
          }}
        >Remove node</button>
      )}
    </div>
  );
}
