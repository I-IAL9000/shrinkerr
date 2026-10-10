import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { getViews, planSpace, queueSpacePlan, type SavedView, type SpacePlan, type SpacePlanInput } from "../api";
import { fmtBytes, fmtDateTime, fmtDuration, fmtNum } from "../fmt";
import { useDialog } from "../useDialog";
import { useToast } from "../useToast";

/**
 * Queue → "Free space…" (v0.10.0): the conversions that free N GB fastest —
 * most estimated savings per hour of encoding — and when they'd be done.
 */
export default function PlannerModal({ onClose, onQueued }: { onClose: () => void; onQueued: () => void }) {
  const { t } = useTranslation(["queue", "common"]);
  const toast = useToast();
  const panelRef = useRef<HTMLDivElement>(null);
  const dialog = useDialog(panelRef, onClose, t("queue:planner.title"));
  const [amount, setAmount] = useState("500");
  const [unit, setUnit] = useState<"GB" | "TB">("GB");
  const [deadline, setDeadline] = useState("");
  const [viewId, setViewId] = useState("");
  const [views, setViews] = useState<SavedView[]>([]);
  const [plan, setPlan] = useState<SpacePlan | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { getViews().then(setViews).catch(() => setViews([])); }, []);

  const input = (): SpacePlanInput => ({
    target_gb: (Number(amount) || 0) * (unit === "TB" ? 1024 : 1),
    deadline: deadline || null,
    view_id: viewId ? Number(viewId) : null,
  });
  const calculate = async () => {
    setBusy(true);
    try {
      setPlan(await planSpace(input()));
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const queue = async () => {
    setBusy(true);
    try {
      const r = await queueSpacePlan(input());
      toast(t("queue:planner.queued", { count: r.queued }), "success");
      onQueued();
      onClose();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const inputStyle = { padding: "6px 8px", fontSize: 13, background: "var(--bg-primary)", color: "var(--text-primary)",
    border: "1px solid var(--border)", borderRadius: 4 };
  const label = { fontSize: 11, color: "var(--text-muted)" };
  return (
    <div onClick={busy ? undefined : onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000,
      display: "flex", alignItems: "center", justifyContent: "center", padding: 20 }}>
      <div ref={panelRef} {...dialog} onClick={e => e.stopPropagation()} style={{ background: "var(--bg-card)",
        border: "1px solid var(--border)", borderRadius: 8, width: "100%", maxWidth: 520, padding: "16px 18px" }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{t("queue:planner.title")}</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", margin: "4px 0 12px" }}>{t("queue:planner.intro")}</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={label}>{t("queue:planner.free")}</label>
            <div style={{ display: "flex", gap: 4 }}>
              <input type="number" min={1} value={amount} aria-label={t("queue:planner.free")} data-autofocus
                onChange={e => { setAmount(e.target.value); setPlan(null); }} style={{ ...inputStyle, width: 90 }} />
              <select value={unit} aria-label={t("queue:planner.unit")} onChange={e => { setUnit(e.target.value as "GB" | "TB"); setPlan(null); }} style={inputStyle}>
                <option>GB</option><option>TB</option>
              </select>
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={label}>{t("queue:planner.by")}</label>
            <input type="datetime-local" value={deadline} aria-label={t("queue:planner.by")}
              onChange={e => { setDeadline(e.target.value); setPlan(null); }} style={inputStyle} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={label}>{t("queue:planner.from")}</label>
            <select value={viewId} aria-label={t("queue:planner.from")} onChange={e => { setViewId(e.target.value); setPlan(null); }} style={{ ...inputStyle, width: 180 }}>
              <option value="">{t("queue:planner.everything")}</option>
              {views.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select>
          </div>
          <button className="btn btn-secondary" disabled={busy || !(Number(amount) > 0)} onClick={calculate}>{t("queue:planner.calculate")}</button>
        </div>

        {plan && (
          <div role="status" style={{ marginTop: 14, fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6 }}>
            {plan.files === 0 ? t("queue:planner.nothing") : <>
              <div>{t("queue:planner.result", { count: plan.files, savings: fmtBytes(plan.savings), time: fmtDuration(plan.seconds) })}</div>
              <div>{t("queue:planner.finish", { when: fmtDateTime(plan.finish_at) })}</div>
              {!plan.reached && <div style={{ color: "var(--caution)" }}>{t("queue:planner.notReached", { available: fmtBytes(plan.available) })}</div>}
              {!plan.on_time && <div style={{ color: "var(--caution)" }}>{t("queue:planner.late")}</div>}
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("queue:planner.note", { files: fmtNum(plan.files) })}</div>
            </>}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>{t("common:actions.close")}</button>
          <button className="btn btn-primary" disabled={busy || !plan || plan.files === 0} onClick={queue}>
            {t("queue:planner.queue", { count: plan?.files ?? 0 })}
          </button>
        </div>
      </div>
    </div>
  );
}
