import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { AudioTrack, Job } from "../types";
import { getJobPlan, updateAudioTracks, updateSubtitleTracks, type JobPlan } from "../api";
import { encoderPresetLabel, encoderSettingsLabel, jobEncoderSettings } from "../utils/encoderLabel";
import { fmtBytes } from "../fmt";
import { trackLanguageName } from "../utils/languages";
import { useToast } from "../useToast";
import AudioTrackRow from "./AudioTrackRow";
import SubTrackRow from "./SubTrackRow";

// A plan's lossless tracks by name, once each: "TrueHD Atmos, DTS-HD MA".
function losslessNames(audio: AudioTrack[], streams: number[]): string {
  const names = audio.filter(tr => streams.includes(tr.stream_index)).map(tr => {
    const codec = tr.codec.toLowerCase();
    const text = `${tr.profile || ""} ${tr.title || ""}`.toLowerCase();
    if (codec === "dts") return /dts[:-]?x/.test(text) ? "DTS:X" : (tr.profile || "DTS").split(" + ")[0];
    const name = codec === "truehd" ? "TrueHD" : codec.startsWith("pcm") ? "PCM" : codec.toUpperCase();
    return text.includes("atmos") ? `${name} Atmos` : name;
  });
  return [...new Set(names)].join(", ");
}

