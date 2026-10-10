import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  addMediaDir, getEncoderCaps, getEncodingSettings, getLanguagePreview, getMediaDirs,
  getQualityPresets, login, removeMediaDir, startScan, updateEncodingSettings,
  type EncoderCaps, type PreviewTrack, type QualityPreset,
} from "../api";
import FolderBrowser from "./FolderBrowser";
import LanguagePicker from "./LanguagePicker";
import { useToast } from "../useToast";

// Setup wizard v2 (v0.10.0): one step at a time — media folders, hardware,
// quality, languages (with a preview on a few of your files), originals and
// a password — then the scan starts and the Scanner opens. It used to be a
// checklist of links into Settings that vanished once a scan existed. The
// scan runs last so it already uses the languages chosen here.

const STEPS = ["folders", "hardware", "quality", "languages", "originals", "password", "finish"] as const;
type Step = (typeof STEPS)[number];
type PresetChoice = QualityPreset["id"] | "custom";
type Originals = "keep" | "trash" | "delete";

// UI language → the ISO 639-2 code tracks use, for the "Add English" hint.
const UI_TO_TRACK_LANG: Record<string, string> = { en: "eng", es: "spa" };
// Shown next to a preset: the setting it sets.
const QUALITY_UNIT: Record<string, string> = {
  nvenc_cq: "CQ", libx265_crf: "CRF", qsv_cq: "ICQ", vaapi_qp: "QP", videotoolbox_quality: "q:v",
};

function ProtectForm({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation(["dashboard"]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await updateEncodingSettings({ auth_enabled: true, auth_username: username, auth_password: password });
      await login(username, password);  // stay signed in now that a password is required
      onDone();
    } catch (err: any) {
      setError(t("dashboard:setup.protect.failed", { error: err?.message || "" }));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="wizard-row" onSubmit={e => { e.preventDefault(); submit(); }}>
      <input type="text" autoComplete="username" value={username} onChange={e => setUsername(e.target.value)}
        aria-label={t("dashboard:setup.protect.username")} placeholder={t("dashboard:setup.protect.username")} />
      <input type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)}
        aria-label={t("dashboard:setup.protect.password")} placeholder={t("dashboard:setup.protect.password")} />
      <button type="submit" className="btn btn-primary" disabled={busy || !username.trim() || !password}>
        {t("dashboard:setup.protect.action")}
      </button>
      {error && <div className="wizard-error" role="alert">{error}</div>}
    </form>
  );
}

function Option({ checked, onChange, name, title, children }: {
  checked: boolean; onChange: () => void; name: string; title: ReactNode; children?: ReactNode;
}) {
  return (
    <label className={`wizard-option ${checked ? "selected" : ""}`}>
      <input type="radio" name={name} checked={checked} onChange={onChange} />
      <span>
        <span className="wizard-option-title">{title}</span>
        {children && <span className="wizard-option-help">{children}</span>}
      </span>
    </label>
  );
}

function TrackChips({ tracks, kind, langName }: { tracks: PreviewTrack[]; kind: "audio" | "subs"; langName: (c: string) => string }) {
  const { t } = useTranslation(["dashboard"]);
  if (tracks.length === 0) return <span className="wizard-muted">—</span>;
  return (
    <>
      {tracks.map((tr, i) => (
        <span key={i} className={`preview-track ${tr.keep ? "kept" : "removed"}`}>
          {langName(tr.language)}
          {kind === "audio" && tr.channels ? ` ${tr.channels}ch` : ""}
          {tr.forced ? ` · ${t("dashboard:setup.languages.forced")}` : ""}
          <span className="sr-only"> ({tr.keep ? t("dashboard:setup.languages.kept") : t("dashboard:setup.languages.removed")})</span>
        </span>
      ))}
    </>
  );
}

