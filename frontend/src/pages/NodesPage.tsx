import { useState, useEffect } from "react";
import { Trans, useTranslation } from "react-i18next";
import i18n from "../i18n";
import { getNodes, removeNode, cancelNodeJob, resetNode, updateNodeSettings } from "../api";
import type { WorkerNode } from "../types";
import NodeSettingsModal from "../components/NodeSettingsModal";

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

function fmtRelative(iso: string | null): string {
  if (!iso) return i18n.t("nodes:relative.never");
  const d = new Date(iso).getTime();
  const diff = (Date.now() - d) / 1000;
  if (diff < 0) return i18n.t("nodes:relative.justNow");
  if (diff < 60) return i18n.t("nodes:relative.secondsAgo", { n: Math.round(diff) });
  if (diff < 3600) return i18n.t("nodes:relative.minutesAgo", { n: Math.round(diff / 60) });
  if (diff < 86400) return i18n.t("nodes:relative.hoursAgo", { n: Math.round(diff / 3600) });
  return i18n.t("nodes:relative.daysAgo", { n: Math.round(diff / 86400) });
}

const STATUS_DOT: Record<string, { color: string; labelKey: string }> = {
  online: { color: "var(--success)", labelKey: "nodes:status.online" },
  working: { color: "#ffa94d", labelKey: "nodes:status.working" },
  offline: { color: "var(--text-muted)", labelKey: "nodes:status.offline" },
  error: { color: "var(--danger)", labelKey: "nodes:status.suspended" },
  paused: { color: "var(--warning)", labelKey: "nodes:status.paused" },
};

