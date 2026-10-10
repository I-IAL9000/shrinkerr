import { useState } from "react";
import { useTranslation } from "react-i18next";
import { approveReviews, rejectReviews } from "../api";
import type { Job } from "../types";
import { fmtBytes } from "../fmt";
import { useConfirm } from "./ConfirmModal";
import { useToast } from "../useToast";

/**
 * Queue → Review (v0.10.0): conversions held until they're approved. The
 * originals are untouched; Approve puts the new file in place (the queue
 * does it, ahead of everything else), Reject deletes it.
 */
export default function ReviewList({ jobs, onChange }: { jobs: Job[]; onChange: () => void }) {
  const { t } = useTranslation(["queue", "common"]);
  const confirm = useConfirm();
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const act = async (ids: number[], approve: boolean) => {
    if (!ids.length || busy) return;
    if (!approve && !await confirm({ message: t("queue:review.rejectConfirm", { count: ids.length }),
                                     confirmLabel: t("queue:review.reject"), danger: true })) return;
    setBusy(true);
    try {
      if (approve) {
        const r = await approveReviews(ids);
        toast(t("queue:review.approved", { count: r.approved }), "success");
      } else {
        const r = await rejectReviews(ids);
        toast(t("queue:review.rejected", { count: r.rejected }));
      }
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
      onChange();
    }
  };

  const all = jobs.map(j => j.id);
  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)", flex: "1 1 280px" }}>{t("queue:review.intro")}</span>
        <button className="btn btn-primary" disabled={busy} style={{ fontSize: 12, padding: "4px 12px" }} onClick={() => act(all, true)}>
          {t("queue:review.approveAll")}
        </button>
        <button className="btn btn-secondary" disabled={busy} style={{ fontSize: 12, padding: "4px 12px" }} onClick={() => act(all, false)}>
          {t("queue:review.rejectAll")}
        </button>
      </div>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, background: "var(--bg-primary)", borderRadius: 6 }}>
        {jobs.map(job => {
          const before = job.original_size || 0;
          const saved = job.space_saved || 0;
          const after = Math.max(0, before - saved);
          const pct = before > 0 ? Math.round(saved / before * 100) : 0;
          return (
            <li key={job.id} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, padding: "10px 12px",
                                      borderBottom: "1px solid var(--border)" }}>
              <div style={{ flex: "1 1 260px", minWidth: 0 }}>
                <div style={{ fontSize: 13, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  title={job.file_path}>{job.file_path.split("/").pop()}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                  {t("queue:review.sizes", { before: fmtBytes(before), after: fmtBytes(after), pct })}
                  {job.vmaf_score != null && <> · VMAF {Number(job.vmaf_score).toFixed(1)}</>}
                  {job.encoder && <> · {job.encoder}</>}
                </div>
              </div>
              <button className="btn btn-primary" disabled={busy} style={{ fontSize: 12, padding: "3px 10px" }}
                onClick={() => act([job.id], true)}>{t("queue:review.approve")}</button>
              <button className="btn btn-secondary" disabled={busy} style={{ fontSize: 12, padding: "3px 10px" }}
                onClick={() => act([job.id], false)}>{t("queue:review.reject")}</button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
