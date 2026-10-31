import { useEffect, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { createPolicy, deletePolicy, getPolicies, runPolicy, updatePolicy,
  type Policy, type PolicyInput, type PolicyRunResult, type SavedView } from "../api";
import { fmtBytes, fmtDateTime, fmtNum } from "../fmt";
import { useConfirm } from "./ConfirmModal";
import SettingHelp from "./SettingHelp";
import { useToast } from "../useToast";

const BLANK: PolicyInput = { name: "", view_id: 0, action: "queue", every: "daily", weekday: null, at: "01:00",
  max_files: null, max_gb: null, start_queue: true, enabled: true };

/**
 * Settings → Automation → Policies (v0.10.0): a saved view + an action + a
 * schedule + a budget — "every night at 01:00, queue up to 200 GB of the
 * view Big x264 films, best savings first". Saved on their own (like rules).
 */
export default function PoliciesSection({ views, inputStyle, labelStyle }: {
  views: SavedView[]; inputStyle: CSSProperties; labelStyle: CSSProperties;
}) {
  const { t } = useTranslation(["settingsSystem", "common"]);
  const confirm = useConfirm();
  const toast = useToast();
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [form, setForm] = useState<PolicyInput | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => getPolicies().then(setPolicies).catch(() => setPolicies([]));
  useEffect(() => { load(); }, []);

  const days = [0, 1, 2, 3, 4, 5, 6].map(d => t(`settingsSystem:policies.days.${d}`));
  const viewName = (id: number) => views.find(v => v.id === id)?.name ?? t("settingsSystem:policies.viewGone");
  const summary = (p: Policy) => [
    t(`settingsSystem:policies.actions.${p.action}`),
    p.every === "weekly" ? t("settingsSystem:policies.weeklyAt", { day: days[p.weekday ?? 0], at: p.at })
      : t("settingsSystem:policies.dailyAt", { at: p.at }),
    p.max_files ? t("settingsSystem:policies.upToFiles", { count: p.max_files }) : null,
    p.max_gb ? t("settingsSystem:policies.upToGb", { gb: fmtNum(p.max_gb) }) : null,
  ].filter(Boolean).join(" · ");
  const lastRun = (r: PolicyRunResult | null) => !r ? "" : r.error
    ? t(`settingsSystem:policies.errors.${r.error}`, { defaultValue: r.error })
    : t("settingsSystem:policies.lastResult", { queued: r.queued ?? 0, matched: r.matched ?? 0, size: fmtBytes(r.bytes) });

  const save = async () => {
    if (!form || busy) return;
    setBusy(true);
    try {
      if (editing != null) await updatePolicy(editing, form);
      else await createPolicy(form);
      setForm(null);
      setEditing(null);
      load();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const run = async (p: Policy) => {
    setBusy(true);
    try {
      const r = await runPolicy(p.id);
      toast(lastRun(r), r.error ? "error" : "success");
      load();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (p: Policy) => {
    if (!await confirm({ message: t("settingsSystem:policies.deleteConfirm", { name: p.name }),
                         confirmLabel: t("common:actions.delete"), danger: true })) return;
    await deletePolicy(p.id);
    load();
  };

  const num = (v: string) => (v.trim() === "" ? null : Number(v));
  const field = { display: "flex", flexDirection: "column" as const, gap: 4 };
  const small = { fontSize: 11, color: "var(--text-muted)" };

  return (
    <div>
      <SettingHelp style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 10 }} summary={t("settingsSystem:policies.introSummary")}>{t("settingsSystem:policies.intro")}</SettingHelp>
      {policies.length > 0 && (
        <ul style={{ listStyle: "none", margin: "0 0 12px", padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          {policies.map(p => (
            <li key={p.id} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, padding: "8px 10px",
              background: "var(--bg-primary)", borderRadius: 4, opacity: p.enabled ? 1 : 0.6 }}>
              <div style={{ flex: "1 1 260px", minWidth: 0 }}>
                <div style={{ color: "var(--text-primary)", fontSize: 13, fontWeight: 600 }}>
                  {p.name} <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>· {viewName(p.view_id)}</span>
                </div>
                <div style={small}>{summary(p)}</div>
                <div style={small}>
                  {p.enabled && p.next_run_at ? t("settingsSystem:policies.nextRun", { when: fmtDateTime(p.next_run_at) }) : null}
                  {!p.enabled ? t("settingsSystem:policies.off") : null}
                  {p.last_run_at ? ` · ${t("settingsSystem:policies.lastRun", { when: fmtDateTime(p.last_run_at) })} ${lastRun(p.last_result)}` : ""}
                </div>
              </div>
              <button className="btn btn-xs btn-secondary" disabled={busy} onClick={() => run(p)}>
                {t("settingsSystem:policies.runNow")}
              </button>
              <button className="btn btn-xs btn-secondary" disabled={busy}
                onClick={() => { setEditing(p.id); setForm({ name: p.name, view_id: p.view_id, action: p.action, every: p.every,
                  weekday: p.weekday, at: p.at, max_files: p.max_files, max_gb: p.max_gb, start_queue: p.start_queue, enabled: p.enabled }); }}>
                {t("settingsSystem:policies.edit")}
              </button>
              <button aria-label={t("common:actions.delete")} style={{ background: "none", border: "none", color: "var(--danger)", cursor: "pointer", fontSize: 13 }}
                onClick={() => remove(p)}>×</button>
            </li>
          ))}
        </ul>
      )}

      {form ? (
        <div style={{ background: "var(--bg-primary)", borderRadius: 4, padding: 12, display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
          <div style={field}>
            <label style={small}>{t("settingsSystem:policies.name")}</label>
            <input style={{ ...inputStyle, width: 200 }} value={form.name} aria-label={t("settingsSystem:policies.name")}
              onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div style={field}>
            <label style={small}>{t("settingsSystem:policies.view")}</label>
            <select style={{ ...inputStyle, width: 200 }} value={form.view_id || ""} aria-label={t("settingsSystem:policies.view")}
              onChange={e => setForm({ ...form, view_id: Number(e.target.value) })}>
              <option value="">{t("settingsSystem:policies.chooseView")}</option>
              {views.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select>
          </div>
          <div style={field}>
            <label style={small}>{t("settingsSystem:policies.action")}</label>
            <select style={{ ...inputStyle, width: 200 }} value={form.action} aria-label={t("settingsSystem:policies.action")}
              onChange={e => setForm({ ...form, action: e.target.value as PolicyInput["action"] })}>
              <option value="queue">{t("settingsSystem:policies.actions.queue")}</option>
              <option value="health_check">{t("settingsSystem:policies.actions.health_check")}</option>
            </select>
          </div>
          <div style={field}>
            <label style={small}>{t("settingsSystem:policies.when")}</label>
            <div style={{ display: "flex", gap: 6 }}>
              <select style={{ ...inputStyle, width: 110 }} value={form.every} aria-label={t("settingsSystem:policies.when")}
                onChange={e => setForm({ ...form, every: e.target.value as PolicyInput["every"],
                  weekday: e.target.value === "weekly" ? (form.weekday ?? 6) : null })}>
                <option value="daily">{t("settingsSystem:policies.daily")}</option>
                <option value="weekly">{t("settingsSystem:policies.weekly")}</option>
              </select>
              {form.every === "weekly" && (
                <select style={{ ...inputStyle, width: 120 }} value={form.weekday ?? 6} aria-label={t("settingsSystem:policies.day")}
                  onChange={e => setForm({ ...form, weekday: Number(e.target.value) })}>
                  {days.map((d, i) => <option key={i} value={i}>{d}</option>)}
                </select>
              )}
              <input type="time" style={{ ...inputStyle, width: 100 }} value={form.at} aria-label={t("settingsSystem:policies.time")}
                onChange={e => setForm({ ...form, at: e.target.value })} />
            </div>
          </div>
          <div style={field}>
            <label style={small}>{t("settingsSystem:policies.maxFiles")}</label>
            <input type="number" min={1} style={{ ...inputStyle, width: 90 }} value={form.max_files ?? ""}
              placeholder={t("settingsSystem:policies.noLimit")} aria-label={t("settingsSystem:policies.maxFiles")}
              onChange={e => setForm({ ...form, max_files: num(e.target.value) })} />
          </div>
          <div style={field}>
            <label style={small}>{t("settingsSystem:policies.maxGb")}</label>
            <input type="number" min={1} style={{ ...inputStyle, width: 90 }} value={form.max_gb ?? ""}
              placeholder={t("settingsSystem:policies.noLimit")} aria-label={t("settingsSystem:policies.maxGb")}
              onChange={e => setForm({ ...form, max_gb: num(e.target.value) })} />
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
            <input type="checkbox" checked={form.start_queue} onChange={() => setForm({ ...form, start_queue: !form.start_queue })} />
            <span style={labelStyle}>{t("settingsSystem:policies.startQueue")}</span>
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
            <input type="checkbox" checked={form.enabled} onChange={() => setForm({ ...form, enabled: !form.enabled })} />
            <span style={labelStyle}>{t("settingsSystem:policies.enabled")}</span>
          </label>
          <div style={{ display: "flex", gap: 8, flexBasis: "100%" }}>
            <button className="btn btn-primary" disabled={busy || !form.name.trim() || !form.view_id} onClick={save}>
              {editing != null ? t("common:actions.save") : t("settingsSystem:policies.add")}
            </button>
            <button className="btn btn-secondary" onClick={() => { setForm(null); setEditing(null); }}>{t("common:actions.cancel")}</button>
          </div>
          {views.length === 0 && <div style={{ ...small, flexBasis: "100%" }}>{t("settingsSystem:policies.noViews")}</div>}
        </div>
      ) : (
        <button className="btn btn-sm btn-secondary" onClick={() => { setEditing(null); setForm({ ...BLANK }); }}>
          + {t("settingsSystem:policies.add")}
        </button>
      )}
    </div>
  );
}