export default function NodesPage() {
  const { t } = useTranslation(["nodes", "common"]);
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
        <h1 style={{ color: "var(--text-primary)", fontSize: 22 }}>{t("nodes:title")}</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {queuePaused && (
            <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: 12, background: "rgba(245,158,11,0.15)", color: "var(--warning)", fontWeight: 600 }}>
              {t("nodes:queuePaused")}
            </span>
          )}
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {t("nodes:onlineCount", { online: nodes.filter(n => n.status !== "offline").length, total: nodes.length })}
          </span>
        </div>
      </div>

      {loading ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 20 }}>
          <div className="spinner" style={{ width: 16, height: 16 }} />
          <span style={{ color: "var(--text-muted)" }}>{t("nodes:loadingNodes")}</span>
        </div>
      ) : nodes.length === 0 ? (
        <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>
          <p style={{ fontSize: 14, marginBottom: 12 }}>{t("nodes:empty.title")}</p>
          <p style={{ fontSize: 12, opacity: 0.7 }}>
            <Trans i18nKey="nodes:empty.hint" components={{ code: <code style={{ color: "var(--accent)" }} /> }} />
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
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 8 }}>{t("nodes:setup.title")}</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.8 }}>
          {t("nodes:setup.intro")}
        </div>
        <pre style={{
          fontSize: 11, padding: 12, marginTop: 8, borderRadius: 4,
          background: "var(--bg-primary)", border: "1px solid var(--border)",
          color: "var(--text-secondary)", overflow: "auto", lineHeight: 1.6,
        }}>
{`# ${t("nodes:setup.gpuComment")}
docker run -d \\
  -e SHRINKERR_MODE=worker \\
  -e SERVER_URL=http://${window.location.hostname}:${window.location.port || "6680"} \\
  -e API_KEY=<your-api-key> \\
  -v /path/to/media:/media:rw \\
  --runtime=nvidia \\
  --gpus all \\
  ghcr.io/i-ial9000/shrinkerr:nvenc

# ${t("nodes:setup.cpuComment")}
docker run -d \\
  -e SHRINKERR_MODE=worker \\
  -e SERVER_URL=http://${window.location.hostname}:${window.location.port || "6680"} \\
  -e API_KEY=<your-api-key> \\
  -e CAPABILITIES=libx265 \\
  -v /path/to/media:/media:rw \\
  ghcr.io/i-ial9000/shrinkerr:latest`}
        </pre>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>
          {t("nodes:setup.footer")}
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
  const { t } = useTranslation(["nodes", "common"]);
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
          {isLocal && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "var(--accent-bg)", color: "var(--accent)", fontWeight: 600, flexShrink: 0 }}>{t("nodes:card.thisServer")}</span>}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
          <span style={{ fontSize: 11, color: st.color }}>{t(st.labelKey)}</span>
          {/* Pause / Resume */}
          <button
            onClick={togglePause}
            title={node.paused ? t("nodes:card.resumeNode") : t("nodes:card.pauseNode")}
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
            title={t("nodes:card.nodeSettings")}
            style={{ background: "none", border: "none", cursor: "pointer", padding: 2, color: "var(--text-muted)", display: "flex", alignItems: "center" }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
        </div>
      </div>

      {/* Capabilities + affinity badge. Per-encoder labels so a host
          with libx265 + qsv + vaapi + nvenc shows four distinct pills,
          not four pills all reading "CPU (x265)" (the pre-v0.3.119
          binary check treated everything-but-nvenc as CPU). */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {node.capabilities.map(cap => {
          const label =
            cap === "nvenc" ? "NVENC (GPU)" :
            cap === "qsv"   ? "QSV (Intel)" :
            cap === "vaapi" ? "VAAPI (GPU)" :
            cap === "libx265" ? "libx265 (CPU)" :
            cap;
          const isHardware = cap === "nvenc" || cap === "qsv" || cap === "vaapi";
          return (
            <span key={cap} style={{
              fontSize: 10, padding: "2px 8px", borderRadius: 12, fontWeight: 600,
              background: isHardware ? "rgba(16,185,129,0.15)" : "rgba(104,96,254,0.15)",
              color: isHardware ? "var(--success)" : "var(--accent)",
            }}>
              {label}
            </span>
          );
        })}
        {node.job_affinity && node.job_affinity !== "any" && (
          <span style={{
            fontSize: 10, padding: "2px 8px", borderRadius: 12, fontWeight: 600,
            background: "rgba(245,158,11,0.15)", color: "var(--warning)",
          }}>
            {node.job_affinity === "cpu_only" ? t("nodes:card.cpuJobsOnly") : t("nodes:card.nvencJobsOnly")}
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
            {t("nodes:card.suspendedAfter", { count: node.consecutive_failures })}
          </span>
          <button
            className="btn btn-secondary"
            style={{ fontSize: 10, padding: "3px 10px", color: "var(--accent)", flexShrink: 0 }}
            onClick={async () => { await resetNode(node.id); onRefresh(); }}
          >{t("nodes:card.reset")}</button>
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
              >{t("common:actions.cancel")}</button>
            )}
          </div>
        </div>
      )}

      {/* Stats */}
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: "var(--text-muted)" }}>
        <span>{t("nodes:card.completed")} <strong style={{ color: "var(--text-secondary)" }}>{node.jobs_completed.toLocaleString()}</strong></span>
        <span>{t("nodes:card.saved")} <strong style={{ color: "var(--success)" }}>{formatBytes(node.total_space_saved)}</strong></span>
        <span>{t("nodes:card.parallel")} <strong style={{ color: "var(--text-secondary)" }}>{node.max_jobs}</strong></span>
      </div>

      {/* Info grid */}
      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 12px", fontSize: 11, color: "var(--text-muted)" }}>
        {node.hostname && <><span>{t("nodes:card.host")}</span><span style={{ color: "var(--text-secondary)" }}>{node.hostname}</span></>}
        {node.gpu_name && <><span>GPU:</span><span style={{ color: "var(--text-secondary)" }}>{node.gpu_name}</span></>}
        {node.ffmpeg_version && <><span>ffmpeg:</span><span style={{ color: "var(--text-secondary)" }}>{node.ffmpeg_version}</span></>}
        <span>{t("nodes:card.heartbeat")}</span><span style={{ color: "var(--text-secondary)" }}>{fmtRelative(node.last_heartbeat)}</span>
      </div>

      {/* Path mappings (collapsible) */}
      {node.path_mappings && node.path_mappings.length > 0 && (
        <details style={{ fontSize: 11, color: "var(--text-muted)" }}>
          <summary style={{ cursor: "pointer", color: "var(--text-secondary)" }}>{t("nodes:card.pathMappings", { count: node.path_mappings.length })}</summary>
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
            if (confirm(node.status === "working" ? t("nodes:card.removeConfirmWorking", { name: node.name }) : t("nodes:card.removeConfirm", { name: node.name }))) {
              await removeNode(node.id);
              onRefresh();
            }
          }}
        >{t("nodes:card.removeNode")}</button>
      )}
    </div>
  );
}
