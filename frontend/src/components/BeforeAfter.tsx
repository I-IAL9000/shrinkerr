import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { getJobFrames, type JobFrames } from "../api";

const LABEL = {
  position: "absolute", top: 6, padding: "1px 6px", borderRadius: 3, fontSize: 11, lineHeight: 1.5,
  background: "rgba(0, 0, 0, 0.6)", color: "white", pointerEvents: "none",
} as const;

function clock(seconds: number): string {
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

/**
 * Before / after (v0.10.0): the same moments of the original and the
 * converted file, grabbed during the conversion, on a slider. Loaded when
 * asked for — the frames are a few hundred KB.
 */
export default function BeforeAfter({ jobId, open = false }: {
  jobId: number;
  /** Load and show the frames straight away, without its own button (Review). */
  open?: boolean;
}) {
  const { t } = useTranslation("queue");
  const [result, setResult] = useState<JobFrames | null>(null);
  const [loading, setLoading] = useState(false);
  const [index, setIndex] = useState(0);
  const [split, setSplit] = useState(50);
  const box = useRef<HTMLDivElement>(null);

  const load = async () => {
    setLoading(true);
    try { setResult(await getJobFrames(jobId)); } catch { setResult({ frames: [], missing: "unknown", keep: 100 }); }
    setLoading(false);
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (open) load(); }, []);

  if (result === null) {
    if (open) return <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>{t("item.frames.loading")}</div>;
    return (
      <button className="btn btn-xs btn-secondary" style={{ marginBottom: 12 }} disabled={loading}
        onClick={(e) => { e.stopPropagation(); load(); }}>
        {loading ? t("item.frames.loading") : t("item.frames.show")}
      </button>
    );
  }
  const frames = result.frames;
  if (!frames.length) {
    const why = result.missing || "unknown";
    return (
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
        {t(`item.frames.missing.${why}`, { keep: result.keep })}
        {why !== "deleted" && (<>{" "}
          <Link to="/settings/video#before-after" onClick={(e) => e.stopPropagation()}
            style={{ color: "var(--accent-text)" }}>{t("item.frames.settingsLink")}</Link>
        </>)}
      </div>
    );
  }
  const frame = frames[Math.min(index, frames.length - 1)];
  const moveTo = (clientX: number) => {
    const r = box.current?.getBoundingClientRect();
    if (r && r.width) setSplit(Math.min(100, Math.max(0, ((clientX - r.left) / r.width) * 100)));
  };
  return (
    <div onClick={(e) => e.stopPropagation()} style={{ marginBottom: 12, maxWidth: 720 }}>
      <div ref={box} style={{ position: "relative", lineHeight: 0, userSelect: "none", touchAction: "none", cursor: "ew-resize" }}
        onPointerDown={(e) => { e.currentTarget.setPointerCapture?.(e.pointerId); moveTo(e.clientX); }}
        onPointerMove={(e) => { if (e.buttons) moveTo(e.clientX); }}>
        <img src={frame.after} alt={t("item.frames.after")} draggable={false}
          style={{ width: "100%", display: "block", borderRadius: 4 }} />
        <img src={frame.before} alt={t("item.frames.before")} draggable={false}
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", borderRadius: 4,
                   clipPath: `inset(0 ${100 - split}% 0 0)` }} />
        <div style={{ position: "absolute", top: 0, bottom: 0, left: `${split}%`, width: 2, marginLeft: -1,
                      background: "white", boxShadow: "0 0 4px rgba(0, 0, 0, 0.6)", pointerEvents: "none" }} />
        <span style={{ ...LABEL, left: 8 }}>{t("item.frames.before")}</span>
        <span style={{ ...LABEL, right: 8 }}>{t("item.frames.after")}</span>
      </div>
      <input type="range" min={0} max={100} step={1} value={Math.round(split)} aria-label={t("item.frames.slider")}
        onChange={(e) => setSplit(Number(e.target.value))} style={{ width: "100%", accentColor: "var(--accent)" }} />
      {frames.length > 1 && (
        <div role="group" aria-label={t("item.frames.moments")} style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {frames.map((f, i) => (
            <button key={i} className={i === index ? "btn btn-xs btn-primary" : "btn btn-xs btn-secondary"} aria-pressed={i === index} onClick={() => setIndex(i)}>
              {clock(f.t)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
