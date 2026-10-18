import { useState } from "react";
import { useTranslation } from "react-i18next";
import { approveReviews, rejectReviews } from "../api";
import type { Job } from "../types";
import { fmtBytes } from "../fmt";
import { useConfirm } from "./ConfirmModal";
import { useToast } from "../useToast";
import VirtualJobList from "./VirtualJobList";
import BeforeAfter from "./BeforeAfter";

/**
 * Queue → Review (v0.10.0): conversions held until they're approved. The
 * originals are untouched; Approve puts the new file in place straight away
 * (the worker's placement lane, not the queue), Reject deletes it. Every
 * held file is listed, as many as there are.
 */
export default function ReviewList({ jobs, onChange }: { jobs: Job[]; onChange: () => void }) {
  const { t } = useTranslation(["queue", "common"]);
  const confirm = useConfirm();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [comparing, setComparing] = useState<Set<number>>(new Set());

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

  const toggleCompare = (id: number) => setComparing(prev => {
    const next = new Set(prev);
    if (!next.delete(id)) next.add(id);
    return next;
  });

  // Not the ones already being put in place.
  const waiting = jobs.filter(j => !j.review_approved).map(j => j.id);

  const renderRow = (i: number) => {
    const job = jobs[i];
    const before = job.original_size || 0;
    const saved = job.space_saved || 0;
    const after = Math.max(0, before - saved);
    const pct = before > 0 ? Math.round(saved / before * 100) : 0;
    const open = comparing.has(job.id);
    return (
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, padding: "10px 12px",
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
        {job.job_type !== "audio" && (
          <button className="btn btn-sm btn-secondary" aria-pressed={open} onClick={() => toggleCompare(job.id)}>
            {t("queue:item.frames.show")}
          </button>
        )}
        {job.review_approved ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-muted)" }}>
            <span className="spinner" style={{ width: 12, height: 12 }} /> {t("queue:review.placing")}
          </span>
        ) : (<>
          <button className="btn btn-sm btn-primary" disabled={busy}
            onClick={() => act([job.id], true)}>{t("queue:review.approve")}</button>
          <button className="btn btn-sm btn-secondary" disabled={busy}
            onClick={() => act([job.id], false)}>{t("queue:review.reject")}</button>
        </>)}
        {open && (
          <div style={{ flexBasis: "100%" }}>
            <BeforeAfter jobId={job.id} open />
          </div>
        )}
      </div>
    );
  };

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)", flex: "1 1 280px" }}>{t("queue:review.intro")}</span>
        <button className="btn btn-sm btn-primary" disabled={busy || !waiting.length} onClick={() => act(waiting, true)}>
          {t("queue:review.approveAll")}
        </button>
        <button className="btn btn-sm btn-secondary" disabled={busy || !waiting.length} onClick={() => act(waiting, false)}>
          {t("queue:review.rejectAll")}
        </button>
      </div>
      <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden" }}>
        <VirtualJobList count={jobs.length} getKey={i => jobs[i].id} renderRow={renderRow} />
      </div>
    </div>
  );
}
