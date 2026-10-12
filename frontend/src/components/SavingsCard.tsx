import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { fmtBytes, fmtNum } from "../fmt";
import { useDialog } from "../useDialog";
import { useToast } from "../useToast";

const W = 1200, H = 630;  // the size link previews use

/** The card's lines, in the interface language. */
export function cardLines(t: (key: string, opts?: Record<string, unknown>) => string,
                          saved: number, files: number, before: number) {
  const pct = before > 0 ? Math.round(saved / before * 100) : 0;
  return {
    headline: fmtBytes(saved),
    saved: t("dashboard:share.saved"),
    detail: t("dashboard:share.files", { count: files, num: fmtNum(files), pct }),
    beforeAfter: before > 0 ? t("dashboard:share.beforeAfter", { before: fmtBytes(before), after: fmtBytes(before - saved) }) : "",
  };
}

async function draw(canvas: HTMLCanvasElement, lines: ReturnType<typeof cardLines>) {
  const ctx = canvas.getContext("2d")!;
  const bg = ctx.createLinearGradient(0, 0, W, H);
  bg.addColorStop(0, "#14122b");
  bg.addColorStop(1, "#33206e");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);
  const logo = new Image();
  logo.src = "/shrinkerr-logo.svg";
  try {
    await logo.decode();
    const h = 56;
    ctx.drawImage(logo, 72, 64, (logo.naturalWidth / logo.naturalHeight) * h || 240, h);
  } catch { /* no logo: the text still reads */ }
  const font = (weight: number, size: number) => `${weight} ${size}px -apple-system, "Segoe UI", Roboto, sans-serif`;
  ctx.fillStyle = "#ffffff";
  ctx.font = font(800, 150);
  ctx.textBaseline = "alphabetic";
  ctx.fillText(lines.headline, 72, 330);
  const width = ctx.measureText(lines.headline).width;
  ctx.fillStyle = "#b9a8ff";
  ctx.font = font(600, 56);
  ctx.fillText(lines.saved, 72 + width + 24, 330);
  ctx.fillStyle = "#e6e2ff";
  ctx.font = font(500, 40);
  ctx.fillText(lines.detail, 72, 410);
  ctx.fillStyle = "#a59ccc";
  ctx.font = font(400, 34);
  if (lines.beforeAfter) ctx.fillText(lines.beforeAfter, 72, 462);
  ctx.font = font(400, 28);
  ctx.fillText("github.com/I-IAL9000/shrinkerr", 72, H - 64);
}

/**
 * Dashboard → Share (v0.10.0): an image of the space Shrinkerr has saved —
 * totals only, no file names — to download, copy or share.
 */
export default function SavingsCard({ saved, files, before, onClose }: {
  saved: number; files: number; before: number; onClose: () => void;
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const toast = useToast();
  const panelRef = useRef<HTMLDivElement>(null);
  const dialog = useDialog(panelRef, onClose, t("dashboard:share.title"));
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [url, setUrl] = useState("");
  const [blob, setBlob] = useState<Blob | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    draw(canvas, cardLines(t, saved, files, before)).then(() => {
      setUrl(canvas.toDataURL("image/png"));
      canvas.toBlob(b => setBlob(b), "image/png");
    });
  }, [saved, files, before, t]);

  const file = blob ? new File([blob], "shrinkerr-savings.png", { type: "image/png" }) : null;
  const canShare = !!file && typeof navigator.canShare === "function" && navigator.canShare({ files: [file] });
  const canCopy = !!blob && typeof ClipboardItem !== "undefined" && !!navigator.clipboard?.write;
  const copy = async () => {
    try {
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob! })]);
      toast(t("dashboard:share.copied"), "success");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const share = async () => {
    try {
      await navigator.share({ files: [file!], text: cardLines(t, saved, files, before).detail });
    } catch { /* cancelled */ }
  };

  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000,
      display: "flex", alignItems: "center", justifyContent: "center", padding: 20 }}>
      <div ref={panelRef} {...dialog} onClick={e => e.stopPropagation()} style={{ background: "var(--bg-card)",
        border: "1px solid var(--border)", borderRadius: 8, width: "100%", maxWidth: 640, padding: "16px 18px" }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)", marginBottom: 10 }}>{t("dashboard:share.title")}</div>
        <canvas ref={canvasRef} width={W} height={H} style={{ display: "none" }} />
        {url && <img src={url} alt={cardLines(t, saved, files, before).detail} style={{ width: "100%", borderRadius: 6, display: "block" }} />}
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>{t("dashboard:share.note")}</div>
        <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
          <button className="btn btn-secondary" onClick={onClose}>{t("common:actions.close")}</button>
          {canCopy && <button className="btn btn-secondary" onClick={copy}>{t("dashboard:share.copy")}</button>}
          {canShare && <button className="btn btn-secondary" onClick={share}>{t("dashboard:share.share")}</button>}
          <a className="btn btn-primary" href={url || undefined} download="shrinkerr-savings.png"
            aria-disabled={!url}>{t("dashboard:share.download")}</a>
        </div>
      </div>
    </div>
  );
}