// What a pending job will do (v0.10.0): the video conversion, every track of
// its file and whether the job removes it, about what it saves and what
// becomes of the original. Ticking a track saves it on the file as the user's
// choice (as in the Scanner) and the server updates the job to follow the
// file. The ticks show the job's own lists — what it will actually do.
export default function PendingJobPlan({ job, encodingDefaults, onChanged }: {
  job: Job;
  encodingDefaults?: any;
  onChanged?: () => void;
}) {
  const { t } = useTranslation(["queue", "fileDetail", "settingsMedia"]);
  const toast = useToast();
  const [plan, setPlan] = useState<JobPlan | null>(null);
  const [failed, setFailed] = useState(false);
  const [saving, setSaving] = useState(false);

  // Reloads when the job changes under it too (a language change, an edit
  // in the Scanner, Edit settings) — the queue polls. Any field: a list of
  // them left the VMAF target out, and the plan kept the quality it had.
  const version = JSON.stringify(job);
  useEffect(() => {
    let cancelled = false;
    getJobPlan(job.id)
      .then(p => { if (!cancelled) { setPlan(p); setFailed(false); } })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [job.id, version]);

  const toggle = async (kind: "audio" | "subtitles", streamIndex: number) => {
    if (!plan || plan.scan_id == null || saving) return;
    const next = plan[kind].map(tr => tr.stream_index === streamIndex
      ? { ...tr, keep: tr.remove, remove: !tr.remove, manual: true } : tr);
    setPlan({ ...plan, [kind]: next });
    setSaving(true);
    try {
      const body = JSON.stringify(next.map(({ remove: _, ...tr }) => tr));
      await (kind === "audio" ? updateAudioTracks(plan.scan_id, body) : updateSubtitleTracks(plan.scan_id, body));
      setPlan(await getJobPlan(job.id));
      onChanged?.();
    } catch (e) {
      toast((e as Error).message || t("queue:plan.saveFailed"), "error");
      getJobPlan(job.id).then(setPlan).catch(() => {});
    } finally {
      setSaving(false);
    }
  };

  const box: React.CSSProperties = {
    padding: "10px 12px 10px 36px", fontSize: 12, lineHeight: 1.6,
    background: "rgba(71,191,255,0.03)", borderBottom: "1px solid var(--bg-card)",
  };
  if (failed) return <div style={{ ...box, color: "var(--text-muted)" }}>{t("queue:plan.loadFailed")}</div>;
  if (!plan) return <div style={{ ...box, color: "var(--text-muted)" }}>{t("queue:plan.loading")}</div>;

  const converts = plan.job_type === "convert" || plan.job_type === "combined";
  const encoder = job.encoder || encodingDefaults?.default_encoder || "nvenc";
  const encoderName = t(`settingsMedia:video.encoderNames.${encoder}`, { defaultValue: encoder });
  const settings = jobEncoderSettings(job, encodingDefaults);
  // A VMAF target job's quality is the search's to find (v0.10.0): it said
  // "P3 / CQ 27", the job's queued quality.
  const preset = encoderPresetLabel(encoder, settings);
  // Its Dolby Vision (v0.10.0): Settings → Video → Dolby Vision, as the encode
  // decides it. One it can't keep or drop keeps the video as it is.
  const dv = plan.dolby_vision;
  const dvParams = dv && { profile: dv.profile, hdr: (dv.hdr || "").toUpperCase(),
    why: dv.why ? t(`queue:plan.dolbyVisionWhy.${dv.why}`) : "" };
  const lines: { text: string; warn?: boolean }[] = [
    dv?.action === "skip" ? { text: t("queue:plan.dolbyVision.skip", dvParams!), warn: true } : {
      text: !converts ? t("queue:plan.noVideo")
        : (job.vmaf_target ?? 0) > 0
          ? preset
            ? t("queue:plan.videoTarget", { encoder: encoderName, preset, target: job.vmaf_target })
            : t("queue:plan.videoTargetNoPreset", { encoder: encoderName, target: job.vmaf_target })
          : t("queue:plan.video", { encoder: encoderName, settings: encoderSettingsLabel(encoder, settings) }),
    },
    ...(dv && dv.action !== "skip"
      ? [{ text: t(`queue:plan.dolbyVision.${dv.action === "keep" && dv.why ? "keepWhy" : dv.action}`, dvParams!) }] : []),
    // Its lossless audio (v0.10.0): Settings' or Add to Queue's choice.
    ...(plan.lossless?.convert.length ? [{ text: t("queue:plan.losslessConvert", {
      tracks: losslessNames(plan.audio, plan.lossless.convert),
      target: `${plan.lossless.codec.toUpperCase()} ${plan.lossless.bitrate}k`,
    }) }] : []),
    ...(plan.lossless?.keep.length ? [{ text: t("queue:plan.losslessKeep", { tracks: losslessNames(plan.audio, plan.lossless.keep) }) }] : []),
    // Its audio with no language, detected when it runs (Auto-detect languages)
    ...(plan.detect_languages ? [{ text: t("queue:plan.detectLanguages", { count: plan.detect_languages }) }] : []),
    ...(plan.native_first ? [{ text: t("queue:plan.nativeFirst", { language: trackLanguageName(plan.native_first) }) }] : []),
    // Fix for direct play (v0.10.0): Hi10P's fix is the conversion above.
    ...(plan.direct_play || []).filter(fix => fix !== "video").map(fix => ({
      text: t(`queue:plan.fix.${fix === "subs" && plan.image_subs_keep_original === false ? "subsReplace" : fix}`),
    })),
    {
      text: plan.estimated_savings > 0
        ? t("queue:plan.size", { before: fmtBytes(plan.file_size), after: fmtBytes(plan.file_size - plan.estimated_savings), saved: fmtBytes(plan.estimated_savings) })
        : t("queue:plan.sizeSame", { size: fmtBytes(plan.file_size) }),
    },
    plan.originals.action === "keep"
      ? { text: t("queue:plan.originalKeep", { count: plan.originals.days }) }
      : plan.originals.action === "trash"
        ? { text: t("queue:plan.originalTrash") }
        : { text: t("queue:plan.originalDelete"), warn: true },
  ];
  const shown = <T extends { remove: boolean }>(tr: T) => ({ ...tr, keep: !tr.remove });
  const embedded = plan.subtitles.filter(s => !s.external);
  const external = plan.subtitles.filter(s => s.external);
  const heading: React.CSSProperties = { fontSize: 11, color: "var(--text-muted)", fontWeight: 600, marginTop: 10, marginBottom: 2 };

  return (
    <div style={box}>
      <div style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 600, marginBottom: 4 }}>{t("queue:plan.title")}</div>
      <ul style={{ margin: 0, paddingLeft: 16 }}>
        {lines.map((l, i) => (
          <li key={i} style={{ color: l.warn ? "var(--warning)" : "var(--text-secondary)" }}>{l.text}</li>
        ))}
      </ul>
      {plan.scan_id == null ? (
        <div style={{ marginTop: 8, color: "var(--text-muted)" }}>{t("queue:plan.notScanned")}</div>
      ) : (
        <div style={{ opacity: saving ? 0.6 : 1 }}>
          <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-muted)" }}>{t("queue:plan.tickToRemove")}</div>
          <div style={heading}>{t("fileDetail:tracks.audioHeading")}</div>
          {plan.audio.length === 0
            ? <div style={{ color: "var(--text-muted)" }}>{t("fileDetail:tracks.noAudio")}</div>
            : plan.audio.map(tr => <AudioTrackRow key={tr.stream_index} track={shown(tr)} onToggle={(si) => toggle("audio", si)} />)}
          {plan.audio.length > 0 && plan.audio.every(tr => tr.remove) && (
            <div style={{ color: "var(--warning)", marginTop: 4 }}>{t("fileDetail:tracks.allAudioRemoved")}</div>
          )}
          <div style={heading}>{t("fileDetail:subtitles.heading")}</div>
          {embedded.length === 0
            ? <div style={{ color: "var(--text-muted)" }}>{t("fileDetail:subtitles.noEmbedded")}</div>
            : embedded.map(tr => <SubTrackRow key={tr.stream_index} track={shown(tr)} filePath={job.file_path} onToggle={(_, si) => toggle("subtitles", si)} />)}
          {external.length > 0 && (<>
            <div style={heading}>{t("fileDetail:subtitles.externalHeading")}</div>
            {external.map(tr => <SubTrackRow key={`ext-${tr.stream_index}`} track={shown(tr)} filePath={job.file_path} isExternal onToggle={(_, si) => toggle("subtitles", si)} />)}
          </>)}
        </div>
      )}
    </div>
  );
}