export default function SetupWizard({ setup, onClose, onChanged }: {
  setup: any;
  /** Skip or finish: the Dashboard marks setup as done. */
  onClose: () => Promise<void> | void;
  onChanged: () => void;
}) {
  const { t, i18n } = useTranslation(["dashboard", "settingsMedia", "settingsSystem", "common"]);
  const navigate = useNavigate();
  const toast = useToast();
  const [step, setStep] = useState<Step>("folders");
  const [busy, setBusy] = useState(false);

  const [dirs, setDirs] = useState<any[]>([]);
  const [newPath, setNewPath] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [browserOpen, setBrowserOpen] = useState(false);
  const [caps, setCaps] = useState<EncoderCaps | null>(null);
  const [encoder, setEncoder] = useState("nvenc");
  const [presets, setPresets] = useState<QualityPreset[]>([]);
  const [preset, setPreset] = useState<PresetChoice>("quality");
  const [custom, setCustom] = useState(false);  // saved quality matches no preset
  const [audioLangs, setAudioLangs] = useState<string[]>([]);
  const [subLangs, setSubLangs] = useState<string[]>([]);
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof getLanguagePreview>> | null>(null);
  const [originals, setOriginals] = useState<Originals>("keep");
  const [days, setDays] = useState(7);

  const loadDirs = () => getMediaDirs().then((r: any) => setDirs(Array.isArray(r) ? r : r.dirs || [])).catch(() => {});
  useEffect(() => {
    loadDirs();
    getEncodingSettings().then((s: any) => {
      setEncoder(s.default_encoder || "nvenc");
      setAudioLangs(s.always_keep_languages || []);
      setSubLangs(s.sub_keep_languages || []);
      const d = Number(s.backup_original_days) || 0;
      setOriginals(d > 0 ? "keep" : s.trash_original_after_conversion ? "trash" : "delete");
      if (d > 0) setDays(d);
    }).catch(() => {});
    getEncoderCaps().then(setCaps).catch(() => {});
  }, []);

  // The presets for the chosen encoder, and which one the settings are. A
  // new install starts on Quality; someone who has converted with their own
  // values keeps them unless they pick a preset.
  useEffect(() => {
    getQualityPresets(encoder).then(r => {
      setPresets(r.presets);
      setCustom(!r.current);
      setPreset((r.current as PresetChoice) || (setup.has_jobs ? "custom" : "quality"));
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [encoder]);

  // Live preview of the languages on a few files (the first call probes them).
  useEffect(() => {
    if (step !== "languages") return;
    const timer = setTimeout(() => {
      getLanguagePreview(audioLangs, subLangs).then(setPreview).catch(() => setPreview({ files: [] }));
    }, 300);
    return () => clearTimeout(timer);
  }, [step, audioLangs, subLangs]);

  const index = STEPS.indexOf(step);
  const langName = (code: string) => t(`settingsMedia:languages.${code}`, { defaultValue: code });
  const available = caps?.available?.length ? caps.available : ["nvenc", "qsv", "vaapi", "videotoolbox", "libx265"];
  const suggested = UI_TO_TRACK_LANG[i18n.language];

  const save = async (values: Record<string, unknown>) => {
    try {
      await updateEncodingSettings(values);
      return true;
    } catch (e: any) {
      toast(t("dashboard:setup.saveFailed", { error: e?.message || String(e) }), "error");
      return false;
    }
  };

  const next = async () => {
    setBusy(true);
    try {
      let ok = true;
      if (step === "hardware") ok = await save({ default_encoder: encoder });
      if (step === "quality" && preset !== "custom") {
        ok = await save(presets.find(p => p.id === preset)!.settings);
      }
      if (step === "languages") {
        // Choosing subtitle languages is what turns subtitle cleanup on.
        ok = await save({ always_keep_languages: audioLangs, sub_keep_languages: subLangs, sub_cleanup_enabled: subLangs.length > 0 });
      }
      if (step === "originals") {
        ok = await save({ backup_original_days: originals === "keep" ? days : 0, trash_original_after_conversion: originals === "trash" });
      }
      if (ok) setStep(STEPS[index + 1]);
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true);
    try {
      await startScan(dirs.map(d => d.path)).catch(() => {});  // a scan already running is fine
      await onClose();
      navigate("/scanner");
    } finally {
      setBusy(false);
    }
  };

  const addDir = async () => {
    if (!newPath.trim()) return;
    try {
      await addMediaDir(newPath.trim(), newLabel);
      setNewPath("");
      setNewLabel("");
      loadDirs();
    } catch (e: any) {
      toast(e?.message || t("settingsMedia:directories.addFailed"), "error");
    }
  };

  const title = (s: Step) => t(`dashboard:setup.${s}.title`);

  let body: ReactNode = null;
  if (step === "folders") {
    body = (
      <>
        {dirs.length === 0 && <p className="wizard-muted">{t("dashboard:setup.folders.empty")}</p>}
        <ul className="wizard-list">
          {dirs.map(d => (
            <li key={d.id}>
              <span className="wizard-path">{d.path}</span>
              {d.label && <span className="wizard-tag">{d.label}</span>}
              <button type="button" className="wizard-remove" aria-label={`${t("common:actions.remove")} ${d.path}`}
                onClick={async () => { await removeMediaDir(d.id); loadDirs(); }}>&times;</button>
            </li>
          ))}
        </ul>
        <div className="wizard-row">
          <button type="button" className="btn btn-secondary" onClick={() => setBrowserOpen(true)}>{t("settingsMedia:directories.browse")}</button>
          <input value={newPath} onChange={e => setNewPath(e.target.value)} style={{ flex: "1 1 220px" }}
            onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addDir(); } }}
            aria-label={t("settingsMedia:directories.pathPlaceholder")} placeholder={t("settingsMedia:directories.pathPlaceholder")} />
          <select value={newLabel} onChange={e => setNewLabel(e.target.value)} aria-label={t("common:labels.folderType")}>
            <option value="">{t("settingsMedia:directories.typeOptional")}</option>
            <option value="Movies">{t("settingsMedia:directories.types.movies")}</option>
            <option value="TV Shows">{t("settingsMedia:directories.types.tvShows")}</option>
            <option value="Other">{t("settingsMedia:directories.types.other")}</option>
          </select>
          <button type="button" className="btn btn-secondary" onClick={addDir} disabled={!newPath.trim()}>{t("settingsMedia:directories.add")}</button>
        </div>
        <FolderBrowser isOpen={browserOpen} initialPath="/media"
          onSelect={p => { setNewPath(p); setBrowserOpen(false); }} onCancel={() => setBrowserOpen(false)} />
      </>
    );
  } else if (step === "hardware") {
    body = available.map(enc => (
      <Option key={enc} name="wizard-encoder" checked={encoder === enc} onChange={() => setEncoder(enc)}
        title={t(`settingsMedia:video.encoderNames.${enc}`)}>
        {t(`settingsMedia:video.encoderHelp.${enc}`)}
      </Option>
    ));
  } else if (step === "quality") {
    body = (
      <>
        {presets.map(p => {
          const [key, value] = Object.entries(p.settings)[0] || [];
          return (
            <Option key={p.id} name="wizard-quality" checked={preset === p.id} onChange={() => setPreset(p.id)}
              title={<>{t(`dashboard:setup.quality.presets.${p.id}.name`)} <span className="wizard-savings">{t("dashboard:setup.quality.savings", { pct: p.savings_pct })}</span></>}>
              {t(`dashboard:setup.quality.presets.${p.id}.desc`)} {key && <span className="wizard-muted">({QUALITY_UNIT[key] || key} {value})</span>}
            </Option>
          );
        })}
        {custom && (
          <Option name="wizard-quality" checked={preset === "custom"} onChange={() => setPreset("custom")} title={t("dashboard:setup.quality.custom.name")}>
            {t("dashboard:setup.quality.custom.desc")}
          </Option>
        )}
      </>
    );
  } else if (step === "languages") {
    body = (
      <>
        <div className="wizard-field">
          <div className="wizard-label">{t("dashboard:setup.languages.audio")}</div>
          <LanguagePicker value={audioLangs} onChange={setAudioLangs} label={t("dashboard:setup.languages.audio")} />
          {suggested && !audioLangs.includes(suggested) && (
            <button type="button" className="btn btn-secondary wizard-suggest" onClick={() => setAudioLangs([...audioLangs, suggested])}>
              {t("dashboard:setup.languages.suggest", { language: langName(suggested) })}
            </button>
          )}
        </div>
        <div className="wizard-field">
          <div className="wizard-label">{t("dashboard:setup.languages.subs")}</div>
          <LanguagePicker value={subLangs} onChange={setSubLangs} label={t("dashboard:setup.languages.subs")} />
          <div className="wizard-muted">{t("dashboard:setup.languages.subsHelp")}</div>
        </div>
        <div className="wizard-field">
          <div className="wizard-label">{t("dashboard:setup.languages.preview")}</div>
          {!preview && <div className="wizard-muted" role="status">{t("dashboard:setup.languages.previewLoading")}</div>}
          {preview && preview.files.length === 0 && <div className="wizard-muted">{t("dashboard:setup.languages.previewEmpty")}</div>}
          {preview && preview.files.length > 0 && (
            <div className="wizard-preview" aria-live="polite">
              {preview.files.map((f, i) => (
                <div key={i} className="wizard-preview-file">
                  <div className="wizard-preview-name" title={f.name}>
                    {f.name} <span className="wizard-muted">· {t("dashboard:setup.languages.original", { language: langName(f.native) })}</span>
                  </div>
                  <div><span className="wizard-muted">{t("dashboard:setup.languages.audioShort")}</span> <TrackChips tracks={f.audio} kind="audio" langName={langName} /></div>
                  <div><span className="wizard-muted">{t("dashboard:setup.languages.subsShort")}</span> <TrackChips tracks={f.subs} kind="subs" langName={langName} /></div>
                </div>
              ))}
            </div>
          )}
        </div>
      </>
    );
  } else if (step === "originals") {
    body = (
      <>
        <Option name="wizard-originals" checked={originals === "keep"} onChange={() => setOriginals("keep")}
          title={<>{t("settingsSystem:automation.originals.keepFor")}{" "}
            <input type="number" min={1} value={days} className="wizard-days"
              aria-label={t("settingsSystem:automation.originals.keepDays")}
              onChange={e => { setDays(Math.max(1, parseInt(e.target.value) || 1)); setOriginals("keep"); }} />{" "}
            {t("settingsSystem:automation.originals.keepDays")}</>}>
          {t("settingsSystem:automation.originals.keepHelp")}
        </Option>
        <Option name="wizard-originals" checked={originals === "trash"} onChange={() => setOriginals("trash")}
          title={t("settingsSystem:automation.originals.trashOption")}>{t("settingsSystem:automation.originals.trashHelp")}</Option>
        <Option name="wizard-originals" checked={originals === "delete"} onChange={() => setOriginals("delete")}
          title={t("settingsSystem:automation.originals.deleteOption")}>{t("settingsSystem:automation.originals.deleteHelp")}</Option>
      </>
    );
  } else if (step === "password") {
    body = setup.has_auth
      ? <p className="wizard-done">{t("dashboard:setup.protect.descriptionDone")}</p>
      : <ProtectForm onDone={() => { onChanged(); setStep("finish"); }} />;
  } else {
    body = <p>{t("dashboard:setup.finish.summary", { count: dirs.length })}</p>;
  }

  return (
    <div className="wizard">
      <div className="wizard-head">
        <img src="/favicon.svg" alt="" width="56" height="56" />
        <h1>{t("dashboard:setup.welcome")}</h1>
        <p className="wizard-muted">{t("dashboard:setup.intro")}</p>
      </div>

      <ol className="wizard-steps">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={s === step ? "step" : undefined} className={i < index ? "done" : s === step ? "current" : ""}>
            <button type="button" disabled={i > index || busy} onClick={() => setStep(s)}>
              <span className="wizard-step-num" aria-hidden="true">{i < index ? "✓" : i + 1}</span>
              <span className="wizard-step-name">{title(s)}</span>
            </button>
          </li>
        ))}
      </ol>

      <section className="wizard-card" aria-labelledby="wizard-step-title">
        <div className="wizard-muted">{t("dashboard:setup.stepOf", { n: index + 1, total: STEPS.length })}</div>
        <h2 id="wizard-step-title">{title(step)}</h2>
        <p className="wizard-muted">{t(`dashboard:setup.${step}.description`)}</p>
        {body}
        <div className="wizard-nav">
          {index > 0 && <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setStep(STEPS[index - 1])}>{t("dashboard:setup.back")}</button>}
          <span style={{ flex: 1 }} />
          {step === "finish" ? (
            <button type="button" className="btn btn-primary" disabled={busy} onClick={finish}>{t("dashboard:setup.finish.action")}</button>
          ) : (
            <button type="button" className="btn btn-primary" onClick={next}
              disabled={busy || (step === "folders" && dirs.length === 0)}>
              {step === "password" && !setup.has_auth ? t("dashboard:setup.password.skip") : t("dashboard:setup.next")}
            </button>
          )}
        </div>
      </section>

      <div style={{ textAlign: "center", marginTop: 16 }}>
        <button type="button" className="wizard-skip" onClick={() => onClose()}>{t("dashboard:setup.skip")}</button>
      </div>
    </div>
  );
}
