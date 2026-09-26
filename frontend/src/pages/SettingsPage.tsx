import { useState, useEffect, useRef } from "react";
import { useRangeFill } from "../useRangeFill";
import FolderBrowser from "../components/FolderBrowser";
import RenamingSettings from "../components/RenamingSettings";
import { vmafColor } from "../utils/vmaf";
import {
  getMediaDirs, addMediaDir, updateMediaDir, removeMediaDir,
  getEncodingSettings, updateEncodingSettings, testApiKey, getApiKey,
  createEncodingRule, updateEncodingRule, deleteEncodingRule,
  reorderEncodingRules, syncPlexRuleMetadata, getPlexOptions,
  getConditionOptions, testNotifications, importSettings,
  listBackups, createBackup, deleteBackup, downloadBackupUrl, restoreBackup,
  plexAuthStart, plexAuthCheck, plexAuthResources, plexAuthSave,
  plexAuthDisconnect, plexAuthStatus,
  getVersion, getChangelog,
  getVmafRemeasureStatus, startVmafRemeasure,
  getEncoderCaps, regenerateApiKey,
  type PlexAuthStatus, type PlexServer, type ChangelogEntry,
  type EncoderCaps,
} from "../api";
import ChangelogEntryView from "../components/ChangelogEntry";
import ChangelogModal from "../components/ChangelogModal";
import { useToast } from "../useToast";
import { useTranslation, Trans } from "react-i18next";
import { LANGUAGES, setLanguage, type LanguageCode } from "../i18n";

// Preset ids whose label/description live in settingsMedia:options.presets.<id>.
const PRESET_IDS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"];

// label/desc text for these lists lives in settingsMedia:options.* — keyed by value.
const TARGET_CODECS = [
  { value: "hevc" },
  { value: "av1" },
];

const SOURCE_CODECS = [
  { value: "h264", always: false, defaultOn: true },
  { value: "mpeg2", always: false, defaultOn: true },
  { value: "mpeg4", always: false, defaultOn: true },
  { value: "vc1", always: false, defaultOn: true },
  { value: "msmpeg4v3", always: false, defaultOn: false },
  { value: "vp9", always: false, defaultOn: false },
  { value: "hevc", always: false, defaultOn: false },
  { value: "av1", always: false, defaultOn: false },
];

// Display names live in settingsMedia:languages.<code>.
const ALL_LANGUAGES = [
  "eng", "isl", "ice", "aar", "afr", "aka", "amh", "ara", "arg", "asm",
  "aze", "bak", "bam", "bel", "ben", "bos", "bre", "bul", "cat", "ces",
  "cze", "chi", "zho", "cmn", "cor", "cos", "cre", "cym", "dan", "deu",
  "ger", "div", "dut", "nld", "dzo", "ell", "gre", "epo", "est", "eus",
  "ewe", "fao", "fas", "per", "fij", "fin", "fra", "fre", "fry", "ful",
  "gla", "gle", "glg", "grn", "guj", "hat", "hau", "heb", "her", "hin",
  "hrv", "hun", "hye", "arm", "ibo", "ido", "ind", "ita", "jav", "jpn",
  "kal", "kan", "kas", "kat", "geo", "kaz", "khm", "kin", "kir", "kor",
  "kur", "lao", "lat", "lav", "lit", "ltz", "mac", "mkd", "mal", "mar",
  "may", "msa", "mlg", "mlt", "mon", "mri", "mya", "bur", "nep", "nob",
  "nor", "nno", "oci", "ori", "orm", "pan", "pol", "por", "pus", "que",
  "roh", "ron", "rum", "run", "rus", "sag", "san", "sin", "slk", "slo",
  "slv", "sme", "smo", "sna", "snd", "som", "sot", "spa", "sqi", "alb",
  "srp", "ssw", "sun", "swa", "swe", "tam", "tat", "tel", "tgk", "tgl",
  "tha", "tib", "bod", "tir", "ton", "tsn", "tso", "tuk", "tur", "twi",
  "uig", "ukr", "urd", "uzb", "vie", "vol", "wln", "wol", "xho", "yid",
  "yor", "zul",
];

const AUDIO_CODECS = [
  { value: "copy" },
  { value: "aac" },
  { value: "ac3" },
  { value: "eac3" },
  { value: "opus" },
  { value: "flac" },
];

const RESOLUTION_OPTIONS = [
  { value: "copy" },
  { value: "1080p" },
  { value: "720p" },
  { value: "480p" },
];

const inputStyle: React.CSSProperties = {
  backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
  border: "1px solid var(--border)", padding: "8px 10px", borderRadius: 4, fontSize: 13,
  height: 36, boxSizing: "border-box",
};

const labelStyle = { color: "var(--text-muted)", fontSize: 13, marginBottom: 6 };
const helpStyle = { fontSize: 12, color: "var(--text-muted)", marginTop: 4, paddingLeft: 0 };
const sectionStyle = { background: "var(--bg-card)", padding: 20, borderRadius: 6, marginBottom: 12 };


/**
 * Re-measure suspect VMAF scores. Sub-component used inside the VMAF
 * settings panel. Polls the status endpoint to know whether a remeasure
 * pass is in flight + how many candidates are queued, exposes a button
 * that POSTs the start endpoint, and listens to websocket events for
 * live progress.
 */
function VmafRemeasureRow() {
  const toast = useToast();
  const { t } = useTranslation(["settingsMedia", "common"]);
  const [status, setStatus] = useState<{ running: boolean; candidates: number } | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number; current: string } | null>(null);
  const [outcome, setOutcome] = useState<{ rescued: number; unchanged: number; skipped: number } | null>(null);

  // Initial status fetch — poll every 5s while a pass is running so the
  // candidate count refreshes without needing a manual reload.
  useEffect(() => {
    let mounted = true;
    let pollHandle: ReturnType<typeof setInterval> | null = null;
    const tick = async () => {
      try {
        const s = await getVmafRemeasureStatus();
        if (!mounted) return;
        setStatus({ running: s.running, candidates: s.candidates });
      } catch {
        // ignore — endpoint may be unavailable transiently
      }
    };
    tick();
    pollHandle = setInterval(tick, 5000);
    return () => { mounted = false; if (pollHandle) clearInterval(pollHandle); };
  }, []);

  // Listen to websocket progress events from the running task. The
  // SettingsPage doesn't currently subscribe to its own WS connection,
  // so we hook the global one via a small bus.
  useEffect(() => {
    const handler = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === "vmaf_remeasure_progress") {
          setProgress({ done: data.done, total: data.total, current: data.current_file || "" });
        } else if (data.type === "vmaf_remeasure_complete") {
          setProgress(null);
          setOutcome({
            rescued: data.rescued || 0,
            unchanged: data.unchanged || 0,
            skipped: data.skipped || 0,
          });
          // Refresh the running flag + candidate count
          getVmafRemeasureStatus().then(s => setStatus({ running: s.running, candidates: s.candidates })).catch(() => {});
        }
      } catch {
        /* not JSON or not ours */
      }
    };
    // Subscribe to all WebSocket connections opened by the app — useWebSocket
    // multiplexes onto a single one, but we don't have direct access here.
    // Instead we install a window-level message listener and the existing
    // ws hook re-broadcasts via window.dispatchEvent.
    window.addEventListener("ws-message", handler as EventListener);
    return () => window.removeEventListener("ws-message", handler as EventListener);
  }, []);

  const start = async () => {
    try {
      const r = await startVmafRemeasure();
      if (r.started) {
        toast(t("settingsMedia:video.vmafRemeasure.startedToast", { count: r.total }), "success");
        setStatus(s => s ? { ...s, running: true } : { running: true, candidates: r.total });
        setOutcome(null);
      } else {
        toast(r.message || t("settingsMedia:video.vmafRemeasure.noCandidatesToast"), "info");
      }
    } catch (e: any) {
      toast(t("settingsMedia:video.vmafRemeasure.startFailed", { error: e?.message || t("settingsMedia:video.vmafRemeasure.unknownError") }), "error");
    }
  };

  if (!status) return null;
  const showCandidates = status.candidates > 0;
  return (
    <div style={{ marginTop: 16, padding: 12, background: "var(--bg-primary)", borderRadius: 4 }}>
      <div style={{ ...labelStyle, marginBottom: 4 }}>{t("settingsMedia:video.vmafRemeasure.title")}</div>
      <div style={{ ...helpStyle, marginBottom: 10, marginTop: 0 }}>
        {t("settingsMedia:video.vmafRemeasure.help")}
      </div>
      {progress && progress.total > 0 ? (
        <div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 6 }}>
            {t("settingsMedia:video.vmafRemeasure.progress", { done: progress.done, total: progress.total })} {progress.current ? `— ${progress.current}` : ""}
          </div>
          <div style={{ height: 6, background: "var(--bg-card)", borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              width: `${(progress.done / progress.total) * 100}%`,
              height: "100%", background: "var(--accent)",
              transition: "width 0.4s ease",
            }} />
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button
            className="btn btn-secondary"
            disabled={status.running || !showCandidates}
            onClick={start}
            style={{ fontSize: 12 }}
          >
            {status.running ? t("settingsMedia:video.vmafRemeasure.running") : t("settingsMedia:video.vmafRemeasure.button", { count: status.candidates })}
          </button>
          {!showCandidates && (
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {t("settingsMedia:video.vmafRemeasure.noCandidates")}
            </span>
          )}
          {outcome && (
            <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>
              <Trans i18nKey="settingsMedia:video.vmafRemeasure.lastPass" count={outcome.rescued}
                components={{ b: <strong style={{ color: "var(--success)" }} /> }} />
              {outcome.unchanged > 0 && t("settingsMedia:video.vmafRemeasure.unchanged", { count: outcome.unchanged })}
              {outcome.skipped > 0 && t("settingsMedia:video.vmafRemeasure.skipped", { count: outcome.skipped })}
            </span>
          )}
        </div>
      )}
    </div>
  );
}


export default function SettingsPage({ theme, onToggleTheme }: { theme: string; onToggleTheme: () => void }) {
  const toast = useToast();
  const { t, i18n } = useTranslation(["settings", "common"]);
  const pageRef = useRef<HTMLDivElement>(null);
  // Paint slider fills inside this page only. See useRangeFill.ts for why
  // this replaced the old document-body MutationObserver.
  useRangeFill(pageRef);

  // Scroll to section when arriving with a hash (e.g. /settings#connections).
  // Delayed so the target heading has rendered.
  useEffect(() => {
    if (!window.location.hash) return;
    const id = window.location.hash.slice(1);
    const t = setTimeout(() => {
      document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
    }, 150);
    return () => clearTimeout(t);
  }, []);

  const [dirs, setDirs] = useState<any[]>([]);
  const [newPath, setNewPath] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [encoding, setEncoding] = useState<any>(null);
  // Encoder caps from /api/stats/encoder-caps. Drives which options the
  // default-encoder dropdown surfaces. Null while loading; once loaded,
  // missing encoders won't appear. v0.3.68+.
  const [encoderCaps, setEncoderCaps] = useState<EncoderCaps | null>(null);
  const [encoderCapsRefreshing, setEncoderCapsRefreshing] = useState(false);
  const [saved, setSaved] = useState(false);
  const [langSearch, setLangSearch] = useState("");
  const [subLangSearch, setSubLangSearch] = useState("");
  const [tmdbKey, setTmdbKey] = useState("");
  const [showTmdbKey, setShowTmdbKey] = useState(false);
  const [tmdbTest, setTmdbTest] = useState<{ status: "idle" | "loading" | "success" | "error"; error?: string }>({ status: "idle" });
  const [plexUrl, setPlexUrl] = useState("");
  const [plexToken, setPlexToken] = useState("");
  const [plexPathMapping, setPlexPathMapping] = useState("");
  const [showPlexToken, setShowPlexToken] = useState(false);
  const [showManualPlex, setShowManualPlex] = useState(false);

  const [plexTest, setPlexTest] = useState<{ status: "idle" | "loading" | "success" | "error"; error?: string; serverName?: string; libraryCount?: number }>({ status: "idle" });

  // Plex Connect (PIN-based OAuth) state
  const [plexConn, setPlexConn] = useState<PlexAuthStatus>({ connected: false, server_url: "", server_name: "", user: null });
  const [plexAuthState, setPlexAuthState] = useState<"idle" | "waiting" | "picking-server" | "saving">("idle");
  const [plexServers, setPlexServers] = useState<PlexServer[]>([]);
  const [plexPendingToken, setPlexPendingToken] = useState("");
  const [plexPickerError, setPlexPickerError] = useState("");
  const [plexPickedUri, setPlexPickedUri] = useState("");
  const [browserOpen, setBrowserOpen] = useState(false);
  const [backupBrowserOpen, setBackupBrowserOpen] = useState(false);

  // ── Updates section state ──────────────────────────────────────────────
  // The Updates section shows the current running version, whether a newer
  // release is available upstream, and the 4 most recent changelog entries
  // parsed from CHANGELOG.md shipped with the image. "Check for updates"
  // hits /stats/version again (which is cached server-side for 6 hours,
  // so repeated clicks are cheap).
  const [versionInfo, setVersionInfo] = useState<{ current: string; latest: string | null; update_available: boolean } | null>(null);
  const [changelogEntries, setChangelogEntries] = useState<ChangelogEntry[] | null>(null);
  const [updateCheckLoading, setUpdateCheckLoading] = useState(false);
  const [changelogModalOpen, setChangelogModalOpen] = useState(false);
  useEffect(() => {
    // Load on mount. Parallel fetches — neither depends on the other.
    getVersion().then(setVersionInfo).catch(() => {});
    getChangelog(4).then((r) => setChangelogEntries(r.entries || [])).catch(() => setChangelogEntries([]));
  }, []);

  // Encoding rules state
  const [rules, setRules] = useState<any[]>([]);
  const [plexOpts, setPlexOpts] = useState<{ labels: string[]; collections: string[]; genres: string[]; libraries: any[] }>({ labels: [], collections: [], genres: [], libraries: [] });
  const [showAddRule, setShowAddRule] = useState(false);
  const [editingRuleId, setEditingRuleId] = useState<number | null>(null);
  const [ruleForm, setRuleForm] = useState<{
    name: string; match_mode: string; conditions: { type: string; operator: string; value: string }[];
    action: string; encoder: string; nvenc_preset: string; nvenc_cq: string;
    libx265_crf: string; libx265_preset: string; target_resolution: string; audio_codec: string; audio_bitrate: string;
    queue_priority: string;
  }>({ name: "", match_mode: "any", conditions: [{ type: "directory", operator: "is", value: "" }], action: "encode", encoder: "", nvenc_preset: "", nvenc_cq: "", libx265_crf: "", libx265_preset: "", target_resolution: "", audio_codec: "", audio_bitrate: "", queue_priority: "" });
  const [condOpts, setCondOpts] = useState<any>({ sources: [], resolutions: [], video_codecs: [], audio_codecs: [], media_types: [], release_groups: [], arr_tags: [] });
  const [ruleSyncing, setRuleSyncing] = useState(false);
  const [ruleDragIdx, setRuleDragIdx] = useState<number | null>(null);

  // Backup state
  const [backupList, setBackupList] = useState<{ name: string; size: number; created_at: string }[]>([]);
  const [backupCreating, setBackupCreating] = useState(false);
  const [ruleDropIdx, setRuleDropIdx] = useState<number | null>(null);

  const loadRules = () => {
    const headers: Record<string, string> = {};
    const k = sessionStorage.getItem("shrinkerr_api_key") || sessionStorage.getItem("squeezarr_api_key") || "";
    if (k) headers["X-Api-Key"] = k;
    fetch("/api/rules/", { headers }).then(r => r.json()).then(data => {
      setRules(Array.isArray(data) ? data : []);
    }).catch(() => {});
  };
  const loadPlexOpts = () => getPlexOptions().then(setPlexOpts).catch(() => {});

  // Display labels come from settingsIntegrations:conditions.*; the `value`
  // fields stay as the API's operator ids.
  const ct = (key: string) => t(`settingsIntegrations:conditions.${key}`);
  const op = (value: string, labelKey: string = value) => ({ value, label: ct(`operators.${labelKey}`) });
  const CONDITION_TYPES: Record<string, { label: string; group: string; operators: { value: string; label: string }[]; valueType: "select" | "text" | "number" | "duration" }> = {
    directory: { label: ct("types.directory"), group: ct("groups.path"), operators: [op("is")], valueType: "select" },
    source: { label: ct("types.source"), group: ct("groups.file"), operators: [op("is"), op("is_not")], valueType: "select" },
    resolution: { label: ct("types.resolution"), group: ct("groups.file"), operators: [op("is"), op("is_not")], valueType: "select" },
    video_codec: { label: ct("types.video_codec"), group: ct("groups.file"), operators: [op("is"), op("is_not")], valueType: "select" },
    audio_codec: { label: ct("types.audio_codec"), group: ct("groups.file"), operators: [op("contains"), op("does_not_contain")], valueType: "select" },
    file_size: { label: ct("types.file_size"), group: ct("groups.file"), operators: [op("greater_than"), op("less_than")], valueType: "number" },
    date_added: { label: ct("types.date_added"), group: ct("groups.file"), operators: [op("less_than", "newer_than"), op("greater_than", "older_than")], valueType: "duration" },
    media_type: { label: ct("types.media_type"), group: ct("groups.file"), operators: [op("is"), op("is_not")], valueType: "select" },
    title: { label: ct("types.title"), group: ct("groups.file"), operators: [op("contains"), op("does_not_contain")], valueType: "text" },
    release_group: { label: ct("types.release_group"), group: ct("groups.file"), operators: [op("is"), op("is_not")], valueType: "select" },
    label: { label: ct("types.label"), group: "Plex", operators: [op("is"), op("is_not")], valueType: "select" },
    collection: { label: ct("types.collection"), group: "Plex", operators: [op("is"), op("is_not")], valueType: "select" },
    genre: { label: ct("types.genre"), group: "Plex", operators: [op("is"), op("is_not")], valueType: "select" },
    library: { label: ct("types.library"), group: "Plex", operators: [op("is")], valueType: "select" },
    arr_tag: { label: ct("types.arr_tag"), group: "Arr", operators: [op("is"), op("is_not")], valueType: "select" },
    jellyfin_tag: { label: ct("types.jellyfin_tag"), group: "Jellyfin", operators: [op("is"), op("is_not")], valueType: "select" },
    emby_tag: { label: ct("types.emby_tag"), group: "Emby", operators: [op("is"), op("is_not")], valueType: "select" },
    emby_watched: { label: ct("types.emby_watched"), group: "Emby", operators: [op("is")], valueType: "select" },
    nzbget_category: { label: ct("types.nzbget_category"), group: ct("groups.downloads"), operators: [op("is"), op("is_not")], valueType: "select" },
  };

  const updateConditionType = (idx: number, newType: string) => {
    const conds = [...ruleForm.conditions];
    const defaultOp = CONDITION_TYPES[newType]?.operators[0]?.value || "is";
    conds[idx] = { type: newType, operator: defaultOp, value: "" };
    setRuleForm({ ...ruleForm, conditions: conds });
  };

  const updateConditionOperator = (idx: number, newOp: string) => {
    const conds = [...ruleForm.conditions];
    conds[idx] = { ...conds[idx], operator: newOp };
    setRuleForm({ ...ruleForm, conditions: conds });
  };

  const updateConditionValue = (idx: number, newVal: string) => {
    const conds = [...ruleForm.conditions];
    conds[idx] = { ...conds[idx], value: newVal };
    setRuleForm({ ...ruleForm, conditions: conds });
  };

  const loadBackups = () => { listBackups().then(setBackupList).catch(() => {}); };

  useEffect(() => {
    loadDirs();
    loadRules();
    loadBackups();
    getConditionOptions().then(setCondOpts).catch(() => {});
    // Don't load Plex options on page load — fetched on demand when adding/editing rules
    getEncodingSettings().then((enc: any) => {
      setEncoding(enc);
      if (enc?.tmdb_api_key) setTmdbKey(enc.tmdb_api_key);
      if (enc?.plex_url) setPlexUrl(enc.plex_url);
      if (enc?.plex_token) setPlexToken(enc.plex_token);
      if (enc?.plex_path_mapping) setPlexPathMapping(enc.plex_path_mapping);
    });
    // Hardware encoder availability — filters the dropdown so we don't
    // show qsv/vaapi options the host can't run. v0.3.68+.
    getEncoderCaps().then(setEncoderCaps).catch(() => {
      // On failure, leave encoderCaps=null and the dropdown falls back
      // to showing all options — better than hiding existing NVENC for
      // users whose backend transiently 500'd.
    });
    // Load Plex connection status for the Connect UI.
    plexAuthStatus().then(setPlexConn).catch(() => {});
  }, []);

  // Plex Connect flow — opens plex.tv popup, polls backend until user approves.
  const handlePlexConnect = async () => {
    setPlexPickerError("");
    try {
      const { pin_id, auth_url } = await plexAuthStart();
      // Open the Plex sign-in popup. Some browsers block popups if we don't
      // open from a direct click — this handler IS a direct click, so it's fine.
      const popup = window.open(auth_url, "plex_auth", "width=550,height=750");
      if (!popup) {
        setPlexPickerError(t("settingsIntegrations:plex.errors.popupBlocked"));
        return;
      }
      setPlexAuthState("waiting");

      // Poll every 2s for up to 5 minutes.
      const deadline = Date.now() + 5 * 60 * 1000;
      const poll = async () => {
        if (Date.now() > deadline) {
          setPlexAuthState("idle");
          setPlexPickerError(t("settingsIntegrations:plex.errors.timedOut"));
          try { popup.close(); } catch {}
          return;
        }
        try {
          const res = await plexAuthCheck(pin_id);
          if (res.expired) {
            setPlexAuthState("idle");
            setPlexPickerError(t("settingsIntegrations:plex.errors.pinExpired"));
            try { popup.close(); } catch {}
            return;
          }
          if (res.token) {
            try { popup.close(); } catch {}
            setPlexPendingToken(res.token);
            // Fetch servers and present the picker.
            setPlexAuthState("picking-server");
            try {
              const { servers } = await plexAuthResources(res.token);
              setPlexServers(servers);
              // Pre-pick the first server's recommended URI, if any.
              const recommended = servers[0]?.recommended_uri || servers[0]?.connections?.[0]?.uri || "";
              setPlexPickedUri(recommended);
            } catch (e: any) {
              setPlexPickerError(t("settingsIntegrations:plex.errors.listServersFailed", { error: e?.message || e }));
            }
            return;
          }
          setTimeout(poll, 2000);
        } catch (e) {
          // Transient error; keep polling.
          setTimeout(poll, 2000);
        }
      };
      poll();
    } catch (e: any) {
      setPlexAuthState("idle");
      setPlexPickerError(e?.message || String(e));
    }
  };

  const handlePlexSaveConnection = async () => {
    if (!plexPickedUri || !plexPendingToken) return;
    setPlexAuthState("saving");
    // Find the picked server to capture its metadata.
    const picked = plexServers.find(s =>
      s.connections.some(c => c.uri === plexPickedUri)
    );
    try {
      await plexAuthSave(
        plexPendingToken,
        plexPickedUri,
        picked?.name || "",
        picked?.client_identifier || "",
      );
      // Refresh everything so the page reflects the new connection.
      const [status, enc] = await Promise.all([
        plexAuthStatus(),
        getEncodingSettings(),
      ]);
      setPlexConn(status);
      setEncoding(enc);
      setPlexUrl(enc?.plex_url || "");
      setPlexToken(enc?.plex_token || "");
      setPlexAuthState("idle");
      setPlexPendingToken("");
      setPlexServers([]);
      setPlexPickedUri("");
    } catch (e: any) {
      setPlexAuthState("picking-server");
      setPlexPickerError(e?.message || String(e));
    }
  };

  const handlePlexDisconnect = async () => {
    await plexAuthDisconnect();
    const [status, enc] = await Promise.all([
      plexAuthStatus(),
      getEncodingSettings(),
    ]);
    setPlexConn(status);
    setEncoding(enc);
    setPlexUrl(enc?.plex_url || "");
    setPlexToken("");
  };

  const loadDirs = () => getMediaDirs().then((r: any) => setDirs(Array.isArray(r) ? r : r.dirs || []));

  const handleAddDir = async () => {
    if (!newPath) return;
    try {
      await addMediaDir(newPath, newLabel);
      setNewPath("");
      setNewLabel("");
      loadDirs();
    } catch (e: any) {
      // Pre-v0.3.50 the awaited rejection was un-caught and the user
      // saw "nothing happens" on click. Surface backend validation
      // errors (path doesn't exist, isn't a directory, system path,
      // already-configured) explicitly via a toast.
      toast(e?.message || t("settingsMedia:directories.addFailed"), "error");
    }
  };

  const handleRemoveDir = async (id: number) => {
    await removeMediaDir(id);
    loadDirs();
  };

  const handleSaveEncoding = async () => {
    if (!encoding) return;
    await updateEncodingSettings(encoding);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const keepLangs: string[] = encoding?.always_keep_languages || [];

  const addLanguage = (code: string) => {
    if (!keepLangs.includes(code)) {
      setEncoding({ ...encoding, always_keep_languages: [...keepLangs, code] });
    }
    setLangSearch("");
  };

  const removeLanguage = (code: string) => {
    setEncoding({ ...encoding, always_keep_languages: keepLangs.filter((l: string) => l !== code) });
  };

  const langName = (code: string) => t(`settingsMedia:languages.${code}`);

  const filteredLangs = langSearch.length > 0
    ? ALL_LANGUAGES.filter(code =>
        (langName(code).toLowerCase().includes(langSearch.toLowerCase()) ||
         code.toLowerCase().includes(langSearch.toLowerCase())) &&
        !keepLangs.includes(code)
      ).slice(0, 8).map(code => ({ code, name: langName(code) }))
    : [];

  const subKeepLangs: string[] = encoding?.sub_keep_languages || [];
  const subFilteredLangs = subLangSearch.length > 0
    ? ALL_LANGUAGES.filter(code =>
        (langName(code).toLowerCase().includes(subLangSearch.toLowerCase()) ||
         code.toLowerCase().includes(subLangSearch.toLowerCase())) &&
        !subKeepLangs.includes(code)
      ).slice(0, 8).map(code => ({ code, name: langName(code) }))
    : [];


  return (
    <div className="settings-page" ref={pageRef}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <h2 style={{ color: "white", fontSize: 20 }}>{t("settingsMedia:header.title")}</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <a href="/api/settings/export" download style={{ textDecoration: "none" }}>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}>{t("settingsMedia:header.export")}</button>
          </a>
          <label className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px", cursor: "pointer" }}>
            {t("settingsMedia:header.import")}
            <input type="file" accept=".json" style={{ display: "none" }}
              onChange={async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                try {
                  const text = await file.text();
                  const data = JSON.parse(text);
                  const res = await importSettings(data);
                  toast(t("settingsMedia:header.importSuccess", { settings: res.settings_count, dirs: res.dirs_count, rules: res.rules_count }), "success");
                  window.location.reload();
                } catch (err: any) {
                  toast(t("settingsMedia:header.importFailed", { error: err.message }));
                }
              }}
            />
          </label>
        </div>
      </div>

      {saved && (
        <div style={{
          position: "fixed", top: 20, right: 20, background: "var(--success)", color: "white",
          padding: "10px 20px", borderRadius: 6, fontSize: 13, fontWeight: "bold", zIndex: 1000,
        }}>
          {t("settingsMedia:header.saved")}
        </div>
      )}


      <h2 id="directories" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 0, marginBottom: 12, scrollMarginTop: 20 }}>
        {t("settingsMedia:directories.title")}
      </h2>
      {/* Media Directories */}
      <div style={sectionStyle}>
        <div style={{
          background: "var(--bg-primary)", borderRadius: 4, padding: 8,
          fontSize: 13, marginBottom: 8,
        }}>
          {dirs.map((d: any) => {
            // auto_scan defaults to true server-side; treat undefined as true
            // for legacy rows / older API responses.
            const autoScan = d.auto_scan !== false && d.auto_scan !== 0;
            return (
              <div key={d.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 0" }}>
                <span>
                  {d.path}
                  {d.label && <span style={{ fontSize: 10, fontFamily: "inherit", color: "var(--text-muted)", marginLeft: 8, padding: "2px 6px", borderRadius: 3, backgroundColor: "var(--border)" }}>{d.label}</span>}
                  {!autoScan && (
                    <span
                      title={t("settingsMedia:directories.noScanTitle")}
                      style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: 8, padding: "2px 6px", borderRadius: 3, backgroundColor: "var(--bg-card)", cursor: "help" }}
                    >{t("settingsMedia:directories.noScan")}</span>
                  )}
                </span>
                <span style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--text-muted)", cursor: "pointer" }}>
                    <input
                      type="checkbox"
                      checked={autoScan}
                      onChange={async (e) => {
                        try {
                          await updateMediaDir(d.id, { auto_scan: e.target.checked });
                          loadDirs();
                        } catch (err: any) {
                          toast(err?.message || t("settingsMedia:directories.updateFailed"), "error");
                        }
                      }}
                    />
                    {t("settingsMedia:directories.scan")}
                  </label>
                  <button onClick={() => handleRemoveDir(d.id)}
                    style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>&times;</button>
                </span>
              </div>
            );
          })}
          {dirs.length === 0 && <div style={{ opacity: 0.5 }}>{t("settingsMedia:directories.empty")}</div>}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn btn-secondary" onClick={() => setBrowserOpen(true)}
            style={{ padding: "8px 12px", fontSize: 12, whiteSpace: "nowrap", height: 36 }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: -2, marginRight: 4 }}>
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            </svg>
            {t("settingsMedia:directories.browse")}
          </button>
          <input placeholder={t("settingsMedia:directories.pathPlaceholder")} value={newPath} onChange={(e) => setNewPath(e.target.value)}
            style={{ ...inputStyle, flex: "1 1 200px", minWidth: 150 }} />
          <select value={newLabel} onChange={(e) => setNewLabel(e.target.value)}
            style={{ ...inputStyle, width: 140 }}>
            <option value="">{t("settingsMedia:directories.typeOptional")}</option>
            <option value="Movies">{t("settingsMedia:directories.types.movies")}</option>
            <option value="TV Shows">{t("settingsMedia:directories.types.tvShows")}</option>
            <option value="Other">{t("settingsMedia:directories.types.other")}</option>
          </select>
          <button className="btn btn-secondary" onClick={handleAddDir} style={{ height: 36, whiteSpace: "nowrap" }}>{t("settingsMedia:directories.add")}</button>
        </div>
        <FolderBrowser
          isOpen={browserOpen}
          initialPath="/media"
          onSelect={(path) => { setNewPath(path); setBrowserOpen(false); }}
          onCancel={() => setBrowserOpen(false)}
        />
      </div>

      {encoding && (
        <>
          <h2 id="video" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsMedia:video.title")}
          </h2>
          {/* Encoding Defaults */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 16 }}>{t("settingsMedia:video.encodingDefaults")}</h3>
            <div style={{ display: "flex", gap: 24, alignItems: "flex-start", flexWrap: "wrap" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 20, flex: "1 1 300px", minWidth: 0, maxWidth: 500 }}>

              {/* Default Encoder */}
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <span style={labelStyle}>{t("settingsMedia:video.defaultEncoder")}</span>
                  <button
                    type="button"
                    onClick={async () => {
                      setEncoderCapsRefreshing(true);
                      try { setEncoderCaps(await getEncoderCaps(true)); }
                      catch { /* keep prior caps */ }
                      finally { setEncoderCapsRefreshing(false); }
                    }}
                    title={t("settingsMedia:video.redetectTitle")}
                    style={{
                      background: "none", border: "1px solid var(--border)",
                      color: "var(--text-muted)", cursor: "pointer",
                      borderRadius: 4, padding: "2px 8px", fontSize: 11,
                    }}
                  >
                    {encoderCapsRefreshing ? t("settingsMedia:video.detecting") : t("settingsMedia:video.redetect")}
                  </button>
                </div>
                <select value={encoding.default_encoder}
                  onChange={(e) => setEncoding({ ...encoding, default_encoder: e.target.value })}
                  style={{ ...inputStyle, width: "100%" }}>
                  {/* Always include nvenc + libx265 as options regardless of
                      caps (libx265 always works; nvenc may have been chosen
                      already on a host whose detection transiently failed,
                      and we don't want to silently drop the saved value —
                      otherwise the <select> falls back to displaying the
                      first present option while state still holds "nvenc",
                      desyncing the dropdown from the help text below).
                      QSV / VAAPI only appear when detection confirms them,
                      OR when they're the currently saved value (same
                      desync-prevention reasoning). v0.3.68+ / v0.3.94+. */}
                  <option value="nvenc">NVENC (NVIDIA GPU)</option>
                  {(encoderCaps?.qsv || encoding.default_encoder === "qsv") && (
                    <option value="qsv">Intel QSV (Intel GPU / iGPU)</option>
                  )}
                  {(encoderCaps?.vaapi || encoding.default_encoder === "vaapi") && (
                    <option value="vaapi">VAAPI (Intel / AMD GPU)</option>
                  )}
                  <option value="libx265">{t("settingsMedia:video.libx265Option")}</option>
                </select>
                <div style={helpStyle}>
                  {(() => {
                    switch (encoding.default_encoder) {
                      case "nvenc":
                        return t("settingsMedia:video.encoderHelp.nvenc");
                      case "qsv":
                        return t("settingsMedia:video.encoderHelp.qsv");
                      case "vaapi":
                        return t("settingsMedia:video.encoderHelp.vaapi");
                      case "libx265":
                      default:
                        return t("settingsMedia:video.encoderHelp.libx265");
                    }
                  })()}
                </div>
                {/* /dev/dri passthrough hint — only relevant when a hardware
                    VA-API path is selected or available. */}
                {(encoderCaps?.qsv || encoderCaps?.vaapi || ["qsv", "vaapi"].includes(encoding.default_encoder)) && (
                  <details style={{ marginTop: 8, fontSize: 12, color: "var(--text-muted)" }}>
                    <summary style={{ cursor: "pointer", color: "var(--text-secondary)" }}>
                      {t("settingsMedia:video.passthrough.summary")}
                    </summary>
                    <div style={{ paddingLeft: 8, lineHeight: 1.7, marginTop: 6 }}>
                      <Trans i18nKey="settingsMedia:video.passthrough.addTo" components={{ code: <code /> }} />
                      <pre style={{ margin: "6px 0", padding: 8, background: "var(--bg-primary)", borderRadius: 4, fontSize: 11, overflow: "auto" }}>{`services:
  shrinkerr:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video
      - "<GID>"   # numeric group that owns /dev/dri/renderD128 on host`}</pre>
                      <Trans i18nKey="settingsMedia:video.passthrough.findGid" components={{ code: <code /> }} />{" "}
                      <Trans i18nKey="settingsMedia:video.passthrough.restart" components={{ code: <code />, em: <em /> }} />{" "}
                      <Trans i18nKey="settingsMedia:video.passthrough.verify" components={{ code: <code /> }} />
                    </div>
                  </details>
                )}
              </div>

              {/* Target Codec */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:video.targetCodec")}</div>
                <select value={encoding.target_codec || "hevc"}
                  onChange={(e) => setEncoding({ ...encoding, target_codec: e.target.value })}
                  style={{ ...inputStyle, width: "100%" }}>
                  {TARGET_CODECS.map(c => (
                    <option key={c.value} value={c.value}>{t(`settingsMedia:options.targetCodecs.${c.value}.label`)}</option>
                  ))}
                </select>
                <div style={helpStyle}>
                  {(() => { const c = TARGET_CODECS.find(c => c.value === (encoding.target_codec || "hevc")); return c && t(`settingsMedia:options.targetCodecs.${c.value}.desc`); })()}
                </div>
              </div>

              {/* Parallel Jobs */}
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                  <span style={labelStyle}>{t("settingsMedia:video.parallelJobs")}</span>
                  <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding?.parallel_jobs ?? 8}</span>
                </div>
                <input type="range" min={1} max={16} value={encoding?.parallel_jobs ?? 8}
                  onChange={(e) => setEncoding({ ...encoding, parallel_jobs: parseInt(e.target.value) })}
                  style={{ width: "100%", accentColor: "var(--accent)" }} />
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                  <span>1</span><span>4</span><span>8</span><span>12</span><span>16</span>
                </div>
                <div style={helpStyle}>
                  {t("settingsMedia:video.parallelJobsHelp")}
                </div>
              </div>

              {/* FFmpeg threads per job */}
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                  <span style={labelStyle}>{t("settingsMedia:video.ffmpegThreads")}</span>
                  <span style={{ color: "var(--accent)", fontWeight: "bold" }}>
                    {(encoding?.ffmpeg_threads ?? 0) === 0 ? t("settingsMedia:video.auto") : encoding?.ffmpeg_threads}
                  </span>
                </div>
                <input type="range" min={0} max={16} value={encoding?.ffmpeg_threads ?? 0}
                  onChange={(e) => setEncoding({ ...encoding, ffmpeg_threads: parseInt(e.target.value) })}
                  style={{ width: "100%", accentColor: "var(--accent)" }} />
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                  <span>{t("settingsMedia:video.auto")}</span><span>2</span><span>4</span><span>8</span><span>12</span><span>16</span>
                </div>
                <div style={helpStyle}>
                  <Trans i18nKey="settingsMedia:video.ffmpegThreadsHelp" components={{ b: <strong /> }} />
                </div>
              </div>

              {/* Target Resolution */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:video.targetResolution")}</div>
                <select value={encoding.target_resolution || "copy"}
                  onChange={(e) => setEncoding({ ...encoding, target_resolution: e.target.value })}
                  style={{ ...inputStyle, width: "100%" }}>
                  {RESOLUTION_OPTIONS.map(r => (
                    <option key={r.value} value={r.value}>{t(`settingsMedia:options.resolutions.${r.value}.label`)}</option>
                  ))}
                </select>
                <div style={helpStyle}>
                  {(() => { const r = RESOLUTION_OPTIONS.find(r => r.value === (encoding.target_resolution || "copy")); return r && t(`settingsMedia:options.resolutions.${r.value}.desc`); })()}
                </div>
              </div>

              {/* Source Codecs to Convert */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:video.convertFrom")}</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {SOURCE_CODECS.map(c => (
                    <label key={c.value} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
                      <input type="checkbox" checked={
                        c.always || (encoding.source_codecs || ["h264"]).includes(c.value)
                      }
                        disabled={c.always}
                        onChange={(e) => {
                          const current = encoding.source_codecs || ["h264"];
                          setEncoding({
                            ...encoding,
                            source_codecs: e.target.checked
                              ? [...current, c.value]
                              : current.filter((v: string) => v !== c.value),
                          });
                        }}
                        style={{ accentColor: "var(--accent)" }}
                      />
                      <span style={{ color: c.always ? "var(--success)" : "var(--text-secondary)" }}>{t(`settingsMedia:options.sourceCodecs.${c.value}`)}</span>
                      {c.always && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{t("settingsMedia:video.always")}</span>}
                    </label>
                  ))}
                </div>
                <div style={helpStyle}>{t("settingsMedia:video.convertFromHelp")}</div>
              </div>

              {encoding.default_encoder === "nvenc" && (
                <>
                  {/* v0.5.7: NVDEC hardware decode pairing */}
                  <div style={{ marginBottom: 16, padding: "10px 12px", backgroundColor: "var(--bg-secondary)",
                                border: "1px solid var(--border)", borderRadius: 4 }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 10,
                                    cursor: encoderCaps?.nvdec_available ? "pointer" : "not-allowed",
                                    opacity: encoderCaps?.nvdec_available ? 1 : 0.5 }}>
                      <input type="checkbox"
                        checked={encoding?.nvenc_hw_decode ?? true}
                        disabled={!encoderCaps?.nvdec_available}
                        onChange={e => setEncoding({ ...encoding, nvenc_hw_decode: e.target.checked })}
                        style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
                      <span style={{ fontSize: 14, fontWeight: 500 }}>{t("settingsMedia:video.nvdec.label")}</span>
                    </label>
                    <div style={{ ...helpStyle, marginTop: 6 }}>
                      {t("settingsMedia:video.nvdec.help")}
                      {!encoderCaps?.nvdec_available && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.nvdec.notDetected")}
                        </span>
                      )}
                      {(encoding?.vmaf_analysis_enabled === true || encoding?.vmaf_analysis_enabled === "true" || encoding?.vmaf_analysis_enabled == null) && (encoding?.nvenc_hw_decode ?? true) && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.vmafDecoderWarning")}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* v0.5.9: NVENC bit-depth choice */}
                  <div style={{ marginBottom: 16 }}>
                    <div style={{ ...labelStyle, marginBottom: 6 }}>{t("settingsMedia:video.bitDepth.label")}</div>
                    <select value={encoding?.nvenc_bit_depth || "10bit"}
                      onChange={e => setEncoding({ ...encoding, nvenc_bit_depth: e.target.value })}
                      style={{ ...inputStyle, width: "100%", maxWidth: 360 }}>
                      <option value="10bit">{t("settingsMedia:video.bitDepth.10bit")}</option>
                      <option value="8bit">{t("settingsMedia:video.bitDepth.8bit")}</option>
                      <option value="auto">{t("settingsMedia:video.bitDepth.auto")}</option>
                    </select>
                    <div style={{ ...helpStyle, marginTop: 6 }}>
                      <Trans i18nKey="settingsMedia:video.bitDepth.help" components={{ b: <strong /> }} />
                    </div>
                  </div>

                  {/* NVENC Preset */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.nvencPreset")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.nvenc_preset || "p6"}</span>
                    </div>
                    <input type="range" min={1} max={7} value={parseInt((encoding.nvenc_preset || "p6").replace("p", ""))}
                      onChange={(e) => setEncoding({ ...encoding, nvenc_preset: `p${e.target.value}` })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>{t("settingsMedia:video.scale.presetFastest")}</span><span>p4</span><span>{t("settingsMedia:video.scale.presetBest")}</span>
                    </div>
                    <div style={{ ...helpStyle, padding: 8, background: "var(--bg-primary)", borderRadius: 4, marginTop: 8 }}>
                      <strong style={{ color: "var(--accent)" }}>{PRESET_IDS.includes(encoding.nvenc_preset || "p6") && t(`settingsMedia:options.presets.${encoding.nvenc_preset || "p6"}.label`)}</strong>
                      {" — "}{PRESET_IDS.includes(encoding.nvenc_preset || "p6") && t(`settingsMedia:options.presets.${encoding.nvenc_preset || "p6"}.desc`)}
                    </div>
                  </div>

                  {/* NVENC CQ */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.nvencCq")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.nvenc_cq}</span>
                    </div>
                    <input type="range" min={15} max={40} value={encoding.nvenc_cq}
                      onChange={(e) => setEncoding({ ...encoding, nvenc_cq: parseInt(e.target.value) })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>{t("settingsMedia:video.scale.highestQuality", { value: 15 })}</span><span>20</span><span>24</span><span>{t("settingsMedia:video.scale.smallestFile", { value: 30 })}</span>
                    </div>
                    <div style={helpStyle}>
                      <Trans i18nKey="settingsMedia:video.nvencCqHelp" components={{ b: <strong /> }} />
                    </div>
                  </div>

                  {/* CPU fallback for NVENC jobs picked up by CPU-only workers */}
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "white", marginBottom: 4 }}>
                      {t("settingsMedia:video.cpuFallback.title")}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 10 }}>
                      {t("settingsMedia:video.cpuFallback.help")}
                    </div>
                    <div style={{ display: "flex", gap: 10, alignItems: "flex-end" }}>
                      <div style={{ flex: 1 }}>
                        <div style={labelStyle}>{t("settingsMedia:video.preset")}</div>
                        <select
                          value={encoding.nvenc_cpu_fallback_preset || ""}
                          onChange={(e) => setEncoding({ ...encoding, nvenc_cpu_fallback_preset: e.target.value })}
                          style={{ ...inputStyle, width: "100%" }}
                        >
                          <option value="">{t("settingsMedia:video.cpuFallback.autoTranslate")}</option>
                          {["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"].map(p => (
                            <option key={p} value={p}>{t(`settingsMedia:video.presetNames.${p}`)}</option>
                          ))}
                        </select>
                      </div>
                      <div style={{ width: 100 }}>
                        <div style={labelStyle}>CRF</div>
                        <input
                          type="number"
                          min={15}
                          max={40}
                          placeholder={t("settingsMedia:video.autoPlaceholder")}
                          value={encoding.nvenc_cpu_fallback_crf ?? ""}
                          onChange={(e) => {
                            const v = e.target.value;
                            setEncoding({ ...encoding, nvenc_cpu_fallback_crf: v === "" ? "" : parseInt(v) });
                          }}
                          style={{ ...inputStyle, width: "100%" }}
                        />
                      </div>
                    </div>
                  </div>
                </>
              )}
              {encoding.default_encoder === "libx265" && (
                <>
                  {/* v0.5.7: libx265 + NVDEC mixed mode (cross-bus opt-in) */}
                  <div style={{ marginBottom: 16, padding: "10px 12px", backgroundColor: "var(--bg-secondary)",
                                border: "1px solid var(--border)", borderRadius: 4 }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 10,
                                    cursor: encoderCaps?.nvdec_available ? "pointer" : "not-allowed",
                                    opacity: encoderCaps?.nvdec_available ? 1 : 0.5 }}>
                      <input type="checkbox"
                        checked={encoding?.libx265_use_nvdec ?? false}
                        disabled={!encoderCaps?.nvdec_available}
                        onChange={e => setEncoding({ ...encoding, libx265_use_nvdec: e.target.checked })}
                        style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
                      <span style={{ fontSize: 14, fontWeight: 500 }}>{t("settingsMedia:video.x265Nvdec.label")}</span>
                    </label>
                    <div style={{ ...helpStyle, marginTop: 6 }}>
                      {t("settingsMedia:video.x265Nvdec.help")}
                      {!encoderCaps?.nvdec_available && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.nvdec.notDetected")}
                        </span>
                      )}
                      {(encoding?.vmaf_analysis_enabled === true || encoding?.vmaf_analysis_enabled === "true" || encoding?.vmaf_analysis_enabled == null) && encoding?.libx265_use_nvdec && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.vmafDecoderWarning")}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* libx265 Preset */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.cpuPreset")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.libx265_preset || "medium"}</span>
                    </div>
                    <select value={encoding.libx265_preset || "medium"}
                      onChange={(e) => setEncoding({ ...encoding, libx265_preset: e.target.value })}
                      style={{ ...inputStyle, width: "100%" }}>
                      <option value="ultrafast">{t("settingsMedia:video.presetNames.ultrafast")}</option>
                      <option value="superfast">{t("settingsMedia:video.presetNames.superfast")}</option>
                      <option value="veryfast">{t("settingsMedia:video.presetNames.veryfast")}</option>
                      <option value="faster">{t("settingsMedia:video.presetNames.faster")}</option>
                      <option value="fast">{t("settingsMedia:video.presetNames.fast")}</option>
                      <option value="medium">{t("settingsMedia:video.presetNames.mediumDefault")}</option>
                      <option value="slow">{t("settingsMedia:video.presetNames.slow")}</option>
                      <option value="slower">{t("settingsMedia:video.presetNames.slower")}</option>
                      <option value="veryslow">{t("settingsMedia:video.presetNames.veryslowBest")}</option>
                    </select>
                    <div style={helpStyle}>
                      <Trans i18nKey="settingsMedia:video.cpuPresetHelp" components={{ b: <strong /> }} />
                    </div>
                  </div>

                  {/* libx265 CRF */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.crf")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.libx265_crf}</span>
                    </div>
                    <input type="range" min={15} max={28} value={encoding.libx265_crf}
                      onChange={(e) => setEncoding({ ...encoding, libx265_crf: parseInt(e.target.value) })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>{t("settingsMedia:video.scale.highestQuality", { value: 15 })}</span><span>20</span><span>24</span><span>{t("settingsMedia:video.scale.smallestFile", { value: 28 })}</span>
                    </div>
                    <div style={helpStyle}>
                      {t("settingsMedia:video.crfHelp")}
                    </div>
                  </div>

                  {/* GPU fallback for libx265 jobs picked up by NVENC workers */}
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "white", marginBottom: 4 }}>
                      {t("settingsMedia:video.gpuFallback.title")}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 10 }}>
                      {t("settingsMedia:video.gpuFallback.help")}
                    </div>
                    <div style={{ display: "flex", gap: 10, alignItems: "flex-end" }}>
                      <div style={{ flex: 1 }}>
                        <div style={labelStyle}>{t("settingsMedia:video.preset")}</div>
                        <select
                          value={encoding.libx265_gpu_fallback_preset || ""}
                          onChange={(e) => setEncoding({ ...encoding, libx265_gpu_fallback_preset: e.target.value })}
                          style={{ ...inputStyle, width: "100%" }}
                        >
                          <option value="">{t("settingsMedia:video.gpuFallback.auto")}</option>
                          {PRESET_IDS.map(p => (
                            <option key={p} value={p}>{p.toUpperCase()} — {t(`settingsMedia:options.presets.${p}.label`)}</option>
                          ))}
                        </select>
                      </div>
                      <div style={{ width: 100 }}>
                        <div style={labelStyle}>CQ</div>
                        <input
                          type="number"
                          min={15}
                          max={40}
                          placeholder={t("settingsMedia:video.autoPlaceholder")}
                          value={encoding.libx265_gpu_fallback_cq ?? ""}
                          onChange={(e) => {
                            const v = e.target.value;
                            setEncoding({ ...encoding, libx265_gpu_fallback_cq: v === "" ? "" : parseInt(v) });
                          }}
                          style={{ ...inputStyle, width: "100%" }}
                        />
                      </div>
                    </div>
                  </div>
                </>
              )}
              {/* Intel Quick Sync — preset matches NVENC's veryslow…veryfast
                  ladder; quality is a 1-51 ICQ target where 22 is a sane
                  default (closest analog to NVENC's CQ). v0.3.68+. */}
              {encoding.default_encoder === "qsv" && (
                <>
                  {/* v0.5.7: QSV hardware decode pairing */}
                  <div style={{ marginBottom: 16, padding: "10px 12px", backgroundColor: "var(--bg-secondary)",
                                border: "1px solid var(--border)", borderRadius: 4 }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 10,
                                    cursor: encoderCaps?.qsv_decode_available ? "pointer" : "not-allowed",
                                    opacity: encoderCaps?.qsv_decode_available ? 1 : 0.5 }}>
                      <input type="checkbox"
                        checked={encoding?.qsv_hw_decode ?? true}
                        disabled={!encoderCaps?.qsv_decode_available}
                        onChange={e => setEncoding({ ...encoding, qsv_hw_decode: e.target.checked })}
                        style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
                      <span style={{ fontSize: 14, fontWeight: 500 }}>{t("settingsMedia:video.qsvDecode.label")}</span>
                    </label>
                    <div style={{ ...helpStyle, marginTop: 6 }}>
                      {t("settingsMedia:video.qsvDecode.help")}
                      {!encoderCaps?.qsv_decode_available && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.qsvDecode.notDetected")}
                        </span>
                      )}
                      {(encoding?.vmaf_analysis_enabled === true || encoding?.vmaf_analysis_enabled === "true" || encoding?.vmaf_analysis_enabled == null) && (encoding?.qsv_hw_decode ?? true) && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.vmafDecoderWarning")}
                        </span>
                      )}
                    </div>
                  </div>

                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.qsvPreset")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.qsv_preset || "medium"}</span>
                    </div>
                    <select value={encoding.qsv_preset || "medium"}
                      onChange={(e) => setEncoding({ ...encoding, qsv_preset: e.target.value })}
                      style={{ ...inputStyle, width: "100%" }}>
                      <option value="veryfast">{t("settingsMedia:video.presetNames.veryfast")}</option>
                      <option value="faster">{t("settingsMedia:video.presetNames.faster")}</option>
                      <option value="fast">{t("settingsMedia:video.presetNames.fast")}</option>
                      <option value="medium">{t("settingsMedia:video.presetNames.mediumRecommended")}</option>
                      <option value="slow">{t("settingsMedia:video.presetNames.slow")}</option>
                      <option value="slower">{t("settingsMedia:video.presetNames.slower")}</option>
                      <option value="veryslow">{t("settingsMedia:video.presetNames.veryslowBest")}</option>
                    </select>
                    <div style={helpStyle}>
                      <Trans i18nKey="settingsMedia:video.qsvPresetHelp" components={{ b: <strong /> }} />
                    </div>
                  </div>

                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.qsvQuality")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.qsv_cq ?? 22}</span>
                    </div>
                    <input type="range" min={15} max={32} value={encoding.qsv_cq ?? 22}
                      onChange={(e) => setEncoding({ ...encoding, qsv_cq: parseInt(e.target.value) })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>{t("settingsMedia:video.scale.highestQuality", { value: 15 })}</span><span>22</span><span>26</span><span>{t("settingsMedia:video.scale.smallestFile", { value: 32 })}</span>
                    </div>
                    <div style={helpStyle}>
                      <Trans i18nKey="settingsMedia:video.qsvQualityHelp" components={{ b: <strong /> }} />
                    </div>
                  </div>

                  {/* QSV look-ahead — opt-in lookahead rate control. Slight
                      quality bump at the cost of throughput (~10-20% slower).
                      Off by default; on a tester's iGPU, QSV ran ~145 fps
                      without lookahead — enabling it would drop to ~120 fps
                      for a small visual quality bump on rapid scenes. v0.3.93+. */}
                  <div>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={!!encoding.qsv_lookahead}
                        onChange={(e) => setEncoding({ ...encoding, qsv_lookahead: e.target.checked })}
                        style={{ accentColor: "var(--accent)" }}
                      />
                      <span style={labelStyle}>{t("settingsMedia:video.lookahead.label")}</span>
                    </label>
                    <div style={helpStyle}>
                      {t("settingsMedia:video.lookahead.help")}
                    </div>
                  </div>
                </>
              )}
              {/* Intel/AMD VA-API — CQP rate control via -qp; compression_level
                  is a 0-7 driver-side knob (lower = more analysis). v0.3.68+. */}
              {encoding.default_encoder === "vaapi" && (
                <>
                  {/* v0.5.7: VAAPI hardware decode pairing */}
                  <div style={{ marginBottom: 16, padding: "10px 12px", backgroundColor: "var(--bg-secondary)",
                                border: "1px solid var(--border)", borderRadius: 4 }}>
                    <label style={{ display: "flex", alignItems: "center", gap: 10,
                                    cursor: encoderCaps?.vaapi_decode_available ? "pointer" : "not-allowed",
                                    opacity: encoderCaps?.vaapi_decode_available ? 1 : 0.5 }}>
                      <input type="checkbox"
                        checked={encoding?.vaapi_hw_decode ?? true}
                        disabled={!encoderCaps?.vaapi_decode_available}
                        onChange={e => setEncoding({ ...encoding, vaapi_hw_decode: e.target.checked })}
                        style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
                      <span style={{ fontSize: 14, fontWeight: 500 }}>{t("settingsMedia:video.vaapiDecode.label")}</span>
                    </label>
                    <div style={{ ...helpStyle, marginTop: 6 }}>
                      {t("settingsMedia:video.vaapiDecode.help")}
                      {!encoderCaps?.vaapi_decode_available && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.vaapiDecode.notDetected")}
                        </span>
                      )}
                      {(encoding?.vmaf_analysis_enabled === true || encoding?.vmaf_analysis_enabled === "true" || encoding?.vmaf_analysis_enabled == null) && (encoding?.vaapi_hw_decode ?? true) && (
                        <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
                          {t("settingsMedia:video.vmafDecoderWarning")}
                        </span>
                      )}
                    </div>
                  </div>

                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.vaapiCompression")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.vaapi_compression_level ?? 4}</span>
                    </div>
                    <input type="range" min={0} max={7} value={encoding.vaapi_compression_level ?? 4}
                      onChange={(e) => setEncoding({ ...encoding, vaapi_compression_level: parseInt(e.target.value) })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>{t("settingsMedia:video.scale.bestQualitySlowest", { value: 0 })}</span><span>4</span><span>{t("settingsMedia:video.scale.fastest", { value: 7 })}</span>
                    </div>
                    <div style={helpStyle}>
                      <Trans i18nKey="settingsMedia:video.vaapiCompressionHelp" components={{ b: <strong /> }} />
                    </div>
                  </div>

                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>{t("settingsMedia:video.vaapiQp")}</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.vaapi_qp ?? 22}</span>
                    </div>
                    <input type="range" min={15} max={32} value={encoding.vaapi_qp ?? 22}
                      onChange={(e) => setEncoding({ ...encoding, vaapi_qp: parseInt(e.target.value) })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>{t("settingsMedia:video.scale.highestQuality", { value: 15 })}</span><span>22</span><span>26</span><span>{t("settingsMedia:video.scale.smallestFile", { value: 32 })}</span>
                    </div>
                    <div style={helpStyle}>
                      <Trans i18nKey="settingsMedia:video.vaapiQpHelp" components={{ b: <strong /> }} />
                    </div>
                  </div>
                </>
              )}

              {/* Smart Encoding */}
              <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
                <div style={{ fontSize: 14, fontWeight: 600, color: "white", marginBottom: 12 }}>{t("settingsMedia:video.smart.title")}</div>

                {/* Content Type Detection Toggle */}
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, cursor: "pointer" }}>
                  <input type="checkbox"
                    checked={encoding.content_type_detection === true || encoding.content_type_detection === "true"}
                    onChange={(e) => setEncoding({ ...encoding, content_type_detection: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={labelStyle}>{t("settingsMedia:video.smart.contentType")}</span>
                </label>
                <div style={helpStyle}>
                  {t("settingsMedia:video.smart.contentTypeHelp")}
                </div>

                {/* VMAF Analysis Toggle */}
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 14, marginBottom: 8, cursor: "pointer" }}>
                  <input type="checkbox"
                    checked={encoding.vmaf_analysis_enabled === true || encoding.vmaf_analysis_enabled === "true" || (encoding.vmaf_analysis_enabled == null)}
                    onChange={(e) => setEncoding({ ...encoding, vmaf_analysis_enabled: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={labelStyle}>{t("settingsMedia:video.smart.vmaf")}</span>
                </label>
                {/* v0.5.7: HW decode / VMAF incompatibility surface.
                    Renders only when VMAF is enabled AND at least one HW decode
                    toggle is currently on. Dynamically counts active decoders so
                    users see the immediate impact. Mirror messaging in each
                    encoder card's HW decode toggle help text. */}
                {(() => {
                  const vmafOn = encoding.vmaf_analysis_enabled === true ||
                                 encoding.vmaf_analysis_enabled === "true" ||
                                 encoding.vmaf_analysis_enabled == null;
                  if (!vmafOn) return null;
                  const activeDecoders: string[] = [];
                  if (encoding?.nvenc_hw_decode ?? true) activeDecoders.push("NVENC+NVDEC");
                  if (encoding?.qsv_hw_decode ?? true) activeDecoders.push("QSV");
                  if (encoding?.vaapi_hw_decode ?? true) activeDecoders.push("VAAPI");
                  if (encoding?.libx265_use_nvdec) activeDecoders.push("libx265+NVDEC");
                  if (activeDecoders.length === 0) return null;
                  return (
                    <div style={{
                      marginTop: 8, marginBottom: 12, padding: "10px 12px",
                      backgroundColor: "rgba(255, 200, 80, 0.10)",
                      border: "1px solid rgba(255, 200, 80, 0.45)",
                      borderRadius: 4, fontSize: 13, color: "var(--text-primary)", lineHeight: 1.5,
                    }}>
                      <Trans i18nKey="settingsMedia:video.smart.hwWarning" count={activeDecoders.length}
                        values={{ decoders: activeDecoders.join(", ") }}
                        components={{ b: <strong style={{ color: "var(--warning)" }} />, b2: <strong /> }} />
                    </div>
                  );
                })()}
                <div style={helpStyle}>
                  <Trans i18nKey="settingsMedia:video.smart.vmafHelp" components={{ b: <strong /> }} />
                </div>
                <table style={{ fontSize: 12, borderCollapse: "collapse", marginTop: 8, width: "100%" }}>
                  <thead>
                    <tr style={{ borderBottom: "1px solid var(--border)" }}>
                      <th style={{ textAlign: "left", padding: "4px 0 4px 28px", color: "var(--text-secondary)", fontWeight: 600, width: 120 }}>{t("settingsMedia:video.smart.score")}</th>
                      <th style={{ textAlign: "left", padding: "4px 0", color: "var(--text-secondary)", fontWeight: 600 }}>{t("settingsMedia:video.smart.quality")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ["93+", t("settingsMedia:video.smart.tiers.transparent"), "#18ffa5"],
                      ["87–93", t("settingsMedia:video.smart.tiers.highQuality"), "var(--accent)"],
                      ["80–87", t("settingsMedia:video.smart.tiers.acceptable"), "#ffa94d"],
                      ["< 80", t("settingsMedia:video.smart.tiers.degradation"), "#e94560"],
                    ].map(([score, desc, color]) => (
                      <tr key={score as string} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td style={{ padding: "4px 0 4px 28px", color: color as string, fontWeight: 600 }}>{score}</td>
                        <td style={{ padding: "4px 0", color: "var(--text-muted)" }}>{desc}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                {/* ─────────────────────────────────────────────────────
                    VMAF minimum-score threshold. Only relevant when the
                    VMAF analysis toggle above is on.
                    ───────────────────────────────────────────────────── */}
                {(encoding.vmaf_analysis_enabled === true || encoding.vmaf_analysis_enabled === "true" || encoding.vmaf_analysis_enabled == null) && (() => {
                  const rawMin = encoding.vmaf_min_score;
                  const minScore = typeof rawMin === "number" ? rawMin
                    : (typeof rawMin === "string" && rawMin !== "" ? parseFloat(rawMin) : 0) || 0;
                  const enabled = minScore > 0;
                  // Tier colour from the canonical helper (utils/vmaf) — same
                  // 3-tier mapping as the dashboard, activity log, and inline
                  // job VMAF chips. Gives quick visual feedback on how strict
                  // the threshold is as the slider moves.
                  const tierBgColor = vmafColor(minScore);
                  return (
                    <div style={{ marginTop: 16, padding: 12, background: "var(--bg-primary)", borderRadius: 4 }}>
                      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, cursor: "pointer" }}>
                        <input
                          type="checkbox"
                          checked={enabled}
                          onChange={(e) => setEncoding({ ...encoding, vmaf_min_score: e.target.checked ? 85 : 0 })}
                          style={{ accentColor: "var(--accent)" }}
                        />
                        <span style={labelStyle}>{t("settingsMedia:video.smart.minScore")}</span>
                      </label>
                      <div style={{ ...helpStyle, marginLeft: 26 }}>
                        <Trans i18nKey="settingsMedia:video.smart.minScoreHelp" components={{ b: <strong /> }} />
                      </div>
                      {enabled && (
                        <div style={{ marginTop: 10, marginLeft: 26, display: "flex", alignItems: "center", gap: 12 }}>
                          <input
                            type="range"
                            min={60}
                            max={100}
                            step={1}
                            value={minScore}
                            onChange={(e) => setEncoding({ ...encoding, vmaf_min_score: parseFloat(e.target.value) })}
                            style={{ flex: 1 }}
                          />
                          <div style={{
                            minWidth: 64, textAlign: "center",
                            padding: "4px 10px", borderRadius: 4,
                            background: "var(--bg-card)",
                            color: tierBgColor, fontWeight: 700, fontSize: 14,
                          }}>
                            {minScore.toFixed(0)}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })()}

                {/* Re-measure suspect VMAF scores (v0.3.32+).
                    Iterates completed jobs whose recorded score is either
                    flagged uncertain or dropped below the Excellent tier,
                    and re-runs VMAF — using the same bimodal-aware retry
                    path as fresh encodes. Skips jobs whose original
                    pre-rename source no longer exists on disk (typical when
                    "delete original after conversion" is on). */}
                <VmafRemeasureRow />

                {/* Resolution-Aware CQ Toggle + Table */}
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 14, marginBottom: 8, cursor: "pointer" }}>
                  <input type="checkbox"
                    checked={encoding.resolution_aware_cq === true || encoding.resolution_aware_cq === "true"}
                    onChange={(e) => setEncoding({ ...encoding, resolution_aware_cq: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={labelStyle}>{t("settingsMedia:video.smart.resAware")}</span>
                </label>
                <div style={helpStyle}>
                  {t("settingsMedia:video.smart.resAwareHelp")}
                </div>

                {(encoding.resolution_aware_cq === true || encoding.resolution_aware_cq === "true") && (
                  <div style={{ display: "grid", gridTemplateColumns: "70px 1fr 40px", gap: "6px 12px", alignItems: "center", marginTop: 10, padding: 12, background: "var(--bg-primary)", borderRadius: 4 }}>
                    {([
                      ["4K", "resolution_cq_4k", 24],
                      ["1080p", "resolution_cq_1080p", 20],
                      ["720p", "resolution_cq_720p", 18],
                      ["SD", "resolution_cq_sd", 16],
                    ] as const).map(([label, key, def]) => (
                      <>
                        <span key={`l-${key}`} style={{ fontSize: 12, color: "var(--text-muted)" }}>{label}</span>
                        <input key={`r-${key}`} type="range" min={15} max={40}
                          value={encoding[key] ?? def}
                          onChange={(e) => setEncoding({ ...encoding, [key]: parseInt(e.target.value) })}
                          style={{ width: "100%", accentColor: "var(--accent)" }} />
                        <span key={`v-${key}`} style={{ fontSize: 12, color: "white", fontWeight: 600, textAlign: "center" }}>
                          {encoding[key] ?? def}
                        </span>
                      </>
                    ))}
                  </div>
                )}
              </div>

              {/* Timeouts */}
              <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
                <h4 style={{ color: "white", fontSize: 13, marginBottom: 12 }}>{t("settingsMedia:video.timeouts.title")}</h4>
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={labelStyle}>{t("settingsMedia:video.timeouts.ffmpeg")}</span>
                    <input type="number" min={1} max={72} step={1}
                      value={Math.round((encoding.ffmpeg_timeout || 21600) / 3600)}
                      onChange={(e) => setEncoding({ ...encoding, ffmpeg_timeout: parseInt(e.target.value) * 3600 })}
                      style={{ ...inputStyle, width: 70, textAlign: "center" as const }} />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={labelStyle}>{t("settingsMedia:video.timeouts.ffprobe")}</span>
                    <input type="number" min={5} max={300} step={5}
                      value={encoding.ffprobe_timeout || 30}
                      onChange={(e) => setEncoding({ ...encoding, ffprobe_timeout: parseInt(e.target.value) })}
                      style={{ ...inputStyle, width: 70, textAlign: "center" as const }} />
                  </div>
                </div>
              </div>

              <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start" }}>
                {t("settingsMedia:video.save")}
              </button>
            </div>

            {/* Conversion Guide */}
            <div style={{
              flex: "1 1 300px", minWidth: 0, background: "var(--bg-primary)", borderRadius: 6,
              padding: 16, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6,
            }}>
              <h4 style={{ color: "white", marginBottom: 12, fontSize: 14 }}>{t("settingsMedia:video.guide.title")}</h4>

              {[
                // ── NVENC (GPU) ────────────────────────────────────────
                {
                  title: t("settingsMedia:video.guide.nvencPresets.title"),
                  desc: t("settingsMedia:video.guide.nvencPresets.desc"),
                  cols: [t("settingsMedia:video.guide.cols.preset"), t("settingsMedia:video.guide.cols.speed"), t("settingsMedia:video.guide.cols.qualitySize")],
                  rows: [
                    ["p1-p2", "~400 fps", t("settingsMedia:video.guide.cells.largestFiles")],
                    ["p3-p4", "~250 fps", t("settingsMedia:video.guide.cells.balanced")],
                    ["p5", "~180 fps", t("settingsMedia:video.guide.cells.goodCompression")],
                    ["p6", "~120 fps", t("settingsMedia:video.guide.cells.greatCompression")],
                    ["p7", "~80 fps", t("settingsMedia:video.guide.cells.bestCompression")],
                  ],
                  note: t("settingsMedia:video.guide.nvencPresets.note"),
                },
                {
                  title: t("settingsMedia:video.guide.nvencCombos.title"),
                  cols: [t("settingsMedia:video.guide.cols.priority"), t("settingsMedia:video.guide.cols.settings"), t("settingsMedia:video.guide.cols.savings")],
                  rows: [
                    [t("settingsMedia:video.guide.cells.maxQuality"), "p7 / CQ 20", "20-30%"],
                    [t("settingsMedia:video.guide.cells.qualityFirst"), "p6 / CQ 21", "25-35%"],
                    [t("settingsMedia:video.guide.cells.balanced"), "p5 / CQ 23", "35-45%"],
                    [t("settingsMedia:video.guide.cells.spaceSaver"), "p4 / CQ 25", "45-55%"],
                    [t("settingsMedia:video.guide.cells.maxCompression"), "p3 / CQ 27", "55-65%"],
                  ],
                },
                // ── libx265 (CPU) ──────────────────────────────────────
                {
                  title: t("settingsMedia:video.guide.x265Presets.title"),
                  desc: t("settingsMedia:video.guide.x265Presets.desc"),
                  cols: [t("settingsMedia:video.guide.cols.preset"), t("settingsMedia:video.guide.cols.speed1080p"), t("settingsMedia:video.guide.cols.qualitySize")],
                  rows: [
                    ["ultrafast", "~120 fps", t("settingsMedia:video.guide.cells.largestFiles")],
                    ["superfast", "~80 fps", t("settingsMedia:video.guide.cells.slightlySmaller")],
                    ["veryfast", "~50 fps", t("settingsMedia:video.guide.cells.goodCompression")],
                    ["fast", "~20 fps", t("settingsMedia:video.guide.cells.betterCompression")],
                    ["medium", "~10 fps", t("settingsMedia:video.guide.cells.greatDefault")],
                    ["slow", "~5 fps", t("settingsMedia:video.guide.cells.veryGood")],
                    ["slower", "~2 fps", t("settingsMedia:video.guide.cells.bestPractical")],
                    ["veryslow", "~1 fps", t("settingsMedia:video.guide.cells.diminishingReturns")],
                  ],
                  note: t("settingsMedia:video.guide.x265Presets.note"),
                },
                {
                  title: t("settingsMedia:video.guide.x265Combos.title"),
                  cols: [t("settingsMedia:video.guide.cols.priority"), t("settingsMedia:video.guide.cols.settings"), t("settingsMedia:video.guide.cols.savings")],
                  rows: [
                    [t("settingsMedia:video.guide.cells.maxQuality"), "slow / CRF 18", "25-35%"],
                    [t("settingsMedia:video.guide.cells.qualityFirst"), "medium / CRF 20", "35-45%"],
                    [t("settingsMedia:video.guide.cells.balanced"), "fast / CRF 23", "45-55%"],
                    [t("settingsMedia:video.guide.cells.spaceSaver"), "veryfast / CRF 25", "55-65%"],
                    [t("settingsMedia:video.guide.cells.maxThroughput"), "superfast / CRF 26", "60-70%"],
                  ],
                },
                // ── Shared quality target ──────────────────────────────
                {
                  title: t("settingsMedia:video.guide.cqCrf.title"),
                  desc: t("settingsMedia:video.guide.cqCrf.desc"),
                  cols: ["CQ/CRF", t("settingsMedia:video.guide.cols.quality"), t("settingsMedia:video.guide.cols.savings")],
                  rows: [
                    ["15-18", t("settingsMedia:video.guide.cells.overkill"), "5-15%"],
                    ["19-20", t("settingsMedia:video.guide.cells.transparent"), "20-30%"],
                    ["21-23", t("settingsMedia:video.guide.cells.excellent"), "30-45%"],
                    ["24-26", t("settingsMedia:video.guide.cells.good"), "45-60%"],
                    ["27-30", t("settingsMedia:video.guide.cells.noticeableLoss"), "60%+"],
                  ],
                },
              ].map((section) => (
                <div key={section.title} style={{ marginBottom: 16 }}>
                  <div style={{ color: "var(--accent)", fontWeight: "bold", marginBottom: 4 }}>{section.title}</div>
                  {section.desc && <p>{section.desc}</p>}
                  <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse", marginTop: 8, tableLayout: "fixed" }}>
                    <colgroup>
                      <col style={{ width: "30%" }} />
                      <col style={{ width: "40%" }} />
                      <col style={{ width: "30%" }} />
                    </colgroup>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--border)" }}>
                        <th style={{ textAlign: "left", padding: "6px 8px", color: "var(--text-secondary)" }}>{section.cols[0]}</th>
                        <th style={{ textAlign: "center", padding: "6px 8px", color: "var(--text-secondary)" }}>{section.cols[1]}</th>
                        <th style={{ textAlign: "right", padding: "6px 8px", color: "var(--text-secondary)" }}>{section.cols[2]}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {section.rows.map(([c1, c2, c3]) => (
                        <tr key={c1} style={{ borderBottom: "1px solid var(--bg-card)" }}>
                          <td style={{ padding: "6px 8px", color: "var(--accent)" }}>{c1}</td>
                          <td style={{ textAlign: "center", padding: "6px 8px" }}>{c2}</td>
                          <td style={{ textAlign: "right", padding: "6px 8px", color: "var(--success)" }}>{c3}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {section.note && (
                    <p style={{ marginTop: 6, fontSize: 11, fontStyle: "italic" }}>{section.note}</p>
                  )}
                </div>
              ))}

              <div style={{ marginBottom: 16 }}>
                <div style={{ color: "var(--accent)", fontWeight: "bold", marginBottom: 4 }}>{t("settingsMedia:video.guide.tipsTitle")}</div>
                <ul style={{ paddingLeft: 16, margin: 0 }}>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.sources" components={{ b: <strong />, em: <em /> }} /></li>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.grain" components={{ b: <strong />, em: <em /> }} /></li>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.animation" components={{ b: <strong />, em: <em /> }} /></li>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.nvencVsX265" components={{ b: <strong />, em: <em /> }} /></li>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.x265Scaling" components={{ b: <strong />, em: <em /> }} /></li>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.nvencScaling" components={{ b: <strong />, em: <em /> }} /></li>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.testEncode" components={{ b: <strong />, em: <em /> }} /></li>
                  <li style={{ marginBottom: 4 }}><Trans i18nKey="settingsMedia:video.guide.tips.mixedFleets" components={{ b: <strong />, em: <em /> }} /></li>
                </ul>
              </div>

              <div style={{
                background: "var(--bg-card)", padding: 10, borderRadius: 4,
                border: "1px solid var(--border)", fontSize: 11,
              }}>
                {(() => {
                  // The "Current" summary reads from whichever encoder's
                  // settings are active so users who switch default_encoder
                  // see the right values + description.
                  const isCpu = encoding.default_encoder === "libx265";
                  if (isCpu) {
                    const preset: string = encoding.libx265_preset || "medium";
                    const crf: number = encoding.libx265_crf ?? 20;
                    const presetRank: Record<string, number> = {
                      ultrafast: 1, superfast: 2, veryfast: 3, faster: 4,
                      fast: 5, medium: 6, slow: 7, slower: 8, veryslow: 9,
                    };
                    const r = presetRank[preset] ?? 6;
                    let desc: string;
                    if (crf <= 20 && r >= 7) desc = "maxQuality";
                    else if (crf <= 20) desc = "highQuality";
                    else if (crf <= 23 && r >= 5) desc = "greatQuality";
                    else if (crf <= 23) desc = "goodSolid";
                    else if (crf <= 26) desc = "goodAggressive";
                    else desc = "maxCompression";
                    return (
                      <>
                        <strong style={{ color: "var(--success)" }}>
                          {t("settingsMedia:video.guide.current.x265", { preset, crf })}
                        </strong>
                        <span> — {t(`settingsMedia:video.guide.current.${desc}`)}</span>
                      </>
                    );
                  }
                  const preset: string = encoding.nvenc_preset || "p6";
                  const cq: number = encoding.nvenc_cq || 20;
                  const p = parseInt(preset.replace("p", ""));
                  let desc: string;
                  if (cq <= 20 && p >= 6) desc = "maxQuality";
                  else if (cq <= 20) desc = "highQuality";
                  else if (cq <= 23 && p >= 5) desc = "greatQuality";
                  else if (cq <= 23) desc = "goodSolid";
                  else if (cq <= 26) desc = "goodAggressive";
                  else desc = "maxCompression";
                  return (
                    <>
                      <strong style={{ color: "var(--success)" }}>
                        {t("settingsMedia:video.guide.current.nvenc", { preset, cq })}
                      </strong>
                      <span> — {t(`settingsMedia:video.guide.current.${desc}`)}</span>
                    </>
                  );
                })()}
              </div>
            </div>
            </div>
          </div>

          <h2 id="audio" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsMedia:audio.title")}
          </h2>
          {/* Audio Track Rules */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>{t("settingsMedia:audio.trackRules")}</h3>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 16 }}>
              <input type="checkbox" checked={encoding?.audio_cleanup_enabled ?? true}
                readOnly
                onClick={() => setEncoding({ ...encoding, audio_cleanup_enabled: !(encoding?.audio_cleanup_enabled ?? true) })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsMedia:audio.removeUnwanted")}</span>
            </label>
            {(encoding?.audio_cleanup_enabled ?? true) && (
            <div style={{ display: "flex", gap: 24, alignItems: "flex-start", flexWrap: "wrap" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 20, flex: "1 1 300px", minWidth: 0, maxWidth: 500 }}>

              {/* Always Keep Languages */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:audio.alwaysKeep")}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                  {keepLangs.map((code: string) => {
                    const known = ALL_LANGUAGES.includes(code);
                    return (
                      <span key={code} style={{
                        background: "var(--border)", color: "var(--success)", padding: "4px 10px",
                        borderRadius: 16, fontSize: 12, display: "flex", alignItems: "center", gap: 6,
                      }}>
                        {known ? `${langName(code)} (${code})` : code}
                        <button onClick={() => removeLanguage(code)} style={{
                          background: "none", border: "none", color: "var(--text-muted)",
                          cursor: "pointer", fontSize: 14, padding: 0, lineHeight: 1,
                        }}>&times;</button>
                      </span>
                    );
                  })}
                </div>
                <div style={{ position: "relative" }}>
                  <input
                    placeholder={t("settingsMedia:audio.searchPlaceholder")}
                    value={langSearch}
                    onChange={(e) => setLangSearch(e.target.value)}
                    style={{ ...inputStyle, width: "100%" }}
                  />
                  {filteredLangs.length > 0 && (
                    <div style={{
                      position: "absolute", top: "100%", left: 0, right: 0, zIndex: 10,
                      background: "var(--bg-secondary)", border: "1px solid var(--border)",
                      borderRadius: 4, maxHeight: 200, overflowY: "auto",
                    }}>
                      {filteredLangs.map(l => (
                        <div key={l.code} onClick={() => addLanguage(l.code)} style={{
                          padding: "8px 12px", cursor: "pointer", fontSize: 13,
                          color: "var(--text-secondary)", borderBottom: "1px solid var(--bg-card)",
                        }}
                          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-card)")}
                          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                        >
                          <strong>{l.name}</strong> <span style={{ color: "var(--text-muted)" }}>({l.code})</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                <div style={helpStyle}>
                  {t("settingsMedia:audio.alwaysKeepHelp")}
                </div>
              </div>

              {/* v0.5.17: dedup tracks in always-keep languages */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.always_keep_dedup !== false}
                  onChange={() => setEncoding({ ...encoding, always_keep_dedup: encoding.always_keep_dedup === false })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:audio.dedup.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                {t("settingsMedia:audio.dedup.help")}
              </div>

              {/* Keep native language */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.keep_native_language !== false}
                  onChange={() => setEncoding({ ...encoding, keep_native_language: encoding.keep_native_language === false })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:audio.keepNative.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                {t("settingsMedia:audio.keepNative.help")}
              </div>

              {/* Reorder native language first */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.reorder_native_audio !== false}
                  onChange={() => setEncoding({ ...encoding, reorder_native_audio: encoding.reorder_native_audio === false })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:audio.reorderNative.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                {t("settingsMedia:audio.reorderNative.help")}
              </div>

              {/* Ignore Unknown Tracks */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.ignore_unknown_tracks}
                  readOnly
                  onClick={() => setEncoding({ ...encoding, ignore_unknown_tracks: !encoding.ignore_unknown_tracks })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:audio.keepUnknown.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                {t("settingsMedia:audio.keepUnknown.help")}
              </div>

              {/* v0.8.0: auto-detect languages for und tracks */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.auto_detect_languages !== false}
                  onChange={() => setEncoding({ ...encoding, auto_detect_languages: encoding.auto_detect_languages === false })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:audio.autoDetect.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                {t("settingsMedia:audio.autoDetect.help")}
              </div>

              {/* v0.9.18: language-detection tuning (model + confidence gates) */}
              <div style={{ marginTop: 14 }}>
                <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:audio.model.label")}</div>
                <select value={encoding.lang_detect_whisper_model || "tiny"}
                  onChange={(e) => setEncoding({ ...encoding, lang_detect_whisper_model: e.target.value })}
                  style={{ ...inputStyle, width: "100%" }}>
                  {["tiny", "base", "small", "medium", "large-v3"].map(m => (
                    <option key={m} value={m}>{t(`settingsMedia:audio.model.${m}`)}</option>
                  ))}
                </select>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                  {t("settingsMedia:audio.model.help")}
                </div>
              </div>
              <div style={{ marginTop: 12, display: "flex", gap: 16 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:audio.audioConfidence")}</div>
                  <input type="number" min={0.1} max={0.95} step={0.05}
                    value={encoding.lang_detect_audio_min ?? 0.6}
                    onChange={(e) => setEncoding({ ...encoding, lang_detect_audio_min: parseFloat(e.target.value) })}
                    style={{ ...inputStyle, width: "100%" }} />
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:audio.subtitleConfidence")}</div>
                  <input type="number" min={0.1} max={0.95} step={0.05}
                    value={encoding.lang_detect_sub_min ?? 0.7}
                    onChange={(e) => setEncoding({ ...encoding, lang_detect_sub_min: parseFloat(e.target.value) })}
                    style={{ ...inputStyle, width: "100%" }} />
                </div>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                <Trans i18nKey="settingsMedia:audio.confidenceHelp" components={{ code: <code /> }} />
              </div>

              {/* Audio Conversion */}
              <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
                <h4 style={{ color: "white", fontSize: 13, marginBottom: 12 }}>{t("settingsMedia:audio.conversion.title")}</h4>

                <div style={{ marginBottom: 16 }}>
                  <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:audio.conversion.codec")}</div>
                  <select value={encoding.audio_codec || "copy"}
                    onChange={(e) => setEncoding({ ...encoding, audio_codec: e.target.value })}
                    style={{ ...inputStyle, width: "100%" }}>
                    {AUDIO_CODECS.map(c => (
                      <option key={c.value} value={c.value}>{t(`settingsMedia:options.audioCodecs.${c.value}.label`)}</option>
                    ))}
                  </select>
                  <div style={helpStyle}>
                    {(() => { const c = AUDIO_CODECS.find(c => c.value === (encoding.audio_codec || "copy")); return c && t(`settingsMedia:options.audioCodecs.${c.value}.desc`); })()}
                  </div>
                </div>

                {(encoding.audio_codec && encoding.audio_codec !== "copy") && (
                  <>
                    <div style={{ marginBottom: 16 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                        <span style={labelStyle}>{t("settingsMedia:audio.conversion.bitrate")}</span>
                        <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.audio_bitrate || 128} kbps</span>
                      </div>
                      <input type="range" min={64} max={640} step={32}
                        value={encoding.audio_bitrate || 128}
                        onChange={(e) => setEncoding({ ...encoding, audio_bitrate: parseInt(e.target.value) })}
                        style={{ width: "100%", accentColor: "var(--accent)" }} />
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                        <span>64 kbps</span><span>128</span><span>256</span><span>384</span><span>640 kbps</span>
                      </div>
                      <div style={helpStyle}>
                        <Trans i18nKey="settingsMedia:audio.conversion.bitrateHelp" components={{ b: <strong /> }} />
                      </div>
                    </div>

                    <div>
                      <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                        <input type="checkbox" checked={encoding.audio_downmix || false}
                          readOnly
                          onClick={() => setEncoding({ ...encoding, audio_downmix: !encoding.audio_downmix })}
                          style={{ flexShrink: 0 }} />
                        <span style={labelStyle}>{t("settingsMedia:audio.conversion.downmix")}</span>
                      </label>
                      <div style={{ ...helpStyle, paddingLeft: 26 }}>
                        {t("settingsMedia:audio.conversion.downmixHelp")}
                      </div>
                    </div>
                  </>
                )}

                {/* Lossless Audio Auto-Convert */}
                <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 16 }}>
                  <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                    <input type="checkbox" checked={encoding.auto_convert_lossless || false}
                      readOnly
                      onClick={() => setEncoding({ ...encoding, auto_convert_lossless: !encoding.auto_convert_lossless })}
                      style={{ flexShrink: 0 }} />
                    <span style={labelStyle}>{t("settingsMedia:audio.lossless.label")}</span>
                  </label>
                  <div style={{ ...helpStyle, paddingLeft: 26 }}>
                    {t("settingsMedia:audio.lossless.help")}
                  </div>

                  {encoding.auto_convert_lossless && (
                    <div style={{ paddingLeft: 26, marginTop: 12, display: "flex", flexDirection: "column", gap: 12 }}>
                      <div>
                        <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:audio.lossless.targetCodec")}</div>
                        <select value={encoding.lossless_target_codec || "eac3"}
                          onChange={(e) => setEncoding({ ...encoding, lossless_target_codec: e.target.value })}
                          style={{ ...inputStyle, width: "100%" }}>
                          {AUDIO_CODECS.filter(c => c.value !== "copy" && c.value !== "flac").map(c => (
                            <option key={c.value} value={c.value}>{t(`settingsMedia:options.audioCodecs.${c.value}.label`)}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                          <span style={labelStyle}>{t("settingsMedia:audio.lossless.targetBitrate")}</span>
                          <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.lossless_target_bitrate || 640} kbps</span>
                        </div>
                        <input type="range" min={128} max={640} step={32}
                          value={encoding.lossless_target_bitrate || 640}
                          onChange={(e) => setEncoding({ ...encoding, lossless_target_bitrate: parseInt(e.target.value) })}
                          style={{ width: "100%", accentColor: "var(--accent)" }} />
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                          <span>128 kbps</span><span>256</span><span>384</span><span>512</span><span>640 kbps</span>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start" }}>
                {t("settingsMedia:audio.save")}
              </button>
            </div>

            {/* Audio Guide */}
            <div style={{
              flex: "1 1 300px", minWidth: 0, background: "var(--bg-primary)", borderRadius: 6,
              padding: 16, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6,
            }}>
              <h4 style={{ color: "white", marginBottom: 12, fontSize: 14 }}>{t("settingsMedia:audio.guide.title")}</h4>

              {[
                {
                  title: t("settingsMedia:audio.guide.codecs.title"),
                  cols: [t("settingsMedia:audio.guide.codecs.cols.codec"), t("settingsMedia:audio.guide.codecs.cols.type"), t("settingsMedia:audio.guide.codecs.cols.typicalSize")],
                  rows: [
                    ["AAC", t("settingsMedia:audio.guide.codecs.lossy"), t("settingsMedia:audio.guide.perHour", { size: "~50 MB" })],
                    ["AC3 (DD)", t("settingsMedia:audio.guide.codecs.lossy51"), t("settingsMedia:audio.guide.perHour", { size: "~250 MB" })],
                    ["EAC3 (DD+)", t("settingsMedia:audio.guide.codecs.lossy5171"), t("settingsMedia:audio.guide.perHour", { size: "~350 MB" })],
                    ["DTS", t("settingsMedia:audio.guide.codecs.lossy51"), t("settingsMedia:audio.guide.perHour", { size: "~550 MB" })],
                    ["DTS-HD MA", t("settingsMedia:audio.guide.codecs.lossless5171"), t("settingsMedia:audio.guide.perHour", { size: "~1.5 GB" })],
                    ["TrueHD", t("settingsMedia:audio.guide.codecs.lossless71"), t("settingsMedia:audio.guide.perHour", { size: "~2 GB" })],
                    ["FLAC", t("settingsMedia:audio.guide.codecs.lossless"), t("settingsMedia:audio.guide.perHour", { size: "~1 GB" })],
                    ["PCM", t("settingsMedia:audio.guide.codecs.uncompressed"), t("settingsMedia:audio.guide.perHour", { size: "~3 GB" })],
                  ],
                },
                {
                  title: t("settingsMedia:audio.guide.removing.title"),
                  desc: t("settingsMedia:audio.guide.removing.desc"),
                  cols: [t("settingsMedia:audio.guide.removing.cols.tracksRemoved"), t("settingsMedia:audio.guide.removing.cols.typicalSavings"), t("settingsMedia:audio.guide.removing.cols.example")],
                  rows: [
                    ["1 × AC3", "~500 MB", t("settingsMedia:audio.guide.removing.commentary")],
                    ["1 × DTS", "~1.1 GB", t("settingsMedia:audio.guide.removing.foreignDub")],
                    ["3 × AC3", "~1.5 GB", t("settingsMedia:audio.guide.removing.threeForeignDubs")],
                    ["1 × DTS-HD MA", "~3 GB", t("settingsMedia:audio.guide.removing.losslessForeign")],
                    ["1 × TrueHD", "~4 GB", t("settingsMedia:audio.guide.removing.atmosForeignDub")],
                  ],
                },
                {
                  title: t("settingsMedia:audio.guide.reencode.title"),
                  cols: [t("settingsMedia:audio.guide.reencode.cols.mode"), t("settingsMedia:audio.guide.reencode.cols.speed"), t("settingsMedia:audio.guide.reencode.cols.useWhen")],
                  rows: [
                    [t("settingsMedia:audio.guide.reencode.copy"), t("settingsMedia:audio.guide.reencode.instant"), t("settingsMedia:audio.guide.reencode.always")],
                    ["AAC 128k", t("settingsMedia:audio.guide.reencode.fast"), t("settingsMedia:audio.guide.reencode.stereoSmall")],
                    ["AAC 256k", t("settingsMedia:audio.guide.reencode.fast"), t("settingsMedia:audio.guide.reencode.stereoGood")],
                    ["AC3 384k", t("settingsMedia:audio.guide.reencode.fast"), t("settingsMedia:audio.guide.reencode.compatibility")],
                    ["EAC3 640k", t("settingsMedia:audio.guide.reencode.fast"), t("settingsMedia:audio.guide.reencode.modern")],
                  ],
                },
              ].map((section) => (
                <div key={section.title} style={{ marginBottom: 16 }}>
                  <div style={{ color: "var(--accent)", fontWeight: "bold", marginBottom: 4 }}>{section.title}</div>
                  {section.desc && <p>{section.desc}</p>}
                  <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse", marginTop: 8, tableLayout: "fixed" }}>
                    <colgroup>
                      <col style={{ width: "30%" }} />
                      <col style={{ width: "40%" }} />
                      <col style={{ width: "30%" }} />
                    </colgroup>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--border)" }}>
                        <th style={{ textAlign: "left", padding: "6px 8px", color: "var(--text-secondary)" }}>{section.cols[0]}</th>
                        <th style={{ textAlign: "center", padding: "6px 8px", color: "var(--text-secondary)" }}>{section.cols[1]}</th>
                        <th style={{ textAlign: "right", padding: "6px 8px", color: "var(--text-secondary)" }}>{section.cols[2]}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {section.rows.map(([c1, c2, c3]) => (
                        <tr key={c1} style={{ borderBottom: "1px solid var(--bg-card)" }}>
                          <td style={{ padding: "6px 8px", color: "var(--accent)" }}>{c1}</td>
                          <td style={{ textAlign: "center", padding: "6px 8px" }}>{c2}</td>
                          <td style={{ textAlign: "right", padding: "6px 8px", color: "var(--success)" }}>{c3}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}

              <div style={{ marginBottom: 16 }}>
                <div style={{ color: "var(--accent)", fontWeight: "bold", marginBottom: 4 }}>{t("settingsMedia:audio.guide.tipsTitle")}</div>
                <ul style={{ paddingLeft: 16, margin: 0 }}>
                  {["copy", "bluray", "lossless", "commentary", "native"].map(k => (
                    <li key={k} style={{ marginBottom: 4 }}>{t(`settingsMedia:audio.guide.tips.${k}`)}</li>
                  ))}
                </ul>
              </div>

              <div style={{
                background: "var(--bg-card)", padding: 10, borderRadius: 4,
                border: "1px solid var(--border)", fontSize: 11,
              }}>
                <strong style={{ color: "var(--success)" }}>{t("settingsMedia:audio.guide.current.summary", { langs: keepLangs.join(", ").toUpperCase() || t("settingsMedia:audio.guide.current.none") })}</strong>
                <span> — </span>
                {keepLangs.length === 0
                  ? t("settingsMedia:audio.guide.current.onlyNative")
                  : t("settingsMedia:audio.guide.current.keptCount", { count: keepLangs.length })}
              </div>
            </div>
            </div>
            )}
          </div>

          <h2 id="subtitles" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsMedia:subtitles.title")}
          </h2>
          {/* Subtitle Cleanup */}
          <div style={sectionStyle}>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 16 }}>
              <input type="checkbox" checked={encoding?.sub_cleanup_enabled ?? true}
                readOnly
                onClick={() => setEncoding({ ...encoding, sub_cleanup_enabled: !(encoding?.sub_cleanup_enabled ?? true) })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsMedia:subtitles.removeUnwanted")}</span>
            </label>
            {(encoding?.sub_cleanup_enabled ?? true) && (
            <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 500 }}>

              {/* Subtitle Keep Languages */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>{t("settingsMedia:subtitles.keepLanguages")}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                  {(encoding.sub_keep_languages || []).map((code: string) => {
                    const known = ALL_LANGUAGES.includes(code);
                    return (
                      <span key={code} style={{
                        background: "var(--border)", color: "var(--success)", padding: "4px 10px",
                        borderRadius: 16, fontSize: 12, display: "flex", alignItems: "center", gap: 6,
                      }}>
                        {known ? `${langName(code)} (${code})` : code}
                        <button onClick={() => {
                          setEncoding({ ...encoding, sub_keep_languages: (encoding.sub_keep_languages || []).filter((c: string) => c !== code) });
                        }} style={{
                          background: "none", border: "none", color: "var(--text-muted)",
                          cursor: "pointer", fontSize: 14, padding: 0, lineHeight: 1,
                        }}>&times;</button>
                      </span>
                    );
                  })}
                </div>
                <div style={{ position: "relative" }}>
                  <input
                    placeholder={t("settingsMedia:subtitles.searchPlaceholder")}
                    value={subLangSearch}
                    onChange={(e) => setSubLangSearch(e.target.value)}
                    style={{ ...inputStyle, width: "100%" }}
                  />
                  {subFilteredLangs.length > 0 && (
                    <div style={{
                      position: "absolute", top: "100%", left: 0, right: 0, zIndex: 10,
                      background: "var(--bg-secondary)", border: "1px solid var(--border)",
                      borderRadius: 4, maxHeight: 200, overflowY: "auto",
                    }}>
                      {subFilteredLangs.map(l => (
                        <div key={l.code} onClick={() => {
                          if (!(encoding.sub_keep_languages || []).includes(l.code)) {
                            setEncoding({ ...encoding, sub_keep_languages: [...(encoding.sub_keep_languages || []), l.code] });
                          }
                          setSubLangSearch("");
                        }} style={{
                          padding: "8px 12px", cursor: "pointer", fontSize: 13,
                          color: "var(--text-secondary)", borderBottom: "1px solid var(--bg-card)",
                        }}
                          onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-card)")}
                          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                        >
                          <strong>{l.name}</strong> <span style={{ color: "var(--text-muted)" }}>({l.code})</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
                <div style={helpStyle}>
                  {t("settingsMedia:subtitles.keepHelp")}
                </div>
              </div>

              {/* Keep Unknown Subs */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.sub_keep_unknown ?? true}
                  readOnly
                  onClick={() => setEncoding({ ...encoding, sub_keep_unknown: !(encoding.sub_keep_unknown ?? true) })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:subtitles.keepUnknown.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -14, paddingLeft: 26 }}>
                {t("settingsMedia:subtitles.keepUnknown.help")}
              </div>

              {/* v0.5.20: Auto-keep native-language subs (separate from audio) */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.keep_native_subs === true || encoding.keep_native_subs === "true"}
                  onChange={() => setEncoding({ ...encoding, keep_native_subs: !(encoding.keep_native_subs === true || encoding.keep_native_subs === "true") })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:subtitles.keepNative.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -14, paddingLeft: 26 }}>
                <Trans i18nKey="settingsMedia:subtitles.keepNative.help" components={{ b: <strong /> }} />
              </div>

              {/* v0.8.2: same auto-detect-languages toggle as audio section (shared setting) */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.auto_detect_languages !== false}
                  onChange={() => setEncoding({ ...encoding, auto_detect_languages: encoding.auto_detect_languages === false })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:subtitles.autoDetect.label")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                {t("settingsMedia:subtitles.autoDetect.help")}
              </div>

              <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start" }}>
                {t("settingsMedia:subtitles.save")}
              </button>
            </div>
            )}

            {/* External Subtitles */}
            <div style={{ borderTop: "1px solid var(--border)", marginTop: 16, paddingTop: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsMedia:subtitles.external.title")}</div>
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 8 }}>
                <input type="checkbox" checked={encoding.merge_external_subs ?? false}
                  onChange={() => setEncoding({ ...encoding, merge_external_subs: !(encoding.merge_external_subs ?? false) })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>{t("settingsMedia:subtitles.external.merge")}</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -4, paddingLeft: 26, marginBottom: 12 }}>
                <Trans i18nKey="settingsMedia:subtitles.external.mergeHelp" components={{ code: <code /> }} />
              </div>

              {(encoding.merge_external_subs ?? false) && (
                <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 8 }}>
                  <input type="checkbox" checked={encoding.delete_external_subs_after_merge ?? false}
                    onChange={() => setEncoding({ ...encoding, delete_external_subs_after_merge: !(encoding.delete_external_subs_after_merge ?? false) })}
                    style={{ flexShrink: 0 }} />
                  <span style={labelStyle}>{t("settingsMedia:subtitles.external.deleteAfter")}</span>
                </label>
              )}
              {(encoding.merge_external_subs ?? false) && (encoding.delete_external_subs_after_merge ?? false) && (
                <div style={{ fontSize: 11, color: "var(--warning)", paddingLeft: 26, marginTop: -4 }}>
                  {t("settingsMedia:subtitles.external.deleteWarning")}
                </div>
              )}

              <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start", marginTop: 12 }}>
                {t("settingsMedia:subtitles.external.save")}
              </button>
            </div>
          </div>

          <h2 id="connections" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsIntegrations:connections.title")}
          </h2>
          {/* Metadata APIs */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 4 }}>{t("settingsIntegrations:tmdb.title")}</h3>
            {encoding.tmdb_key_source === "bundled" || encoding.tmdb_key_source === "user" ? (
              <>
                {/* Success banner shown for BOTH sources (bundled + user) — the
                    goal is a single "TMDB is working, don't worry about it"
                    signal regardless of whose key is in play. Subtext differs
                    per source so the admin knows which key is active. */}
                <div
                  style={{
                    display: "flex", alignItems: "flex-start", gap: 10,
                    padding: "10px 12px", marginTop: 8, marginBottom: 14,
                    background: "rgba(16, 185, 129, 0.1)",
                    border: "1px solid rgba(16, 185, 129, 0.4)",
                    borderRadius: 6, fontSize: 12, lineHeight: 1.5,
                    color: "var(--text-secondary)",
                  }}
                >
                  <svg
                    width="18" height="18" viewBox="0 0 24 24" fill="none"
                    stroke="var(--success)" strokeWidth="2.5"
                    strokeLinecap="round" strokeLinejoin="round"
                    style={{ flexShrink: 0, marginTop: 1 }}
                  >
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                  <div>
                    <div style={{ color: "var(--success)", fontWeight: 600, marginBottom: 2 }}>
                      {t("settingsIntegrations:tmdb.alreadyConnected")}
                    </div>
                    {encoding.tmdb_key_source === "bundled" ? (
                      <>
                        {t("settingsIntegrations:tmdb.bundledDesc")}
                      </>
                    ) : (
                      <>
                        {t("settingsIntegrations:tmdb.userDesc")}
                      </>
                    )}
                  </div>
                </div>
                {encoding.tmdb_key_source === "bundled" && (
                  <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
                    <Trans i18nKey="settingsIntegrations:tmdb.optionalNote" components={{ strong: <strong /> }} />
                  </div>
                )}
              </>
            ) : (
              <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
                {t("settingsIntegrations:tmdb.intro")}
              </div>
            )}

            {/* TMDB API Key */}
            <div style={{ marginBottom: 20 }}>
              <div style={{ ...labelStyle, marginBottom: 8 }}>
                {t("settingsIntegrations:tmdb.apiKey")}{" "}
                <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener noreferrer"
                  style={{ fontSize: 11, color: "var(--accent)" }}>{t("settingsIntegrations:tmdb.getFreeKey")}</a>
                {encoding.tmdb_key_source === "bundled" && (
                  <span style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 400, marginLeft: 8 }}>
                    {t("settingsIntegrations:tmdb.optional")}
                  </span>
                )}
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <div style={{ position: "relative", flex: 1, maxWidth: 400 }}>
                  <input
                    type={showTmdbKey ? "text" : "password"}
                    value={tmdbKey}
                    onChange={(e) => setTmdbKey(e.target.value)}
                    placeholder={t("settingsIntegrations:tmdb.keyPlaceholder")}
                    style={{ ...inputStyle, width: "100%", paddingRight: 36 }}
                  />
                  <button
                    onClick={() => setShowTmdbKey(!showTmdbKey)}
                    style={{
                      position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)",
                      background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 14,
                    }}
                    title={showTmdbKey ? t("settingsIntegrations:tmdb.hide") : t("settingsIntegrations:tmdb.show")}
                  >
                    {showTmdbKey ? (
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
                        <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
                        <line x1="1" y1="1" x2="23" y2="23"/>
                      </svg>
                    ) : (
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                        <circle cx="12" cy="12" r="3"/>
                      </svg>
                    )}
                  </button>
                </div>
                <button
                  className="btn btn-secondary"
                  onClick={async () => {
                    setTmdbTest({ status: "loading" });
                    try {
                      const res = await testApiKey("tmdb");
                      if (res.success) {
                        setTmdbTest({ status: "success" });
                      } else {
                        setTmdbTest({ status: "error", error: res.error || t("settingsIntegrations:tmdb.testFailed") });
                      }
                    } catch (e: any) {
                      setTmdbTest({ status: "error", error: e.message || t("settingsIntegrations:tmdb.requestFailed") });
                    }
                  }}
                  disabled={tmdbTest.status === "loading"}
                  style={{ minWidth: 60 }}
                >
                  {tmdbTest.status === "loading" ? "..." : t("settingsIntegrations:tmdb.test")}
                </button>
                {tmdbTest.status === "success" && (
                  <span style={{ color: "var(--success)", fontSize: 16 }}>&#10003;</span>
                )}
                {tmdbTest.status === "error" && (
                  <span style={{ color: "var(--danger, #e74c3c)", fontSize: 12 }}>&#10007; {tmdbTest.error}</span>
                )}
              </div>
              {encoding.tmdb_configured && (
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6 }}>
                  <span style={{
                    width: 8, height: 8, borderRadius: "50%", display: "inline-block",
                    background: encoding.tmdb_key_source === "user" ? "var(--success)" : "var(--accent)",
                  }} />
                  <span style={{
                    fontSize: 12,
                    color: encoding.tmdb_key_source === "user" ? "var(--success)" : "var(--accent)",
                  }}>
                    {encoding.tmdb_key_source === "user" ? t("settingsIntegrations:tmdb.connectedUser") : t("settingsIntegrations:tmdb.connectedBundled")}
                  </span>
                </div>
              )}
            </div>

            <button
              className="btn btn-primary"
              style={{ marginTop: 4 }}
              onClick={async () => {
                await updateEncodingSettings({
                  ...encoding,
                  tmdb_api_key: tmdbKey,
                });
                setSaved(true);
                setTimeout(() => setSaved(false), 2000);
              }}
            >
              {t("settingsIntegrations:tmdb.save")}
            </button>

          </div>

          {/* Plex */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 4 }}>Plex</h3>
            <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
              {t("settingsIntegrations:plex.intro")}
            </div>

            {/* ── Connected state: show current connection + disconnect ────── */}
            {plexConn.connected && plexAuthState !== "picking-server" && (
              <div style={{
                background: "var(--bg-primary)",
                border: "1px solid var(--border)",
                borderRadius: 6,
                padding: 12,
                marginBottom: 12,
                display: "flex",
                alignItems: "center",
                gap: 12,
              }}>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: "var(--success)", flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 500, color: "white" }}>
                    <Trans i18nKey="settingsIntegrations:plex.connectedTo" values={{ name: plexConn.server_name || "Plex" }}
                      components={{ srv: <span style={{ color: "#e5a00d" }} /> }} />
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {plexConn.user?.email ? `${plexConn.user.email} · ` : ""}{plexConn.server_url}
                  </div>
                </div>
                <button
                  className="btn btn-secondary"
                  style={{ fontSize: 12, padding: "6px 12px" }}
                  onClick={handlePlexDisconnect}
                >
                  {t("settingsIntegrations:plex.disconnect")}
                </button>
              </div>
            )}

            {/* ── Disconnected state: big Connect button ─────────────────── */}
            {!plexConn.connected && plexAuthState === "idle" && (
              <>
                <button
                  onClick={handlePlexConnect}
                  style={{
                    background: "#e5a00d",
                    color: "#1f1f1f",
                    border: "none",
                    borderRadius: 6,
                    padding: "10px 18px",
                    fontSize: 14,
                    fontWeight: 600,
                    cursor: "pointer",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M11.644 1.59a.9.9 0 0 1 .712 0l9 4.5a.9.9 0 0 1 .544.826v10.168a.9.9 0 0 1-.544.826l-9 4.5a.9.9 0 0 1-.712 0l-9-4.5a.9.9 0 0 1-.544-.826V6.916a.9.9 0 0 1 .544-.826l9-4.5Z"/>
                  </svg>
                  {t("settingsIntegrations:plex.connect")}
                </button>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
                  {t("settingsIntegrations:plex.connectHelp")}
                </div>
              </>
            )}

            {/* ── Waiting for user sign-in ──────────────────────────────── */}
            {plexAuthState === "waiting" && (
              <div style={{
                background: "var(--bg-primary)",
                border: "1px dashed var(--border)",
                borderRadius: 6,
                padding: 16,
                display: "flex",
                alignItems: "center",
                gap: 12,
              }}>
                <div className="spinner" style={{ width: 18, height: 18 }} />
                <div>
                  <div style={{ fontSize: 13, color: "white" }}>{t("settingsIntegrations:plex.waiting")}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    {t("settingsIntegrations:plex.waitingHelp")}
                  </div>
                </div>
                <div style={{ flex: 1 }} />
                <button
                  className="btn btn-secondary"
                  style={{ fontSize: 12, padding: "4px 10px" }}
                  onClick={() => { setPlexAuthState("idle"); setPlexPickerError(""); }}
                >
                  {t("common:actions.cancel")}
                </button>
              </div>
            )}

            {/* ── Server picker ─────────────────────────────────────────── */}
            {plexAuthState === "picking-server" && (
              <div style={{
                background: "var(--bg-primary)",
                border: "1px solid var(--border)",
                borderRadius: 6,
                padding: 12,
                marginBottom: 12,
              }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>
                  {t("settingsIntegrations:plex.chooseServer")}
                </div>
                {plexServers.length === 0 ? (
                  <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>
                    {t("settingsIntegrations:plex.noServers")}
                  </div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {plexServers.flatMap(server =>
                      server.connections.map(conn => {
                        const id = `${server.client_identifier}|${conn.uri}`;
                        const picked = plexPickedUri === conn.uri;
                        return (
                          <label
                            key={id}
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 10,
                              padding: "8px 10px",
                              borderRadius: 4,
                              border: `1px solid ${picked ? "#e5a00d" : "var(--border)"}`,
                              background: picked ? "rgba(229,160,13,0.08)" : "transparent",
                              cursor: "pointer",
                            }}
                          >
                            <input
                              type="radio"
                              name="plex_server_uri"
                              checked={picked}
                              onChange={() => setPlexPickedUri(conn.uri)}
                              style={{ accentColor: "#e5a00d" }}
                            />
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontSize: 13, color: "white", display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                                <span style={{ fontWeight: 500 }}>{server.name}</span>
                                {server.owned && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(229,160,13,0.2)", color: "#e5a00d" }}>{t("settingsIntegrations:plex.badges.owned")}</span>}
                                {conn.local && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(0,200,100,0.15)", color: "var(--success)" }}>{t("settingsIntegrations:plex.badges.local")}</span>}
                                {conn.relay && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "var(--border)", color: "var(--text-muted)" }}>{t("settingsIntegrations:plex.badges.relay")}</span>}
                                {conn.reachable === true && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(0,200,100,0.15)", color: "var(--success)" }}>{t("settingsIntegrations:plex.badges.reachable")}</span>}
                                {conn.reachable === false && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(231,76,60,0.2)", color: "var(--danger, #e74c3c)" }}>{t("settingsIntegrations:plex.badges.unreachable")}</span>}
                              </div>
                              <div style={{ fontSize: 11, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                {conn.uri}
                              </div>
                            </div>
                          </label>
                        );
                      })
                    )}
                  </div>
                )}
                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <button
                    className="btn btn-primary"
                    style={{ fontSize: 12, padding: "6px 14px" }}
                    disabled={!plexPickedUri}
                    onClick={handlePlexSaveConnection}
                  >
                    {t("settingsIntegrations:plex.useServer")}
                  </button>
                  <button
                    className="btn btn-secondary"
                    style={{ fontSize: 12, padding: "6px 14px" }}
                    onClick={() => { setPlexAuthState("idle"); setPlexPendingToken(""); setPlexServers([]); setPlexPickedUri(""); setPlexPickerError(""); }}
                  >
                    {t("common:actions.cancel")}
                  </button>
                </div>
              </div>
            )}

            {plexAuthState === "saving" && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>{t("settingsIntegrations:plex.saving")}</div>
            )}

            {plexPickerError && (
              <div style={{ fontSize: 12, color: "var(--danger, #e74c3c)", marginBottom: 12 }}>
                {plexPickerError}
              </div>
            )}

            {/* ── Manual setup (fallback / advanced) ───────────────────── */}
            <button
              onClick={() => setShowManualPlex(!showManualPlex)}
              style={{
                background: "none",
                border: "none",
                color: "var(--text-muted)",
                fontSize: 11,
                cursor: "pointer",
                padding: "6px 0",
                textDecoration: "underline",
              }}
            >
              {showManualPlex ? t("settingsIntegrations:plex.hideManual") : t("settingsIntegrations:plex.showManual")}
            </button>

            {showManualPlex && (
              <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: 12, marginTop: 8 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={labelStyle}>{t("settingsIntegrations:plex.serverUrl")}</span>
                </div>
                <input
                  type="text"
                  value={plexUrl}
                  onChange={(e) => setPlexUrl(e.target.value)}
                  placeholder="http://192.168.0.103:32400"
                  style={{ ...inputStyle, width: "100%", marginBottom: 12 }}
                />

                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={labelStyle}>{t("settingsIntegrations:plex.authToken")}</span>
                  <a href="https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/" target="_blank" rel="noopener noreferrer" style={{ fontSize: 11, color: "var(--accent)" }}>{t("settingsIntegrations:plex.findToken")}</a>
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <div style={{ position: "relative", flex: 1 }}>
                    <input
                      type={showPlexToken ? "text" : "password"}
                      value={plexToken}
                      onChange={(e) => setPlexToken(e.target.value)}
                      placeholder={t("settingsIntegrations:plex.tokenPlaceholder")}
                      style={{ ...inputStyle, width: "100%", paddingRight: 36 }}
                    />
                    <button onClick={() => setShowPlexToken(!showPlexToken)} style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                    </button>
                  </div>
                  <button
                    className="btn btn-secondary"
                    style={{ fontSize: 12, padding: "6px 12px", whiteSpace: "nowrap" }}
                    onClick={async () => {
                      setPlexTest({ status: "loading" });
                      try {
                        const result = await testApiKey("plex" as any);
                        if (result.success) {
                          setPlexTest({ status: "success", serverName: (result as any).server_name, libraryCount: (result as any).library_count });
                        } else {
                          setPlexTest({ status: "error", error: (result as any).error || t("settingsIntegrations:plex.failed") });
                        }
                      } catch (e: any) {
                        setPlexTest({ status: "error", error: e.message });
                      }
                    }}
                  >
                    {plexTest.status === "loading" ? t("settingsIntegrations:plex.testing") : t("settingsIntegrations:plex.test")}
                  </button>
                  {plexTest.status === "success" && (
                    <span style={{ color: "var(--success)", fontSize: 12 }}>&#10003; {t("settingsIntegrations:plex.testSuccess", { name: plexTest.serverName, count: plexTest.libraryCount })}</span>
                  )}
                  {plexTest.status === "error" && (
                    <span style={{ color: "var(--danger, #e74c3c)", fontSize: 12 }}>&#10007; {plexTest.error}</span>
                  )}
                </div>
              </div>
            )}

            <div style={{ marginTop: 16 }}>
              <span style={labelStyle}>{t("settingsIntegrations:plex.pathMapping")}</span>
              <input
                type="text"
                value={plexPathMapping}
                onChange={(e) => setPlexPathMapping(e.target.value)}
                placeholder="/media=/srv/media"
                style={{ ...inputStyle, width: "100%", marginTop: 4 }}
              />
              <div style={helpStyle}>
                <Trans i18nKey="settingsIntegrations:plex.pathMappingHelp" components={{ code: <code /> }} />
              </div>
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginTop: 12 }}>
              <input type="checkbox" checked={encoding?.plex_scan_after_conversion !== false && encoding?.plex_scan_after_conversion !== "false"}
                onChange={() => setEncoding({ ...encoding, plex_scan_after_conversion: encoding?.plex_scan_after_conversion === false || encoding?.plex_scan_after_conversion === "false" })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsIntegrations:shared.refreshAfter")}</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 28 }}>
              {t("settingsIntegrations:plex.refreshAfterHelp")}
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginTop: 12 }}>
              <input type="checkbox" checked={encoding?.plex_empty_trash_after_scan || false}
                readOnly
                onClick={() => setEncoding({ ...encoding, plex_empty_trash_after_scan: !encoding?.plex_empty_trash_after_scan })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsIntegrations:plex.emptyTrash")}</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
              {t("settingsIntegrations:plex.emptyTrashHelp")}
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginTop: 12 }}>
              <input type="checkbox" checked={encoding?.plex_prioritize_unwatched || false}
                onChange={() => setEncoding({ ...encoding, plex_prioritize_unwatched: !encoding?.plex_prioritize_unwatched })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsIntegrations:plex.prioritizeUnwatched")}</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
              {t("settingsIntegrations:plex.prioritizeUnwatchedHelp")}
            </div>

            <button
              className="btn btn-primary"
              style={{ marginTop: 16 }}
              onClick={async () => {
                await updateEncodingSettings({
                  ...encoding,
                  plex_url: plexUrl,
                  plex_token: plexToken,
                  plex_path_mapping: plexPathMapping,
                });
                setSaved(true);
                setTimeout(() => setSaved(false), 2000);
              }}
            >
              {t("settingsIntegrations:plex.save")}
            </button>
          </div>

          {/* Jellyfin */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 4 }}>Jellyfin</h3>
            <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
              {t("settingsIntegrations:jellyfin.intro")}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:jellyfin.url")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="http://192.168.0.103:8096"
                  value={encoding?.jellyfin_url || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_url: e.target.value })} />
              </div>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:shared.apiKey")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder={t("settingsIntegrations:jellyfin.apiKeyPlaceholder")}
                  type="password"
                  value={encoding?.jellyfin_api_key || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_api_key: e.target.value })} />
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:shared.userIdOptional")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder={t("settingsIntegrations:shared.autoDetected")}
                  value={encoding?.jellyfin_user_id || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_user_id: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  {t("settingsIntegrations:shared.userIdHelp")}
                </div>
              </div>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:shared.pathMapping")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="/media=/mnt/media"
                  value={encoding?.jellyfin_path_mapping || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_path_mapping: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  {t("settingsIntegrations:jellyfin.pathMappingHelp")}
                </div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12 }}>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    const r = await testApiKey("jellyfin");
                    if (r.success) toast(t("settingsIntegrations:shared.connectedLibraries", { name: (r as any).server_name || "Jellyfin", count: (r as any).library_count || 0 }), "success");
                    else toast(r.error || t("settingsIntegrations:shared.connectionFailed"));
                  } catch { toast(t("settingsIntegrations:shared.connectionFailed")); }
                }}>{t("settingsIntegrations:shared.testConnection")}</button>
              {encoding?.jellyfin_configured && (
                <span style={{ fontSize: 11, color: "var(--success)", display: "flex", alignItems: "center", gap: 4 }}>
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--success)" }} />
                  {t("settingsIntegrations:shared.connected")}
                </span>
              )}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding?.jellyfin_scan_after_conversion !== false && encoding?.jellyfin_scan_after_conversion !== "false"}
                  onChange={() => setEncoding({ ...encoding, jellyfin_scan_after_conversion: encoding?.jellyfin_scan_after_conversion === false || encoding?.jellyfin_scan_after_conversion === "false" })} />
                <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{t("settingsIntegrations:shared.refreshAfter")}</span>
              </label>
            </div>
            <button className="btn btn-primary" style={{ marginTop: 16 }}
              onClick={async () => {
                await updateEncodingSettings({
                  jellyfin_url: encoding?.jellyfin_url,
                  jellyfin_api_key: encoding?.jellyfin_api_key,
                  jellyfin_user_id: encoding?.jellyfin_user_id,
                  jellyfin_path_mapping: encoding?.jellyfin_path_mapping,
                  jellyfin_scan_after_conversion: encoding?.jellyfin_scan_after_conversion,
                } as any);
                toast(t("settingsIntegrations:jellyfin.saved"), "success");
              }}>{t("settingsIntegrations:jellyfin.save")}</button>
          </div>

          {/* Emby */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 4 }}>Emby</h3>
            <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
              {t("settingsIntegrations:emby.intro")}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:emby.url")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="http://192.168.0.103:8096"
                  value={encoding?.emby_url || ""}
                  onChange={(e) => setEncoding({ ...encoding, emby_url: e.target.value })} />
              </div>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:shared.apiKey")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder={t("settingsIntegrations:emby.apiKeyPlaceholder")}
                  type="password"
                  value={encoding?.emby_api_key || ""}
                  onChange={(e) => setEncoding({ ...encoding, emby_api_key: e.target.value })} />
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:shared.userIdOptional")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder={t("settingsIntegrations:shared.autoDetected")}
                  value={encoding?.emby_user_id || ""}
                  onChange={(e) => setEncoding({ ...encoding, emby_user_id: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  {t("settingsIntegrations:shared.userIdHelp")}
                </div>
              </div>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:shared.pathMapping")}</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="/media=/mnt/media"
                  value={encoding?.emby_path_mapping || ""}
                  onChange={(e) => setEncoding({ ...encoding, emby_path_mapping: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  {t("settingsIntegrations:emby.pathMappingHelp")}
                </div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12 }}>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    const r = await testApiKey("emby");
                    if (r.success) toast(t("settingsIntegrations:shared.connectedLibraries", { name: (r as any).server_name || "Emby", count: (r as any).library_count || 0 }), "success");
                    else toast(r.error || t("settingsIntegrations:shared.connectionFailed"));
                  } catch { toast(t("settingsIntegrations:shared.connectionFailed")); }
                }}>{t("settingsIntegrations:shared.testConnection")}</button>
              {encoding?.emby_configured && (
                <span style={{ fontSize: 11, color: "var(--success)", display: "flex", alignItems: "center", gap: 4 }}>
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--success)" }} />
                  {t("settingsIntegrations:shared.connected")}
                </span>
              )}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding?.emby_scan_after_conversion !== false && encoding?.emby_scan_after_conversion !== "false"}
                  onChange={() => setEncoding({ ...encoding, emby_scan_after_conversion: encoding?.emby_scan_after_conversion === false || encoding?.emby_scan_after_conversion === "false" })} />
                <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{t("settingsIntegrations:shared.refreshAfter")}</span>
              </label>
            </div>
            <button className="btn btn-primary" style={{ marginTop: 16 }}
              onClick={async () => {
                await updateEncodingSettings({
                  emby_url: encoding?.emby_url,
                  emby_api_key: encoding?.emby_api_key,
                  emby_user_id: encoding?.emby_user_id,
                  emby_path_mapping: encoding?.emby_path_mapping,
                  emby_scan_after_conversion: encoding?.emby_scan_after_conversion,
                } as any);
                toast(t("settingsIntegrations:emby.saved"), "success");
              }}>{t("settingsIntegrations:emby.save")}</button>
          </div>

          {/* Sonarr / Radarr Integration */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 16 }}>Sonarr / Radarr</h3>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
              {t("settingsIntegrations:arr.intro")}
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16, marginBottom: 16 }}>
              {/* Sonarr */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 10 }}>Sonarr</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <div>
                    <label style={labelStyle}>{t("settingsIntegrations:shared.url")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="http://localhost:8989"
                      value={encoding?.sonarr_url || ""}
                      onChange={e => setEncoding({ ...encoding, sonarr_url: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>{t("settingsIntegrations:shared.apiKey")}</label>
                    <input type="password" style={{ ...inputStyle, width: "100%" }} placeholder={t("settingsIntegrations:arr.sonarrKeyPlaceholder")}
                      value={encoding?.sonarr_api_key || ""}
                      onChange={e => setEncoding({ ...encoding, sonarr_api_key: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>{t("settingsIntegrations:arr.pathMapping")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="/media=/  (container=sonarr)"
                      value={encoding?.sonarr_path_mapping || ""}
                      onChange={e => setEncoding({ ...encoding, sonarr_path_mapping: e.target.value })} />
                    <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                      {t("settingsIntegrations:arr.sonarrPathHelp")}
                    </div>
                  </div>
                </div>
              </div>
              {/* Radarr */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 10 }}>Radarr</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <div>
                    <label style={labelStyle}>{t("settingsIntegrations:shared.url")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="http://localhost:7878"
                      value={encoding?.radarr_url || ""}
                      onChange={e => setEncoding({ ...encoding, radarr_url: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>{t("settingsIntegrations:shared.apiKey")}</label>
                    <input type="password" style={{ ...inputStyle, width: "100%" }} placeholder={t("settingsIntegrations:arr.radarrKeyPlaceholder")}
                      value={encoding?.radarr_api_key || ""}
                      onChange={e => setEncoding({ ...encoding, radarr_api_key: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>{t("settingsIntegrations:arr.pathMapping")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="/media/Movies=/  (container=radarr)"
                      value={encoding?.radarr_path_mapping || ""}
                      onChange={e => setEncoding({ ...encoding, radarr_path_mapping: e.target.value })} />
                    <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                      {t("settingsIntegrations:arr.radarrPathHelp")}
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    await updateEncodingSettings(encoding);
                    toast(t("settingsIntegrations:arr.saved"), "success");
                  } catch (err: any) { toast(t("settingsIntegrations:shared.saveFailed", { error: err.message })); }
                }}
              >{t("common:actions.save")}</button>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    await updateEncodingSettings(encoding);
                    const res = await testApiKey("sonarr") as any;
                    if (res.success) toast(t("settingsIntegrations:arr.connected", { name: "Sonarr", version: res.version }), "success");
                    else toast(`Sonarr: ${res.error}`);
                  } catch (err: any) { toast(t("settingsIntegrations:arr.testFailed", { name: "Sonarr", error: err.message })); }
                }}
              >{t("settingsIntegrations:arr.testSonarr")}</button>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    await updateEncodingSettings(encoding);
                    const res = await testApiKey("radarr") as any;
                    if (res.success) toast(t("settingsIntegrations:arr.connected", { name: "Radarr", version: res.version }), "success");
                    else toast(`Radarr: ${res.error}`);
                  } catch (err: any) { toast(t("settingsIntegrations:arr.testFailed", { name: "Radarr", error: err.message })); }
                }}
              >{t("settingsIntegrations:arr.testRadarr")}</button>
            </div>
          </div>

          {/* Download Client Integration */}
          <div style={sectionStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ color: "white", margin: 0 }}>NZBGet / SABnzbd</h3>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{encoding?.nzbget_enabled ? t("settingsIntegrations:shared.enabled") : t("settingsIntegrations:shared.disabled")}</span>
                <input type="checkbox" checked={encoding?.nzbget_enabled || false}
                  onChange={() => setEncoding({ ...encoding, nzbget_enabled: !encoding?.nzbget_enabled })}
                  style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
              </label>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
              {t("settingsIntegrations:downloaders.intro")}
            </div>

            {/* Tags */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelStyle}>{t("settingsIntegrations:downloaders.tagsLabel")}</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                {(encoding?.nzbget_tags || []).map((tag: string) => (
                  <span key={tag} style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "var(--border)", padding: "4px 10px", borderRadius: 16, fontSize: 12, color: "var(--success)" }}>
                    {tag}
                    <button onClick={() => setEncoding({ ...encoding, nzbget_tags: (encoding?.nzbget_tags || []).filter((t: string) => t !== tag) })}
                      style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1 }}>&times;</button>
                  </span>
                ))}
                <input type="text" placeholder={t("settingsIntegrations:downloaders.addTag")}
                  style={{ backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 16, width: 100, padding: "4px 10px", fontSize: 12, outline: "none", height: "auto" }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.target as HTMLInputElement).value.trim()) {
                      const val = (e.target as HTMLInputElement).value.trim().toLowerCase();
                      if (!(encoding?.nzbget_tags || []).includes(val)) {
                        setEncoding({ ...encoding, nzbget_tags: [...(encoding?.nzbget_tags || []), val] });
                      }
                      (e.target as HTMLInputElement).value = "";
                    }
                  }} />
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("settingsIntegrations:downloaders.tagsHelp")}</div>
            </div>

            {/* Categories */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelStyle}>{t("settingsIntegrations:downloaders.categoriesLabel")}</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                {(encoding?.nzbget_categories || []).map((cat: string) => (
                  <span key={cat} style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "var(--border)", padding: "4px 10px", borderRadius: 16, fontSize: 12, color: "var(--success)" }}>
                    {cat}
                    <button onClick={() => setEncoding({ ...encoding, nzbget_categories: (encoding?.nzbget_categories || []).filter((c: string) => c !== cat) })}
                      style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1 }}>&times;</button>
                  </span>
                ))}
                <input type="text" placeholder={t("settingsIntegrations:downloaders.addCategory")}
                  style={{ backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 16, width: 120, padding: "4px 10px", fontSize: 12, outline: "none", height: "auto" }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.target as HTMLInputElement).value.trim()) {
                      const val = (e.target as HTMLInputElement).value.trim();
                      if (!(encoding?.nzbget_categories || []).includes(val)) {
                        setEncoding({ ...encoding, nzbget_categories: [...(encoding?.nzbget_categories || []), val] });
                      }
                      (e.target as HTMLInputElement).value = "";
                    }
                  }} />
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("settingsIntegrations:downloaders.categoriesHelp")}</div>
            </div>

            {/* Path Mappings */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelStyle}>{t("settingsIntegrations:downloaders.pathMappingsLabel")}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>{t("settingsIntegrations:downloaders.pathMappingsHelp")}</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 8 }}>
                {(encoding?.nzbget_path_mappings || []).map((m: any, i: number) => (
                  <div key={i} style={{ display: "flex", gap: 6, alignItems: "center", minWidth: 0 }}>
                    <input type="text" value={m.from || ""} placeholder="/Downloads/completed/TV"
                      style={{ ...inputStyle, flex: 1, minWidth: 0, padding: "4px 8px", fontSize: 12 }}
                      onChange={(e) => {
                        const mappings = [...(encoding?.nzbget_path_mappings || [])];
                        mappings[i] = { ...mappings[i], from: e.target.value };
                        setEncoding({ ...encoding, nzbget_path_mappings: mappings });
                      }} />
                    <span style={{ color: "var(--text-muted)", fontSize: 12 }}>→</span>
                    <input type="text" value={m.to || ""} placeholder="/downloads/tv"
                      style={{ ...inputStyle, flex: 1, minWidth: 0, padding: "4px 8px", fontSize: 12 }}
                      onChange={(e) => {
                        const mappings = [...(encoding?.nzbget_path_mappings || [])];
                        mappings[i] = { ...mappings[i], to: e.target.value };
                        setEncoding({ ...encoding, nzbget_path_mappings: mappings });
                      }} />
                    <button onClick={() => {
                      const mappings = (encoding?.nzbget_path_mappings || []).filter((_: any, j: number) => j !== i);
                      setEncoding({ ...encoding, nzbget_path_mappings: mappings });
                    }} style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", fontSize: 16, padding: "0 4px" }}>&times;</button>
                  </div>
                ))}
                <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 12px", alignSelf: "flex-start" }}
                  onClick={() => setEncoding({ ...encoding, nzbget_path_mappings: [...(encoding?.nzbget_path_mappings || []), { from: "", to: "" }] })}>
                  {t("settingsIntegrations:downloaders.addMapping")}
                </button>
              </div>
            </div>

            {/* Options row */}
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 16 }}>
              <div>
                <div style={labelStyle}>{t("settingsIntegrations:downloaders.priority")}</div>
                <select value={encoding?.nzbget_priority || "High"}
                  onChange={(e) => setEncoding({ ...encoding, nzbget_priority: e.target.value })}
                  style={{ ...inputStyle, width: 140 }}>
                  <option value="Normal">{t("settingsIntegrations:priorities.normal")}</option>
                  <option value="High">{t("settingsIntegrations:priorities.high")}</option>
                  <option value="Highest">{t("settingsIntegrations:priorities.highest")}</option>
                </select>
              </div>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 18 }}>
                <input type="checkbox" checked={encoding?.nzbget_wait_for_completion !== false}
                  onChange={() => setEncoding({ ...encoding, nzbget_wait_for_completion: encoding?.nzbget_wait_for_completion === false })}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{t("settingsIntegrations:downloaders.waitForCompletion")}</span>
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 18 }}>
                <input type="checkbox" checked={encoding?.nzbget_check_sonarr_tags !== false}
                  onChange={() => setEncoding({ ...encoding, nzbget_check_sonarr_tags: encoding?.nzbget_check_sonarr_tags === false })}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{t("settingsIntegrations:downloaders.checkSonarrTags")}</span>
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 18 }}>
                <input type="checkbox" checked={encoding?.nzbget_check_radarr_tags !== false}
                  onChange={() => setEncoding({ ...encoding, nzbget_check_radarr_tags: encoding?.nzbget_check_radarr_tags === false })}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{t("settingsIntegrations:downloaders.checkRadarrTags")}</span>
              </label>
            </div>

            {/* Save + Download */}
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 16 }}>
              <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 16px" }}
                onClick={async () => {
                  await updateEncodingSettings(encoding);
                  toast(t("settingsIntegrations:downloaders.saved"), "success");
                }}>{t("common:actions.save")}</button>
              <a href="/api/settings/nzbget-script" download="Shrinkerr.py" style={{ textDecoration: "none" }}>
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 16px" }}>
                  {t("settingsIntegrations:downloaders.downloadNzbget")}
                </button>
              </a>
              <a href="/api/settings/sabnzbd-script" download="shrinkerr.py" style={{ textDecoration: "none" }}>
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 16px" }}>
                  {t("settingsIntegrations:downloaders.downloadSabnzbd")}
                </button>
              </a>
            </div>

            {/* Installation instructions */}
            <details style={{ fontSize: 12, color: "var(--text-muted)" }} open>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>{t("settingsIntegrations:downloaders.prereq.summary")}</summary>
              <div style={{ paddingLeft: 8, lineHeight: 1.7 }}>
                <p style={{ margin: "4px 0" }}>
                  <Trans i18nKey="settingsIntegrations:downloaders.prereq.intro" components={{ em: <em /> }} />
                </p>
                <ol style={{ margin: "4px 0 8px", paddingLeft: 20 }}>
                  <li>
                    <Trans i18nKey="settingsIntegrations:downloaders.prereq.sameMount" components={{ b: <b /> }} />
                    <pre style={{ margin: "4px 0", padding: 8, background: "var(--bg-primary)", borderRadius: 4, fontSize: 11, overflow: "auto" }}>{`# nzbget docker-compose.yml
volumes:
  - /home/me/Downloads:/Downloads:rw

# shrinkerr docker-compose.yml
volumes:
  - /home/me/Downloads:/Downloads:rw   # ← same line, same case`}</pre>
                    <Trans i18nKey="settingsIntegrations:downloaders.prereq.recreate" components={{ code: <code /> }} />
                  </li>
                  <li>
                    <Trans i18nKey="settingsIntegrations:downloaders.prereq.mediaDirs" components={{ b: <b />, code: <code /> }} />
                    <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                      <li><Trans i18nKey="settingsIntegrations:downloaders.prereq.typeOther" components={{ b: <b /> }} /></li>
                      <li><Trans i18nKey="settingsIntegrations:downloaders.prereq.uncheckScan" components={{ b: <b /> }} /></li>
                    </ul>
                  </li>
                  <li>
                    <Trans i18nKey="settingsIntegrations:downloaders.prereq.pathMappings" components={{ b: <b /> }} />
                  </li>
                </ol>
              </div>
            </details>
            <details style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8 }}>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>{t("settingsIntegrations:downloaders.nzbgetInstall.summary")}</summary>
              <ol style={{ margin: 0, paddingLeft: 20, lineHeight: 2 }}>
                <li>{t("settingsIntegrations:downloaders.install.prereq")}</li>
                <li><Trans i18nKey="settingsIntegrations:downloaders.nzbgetInstall.download" components={{ b: <b /> }} /></li>
                <li><Trans i18nKey="settingsIntegrations:downloaders.nzbgetInstall.place" components={{ b: <b />, code: <code /> }} /></li>
                <li><Trans i18nKey="settingsIntegrations:downloaders.nzbgetInstall.enable" components={{ b: <b /> }} /></li>
                <li><Trans i18nKey="settingsIntegrations:downloaders.nzbgetInstall.restart" components={{ b: <b /> }} /></li>
                <li>{t("settingsIntegrations:downloaders.nzbgetInstall.autoConfig")}</li>
                <li>{t("settingsIntegrations:downloaders.install.addTags")}</li>
              </ol>
            </details>
            <details style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8 }}>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>{t("settingsIntegrations:downloaders.sabInstall.summary")}</summary>
              <ol style={{ margin: 0, paddingLeft: 20, lineHeight: 2 }}>
                <li>{t("settingsIntegrations:downloaders.install.prereq")}</li>
                <li><Trans i18nKey="settingsIntegrations:downloaders.sabInstall.download" components={{ b: <b /> }} /></li>
                <li><Trans i18nKey="settingsIntegrations:downloaders.sabInstall.place" components={{ b: <b />, code: <code /> }} /></li>
                <li><Trans i18nKey="settingsIntegrations:downloaders.sabInstall.categories" components={{ b: <b /> }} /></li>
                <li>{t("settingsIntegrations:downloaders.sabInstall.autoConfig")}</li>
                <li>{t("settingsIntegrations:downloaders.install.addTags")}</li>
              </ol>
            </details>
            <details style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8 }}>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>{t("settingsIntegrations:downloaders.troubleshoot.summary")}</summary>
              <div style={{ paddingLeft: 8, lineHeight: 1.7 }}>
                <p style={{ margin: "4px 0" }}>
                  <Trans i18nKey="settingsIntegrations:downloaders.troubleshoot.intro" components={{ code: <code /> }} />
                </p>
                <ol style={{ margin: "4px 0", paddingLeft: 20 }}>
                  <li>{t("settingsIntegrations:downloaders.troubleshoot.checkPath")}</li>
                  <li>{t("settingsIntegrations:downloaders.troubleshoot.verify")} <code>docker exec shrinkerr ls &lt;path&gt;</code></li>
                  <li><Trans i18nKey="settingsIntegrations:downloaders.troubleshoot.notThere" components={{ code: <code /> }} /></li>
                  <li>{t("settingsIntegrations:downloaders.troubleshoot.isThere")}</li>
                </ol>
              </div>
            </details>
            <div style={{ marginTop: 8, padding: "8px 12px", background: "rgba(104,96,254,0.1)", borderRadius: 4, fontSize: 12 }}>
              <Trans i18nKey="settingsIntegrations:downloaders.tip" components={{ tip: <b style={{ color: "var(--accent)" }} />, b: <b /> }} />
            </div>
          </div>

          <h2 id="rules" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsIntegrations:rules.title")}
          </h2>
          {/* Encoding Rules */}
          <div style={sectionStyle}>
            <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", marginBottom: 4 }}>
              <div style={{ display: "flex", gap: 8 }}>
                {encoding?.plex_configured && (
                  <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                    disabled={ruleSyncing}
                    onClick={async () => {
                      setRuleSyncing(true);
                      try {
                        const res = await syncPlexRuleMetadata();
                        await loadPlexOpts();
                        const parts = [];
                        if (res.labels_synced) parts.push(t("settingsIntegrations:rules.sync.labels", { count: res.labels_synced }));
                        if (res.collections_synced) parts.push(t("settingsIntegrations:rules.sync.collections", { count: res.collections_synced }));
                        if (res.genres_synced) parts.push(t("settingsIntegrations:rules.sync.genres", { count: res.genres_synced }));
                        if (res.libraries_synced) parts.push(t("settingsIntegrations:rules.sync.libraries", { count: res.libraries_synced }));
                        if (res.watch_synced) parts.push(t("settingsIntegrations:rules.sync.watch", { count: res.watch_synced }));
                        toast(t("settingsIntegrations:rules.synced", { parts: parts.join(", ") || t("settingsIntegrations:rules.noChanges") }), "success");
                      } catch (err: any) { toast(err.message || t("settingsIntegrations:rules.syncFailed")); }
                      setRuleSyncing(false);
                    }}
                  >
                    {ruleSyncing ? t("settingsIntegrations:rules.syncing") : t("settingsIntegrations:rules.syncFromPlex")}
                  </button>
                )}
                <button className="btn btn-primary" style={{ fontSize: 11, padding: "4px 10px" }}
                  onClick={() => {
                    setShowAddRule(true);
                    setEditingRuleId(null);
                    setRuleForm({ name: "", match_mode: "any", conditions: [{ type: "directory", operator: "is", value: "" }], action: "encode", encoder: "", nvenc_preset: "", nvenc_cq: "", libx265_crf: "", libx265_preset: "", target_resolution: "", audio_codec: "", audio_bitrate: "", queue_priority: "" });
                    if (plexOpts.labels.length === 0) loadPlexOpts();
                  }}
                >{t("settingsIntegrations:rules.addRule")}</button>
              </div>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14, lineHeight: 1.6 }}>
              {t("settingsIntegrations:rules.intro")}
              {encoding?.plex_configured && <span> <Trans i18nKey="settingsIntegrations:rules.introPlex" components={{ b: <b /> }} /></span>}
            </div>

            {/* Rule list */}
            {rules.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: showAddRule ? 16 : 0 }}>
                {rules.map((rule, idx) => {
                  return (
                    <div key={rule.id}
                      draggable
                      onDragStart={() => setRuleDragIdx(idx)}
                      onDragOver={(e) => { e.preventDefault(); setRuleDropIdx(idx); }}
                      onDragEnd={async () => {
                        if (ruleDragIdx !== null && ruleDropIdx !== null && ruleDragIdx !== ruleDropIdx) {
                          const ids = rules.map(r => r.id);
                          const [moved] = ids.splice(ruleDragIdx, 1);
                          ids.splice(ruleDropIdx, 0, moved);
                          await reorderEncodingRules(ids);
                          loadRules();
                        }
                        setRuleDragIdx(null);
                        setRuleDropIdx(null);
                      }}
                      style={{
                        background: ruleDropIdx === idx && ruleDragIdx !== null && ruleDragIdx !== idx ? "rgba(104,96,254,0.15)" : "var(--bg-primary)",
                        borderRadius: 4, padding: "10px 12px",
                        display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8,
                        opacity: ruleDragIdx === idx ? 0.4 : rule.enabled ? 1 : 0.5,
                        transition: "background 0.1s, opacity 0.1s",
                        cursor: "grab",
                      }}>
                      {/* Drag handle + priority + toggle + name */}
                      <span style={{ cursor: "grab", opacity: 0.3, fontSize: 14, flexShrink: 0 }} title={t("settingsIntegrations:rules.dragToReorder")}>&#x2807;</span>
                      <span style={{ color: "var(--text-muted)", fontSize: 11 }}>#{idx + 1}</span>
                      <input type="checkbox" checked={!!rule.enabled}
                        onChange={async () => {
                          await updateEncodingRule(rule.id, { enabled: !rule.enabled });
                          loadRules();
                        }}
                        style={{ accentColor: "var(--accent)" }}
                      />
                      <span style={{ color: "white", fontSize: 13, fontWeight: 500 }}>{rule.name}</span>
                      {/* Conditions — wraps naturally */}
                      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center", flex: "1 1 200px" }}>
                        {(rule.match_conditions || []).map((c: any, ci: number) => {
                          const condColors: Record<string, string> = {
                            directory: "#ffa94d", label: "#b680ff", collection: "#40ceff", genre: "#ff6b9d",
                            library: "#18ffa5", source: "#74c0fc", resolution: "#ffd43b", video_codec: "#e94560",
                            audio_codec: "#69db7c", file_size: "#ffa94d", media_type: "#6860fe", title: "#40ceff",
                            release_group: "#ff6b9d", arr_tag: "#74c0fc",
                          };
                          const fg = condColors[c.type] || "#ccc";
                          const bg = fg + "22";
                          const display = c.type === "directory"
                            ? c.value.split("/").filter(Boolean).pop() || c.value
                            : c.value;
                          const opLabel = c.operator === "is" ? "" : c.operator === "is_not" ? "!=" : c.operator === "contains" ? "~" : c.operator === "does_not_contain" ? "!~" : c.operator === "greater_than" ? ">" : c.operator === "less_than" ? "<" : "";
                          const suffix = c.type === "file_size" ? " GB" : "";
                          return (
                            <span key={ci} style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 11, whiteSpace: "nowrap" }}>
                              {ci > 0 && <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{rule.match_mode === "all" ? t("settingsIntegrations:rules.and") : t("settingsIntegrations:rules.or")}</span>}
                              <span style={{ fontSize: 9, padding: "1px 4px", borderRadius: 6, fontWeight: "bold", background: bg, color: fg }}>
                                {t(`settingsIntegrations:conditions.badges.${c.type}`, { defaultValue: c.type.replace("_", " ") })}
                              </span>
                              {opLabel && <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{opLabel}</span>}
                              <span style={{ color: "var(--text-secondary)", fontSize: 11 }} title={c.value}>{display}{suffix}</span>
                            </span>
                          );
                        })}
                      </div>
                      {/* Action badge + settings + buttons */}
                      <span style={{
                        fontSize: 10, padding: "1px 6px", borderRadius: 8, fontWeight: "bold", whiteSpace: "nowrap",
                        background: rule.action === "encode" ? "rgba(24,255,165,0.15)" : rule.action === "skip" ? "rgba(233,69,96,0.15)" : "rgba(255,169,77,0.15)",
                        color: rule.action === "encode" ? "#18ffa5" : rule.action === "skip" ? "#e94560" : "#ffa94d",
                      }}>
                        {rule.action === "encode" ? t("settingsIntegrations:rules.actionBadge.encode") : rule.action === "skip" ? t("settingsIntegrations:rules.actionBadge.skip") : t("settingsIntegrations:rules.actionBadge.ignore")}
                      </span>
                      {rule.action !== "skip" && (
                        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          {[
                            rule.action === "encode" && rule.encoder && rule.encoder !== "nvenc" ? rule.encoder : null,
                            rule.action === "encode" && rule.nvenc_preset ? rule.nvenc_preset.toUpperCase() : null,
                            rule.action === "encode" && rule.nvenc_cq ? `CQ${rule.nvenc_cq}` : null,
                            rule.action === "encode" && rule.libx265_crf ? `CRF${rule.libx265_crf}` : null,
                            rule.action === "encode" && rule.target_resolution && rule.target_resolution !== "copy" ? rule.target_resolution : null,
                            rule.audio_codec && rule.audio_codec !== "copy" ? `${rule.audio_codec.toUpperCase()}${rule.audio_bitrate ? ` ${rule.audio_bitrate}k` : ""}` : null,
                            rule.queue_priority != null ? [t("settingsIntegrations:priorities.normal"), t("settingsIntegrations:priorities.high"), t("settingsIntegrations:priorities.highest")][rule.queue_priority] : null,
                          ].filter(Boolean).join(" ") || t("settingsIntegrations:rules.defaults")}
                        </span>
                      )}
                      <div style={{ display: "flex", gap: 4, marginLeft: 8 }}>
                        <button style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", padding: 2, fontSize: 12 }}
                          onClick={() => {
                            setEditingRuleId(rule.id);
                            setShowAddRule(true);
                            if (plexOpts.labels.length === 0) loadPlexOpts();
                            setRuleForm({
                              name: rule.name,
                              match_mode: rule.match_mode || "any",
                              conditions: (rule.match_conditions || []).map((c: any) => ({
                                type: c.type || "directory",
                                operator: c.operator || "is",
                                value: c.value || "",
                              })),
                              action: rule.action, encoder: rule.encoder || "",
                              nvenc_preset: rule.nvenc_preset || "", nvenc_cq: rule.nvenc_cq ? String(rule.nvenc_cq) : "",
                              libx265_crf: rule.libx265_crf ? String(rule.libx265_crf) : "",
                              libx265_preset: rule.libx265_preset || "",
                              target_resolution: rule.target_resolution || "",
                              audio_codec: rule.audio_codec || "", audio_bitrate: rule.audio_bitrate ? String(rule.audio_bitrate) : "",
                              queue_priority: rule.queue_priority != null ? String(rule.queue_priority) : "",
                            });
                          }}
                          title={t("settingsIntegrations:rules.edit")}
                        >&#9998;</button>
                        <button style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", padding: 2, fontSize: 12 }}
                          onClick={async () => {
                            await deleteEncodingRule(rule.id);
                            loadRules();
                          }}
                          title={t("common:actions.delete")}
                        >&times;</button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {rules.length === 0 && !showAddRule && (
              <div style={{ textAlign: "center", padding: 20, opacity: 0.4, fontSize: 13 }}>
                {encoding?.plex_configured ? t("settingsIntegrations:rules.emptyPlex") : t("settingsIntegrations:rules.empty")}
              </div>
            )}

            {/* Add/Edit Rule form */}
            {showAddRule && (
              <div style={{ background: "var(--bg-primary)", borderRadius: 4, padding: 16, marginTop: rules.length > 0 ? 0 : 8 }}>
                {/* Rule name */}
                <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 12 }}>
                  <label style={labelStyle}>{t("settingsIntegrations:rules.form.name")}</label>
                  <input style={{ ...inputStyle, width: 300 }} value={ruleForm.name} placeholder={t("settingsIntegrations:rules.form.namePlaceholder")}
                    onChange={e => setRuleForm({ ...ruleForm, name: e.target.value })} />
                </div>

                {/* Match conditions */}
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>{t("settingsIntegrations:rules.form.matchConditions")}</label>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4, marginBottom: 12 }}>
                    <select value={ruleForm.match_mode} onChange={e => setRuleForm({...ruleForm, match_mode: e.target.value})}
                      style={{ ...inputStyle, width: 260, fontWeight: 500 }}>
                      <option value="any">{t("settingsIntegrations:rules.form.matchAny")}</option>
                      <option value="all">{t("settingsIntegrations:rules.form.matchAll")}</option>
                    </select>
                    <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 8px" }}
                      onClick={() => setRuleForm({...ruleForm, conditions: [...ruleForm.conditions, { type: "directory", operator: "is", value: "" }]})}>
                      +
                    </button>
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {ruleForm.conditions.map((cond, condIdx) => (
                      <div key={condIdx} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        {/* Type */}
                        <select value={cond.type} onChange={e => updateConditionType(condIdx, e.target.value)} style={{ ...inputStyle, width: 160 }}>
                          <optgroup label={t("settingsIntegrations:conditions.groups.path")}>
                            <option value="directory">{t("settingsIntegrations:conditions.types.directory")}</option>
                          </optgroup>
                          <optgroup label={t("settingsIntegrations:conditions.groups.file")}>
                            <option value="source">{t("settingsIntegrations:conditions.types.source")}</option>
                            <option value="resolution">{t("settingsIntegrations:conditions.types.resolution")}</option>
                            <option value="video_codec">{t("settingsIntegrations:conditions.types.video_codec")}</option>
                            <option value="audio_codec">{t("settingsIntegrations:conditions.types.audio_codec")}</option>
                            <option value="file_size">{t("settingsIntegrations:conditions.types.file_size")}</option>
                            <option value="date_added">{t("settingsIntegrations:conditions.types.date_added")}</option>
                            <option value="media_type">{t("settingsIntegrations:conditions.types.media_type")}</option>
                            <option value="title">{t("settingsIntegrations:conditions.types.title")}</option>
                            <option value="release_group">{t("settingsIntegrations:conditions.types.release_group")}</option>
                          </optgroup>
                          <optgroup label="Plex">
                            <option value="label">{t("settingsIntegrations:conditions.plexOptions.label")}</option>
                            <option value="collection">{t("settingsIntegrations:conditions.plexOptions.collection")}</option>
                            <option value="genre">{t("settingsIntegrations:conditions.plexOptions.genre")}</option>
                            <option value="library">{t("settingsIntegrations:conditions.plexOptions.library")}</option>
                          </optgroup>
                          <optgroup label="Arr">
                            <option value="arr_tag">{t("settingsIntegrations:conditions.types.arr_tag")}</option>
                          </optgroup>
                          <optgroup label="Jellyfin">
                            <option value="jellyfin_tag">{t("settingsIntegrations:conditions.types.jellyfin_tag")}</option>
                          </optgroup>
                          <optgroup label="Emby">
                            <option value="emby_tag">{t("settingsIntegrations:conditions.types.emby_tag")}</option>
                            <option value="emby_watched">{t("settingsIntegrations:conditions.types.emby_watched")}</option>
                          </optgroup>
                          <optgroup label={t("settingsIntegrations:conditions.groups.downloads")}>
                            <option value="nzbget_category">{t("settingsIntegrations:conditions.types.nzbget_category")}</option>
                          </optgroup>
                        </select>
                        {/* Operator */}
                        <select value={cond.operator} onChange={e => updateConditionOperator(condIdx, e.target.value)} style={{ ...inputStyle, width: 140 }}>
                          {(CONDITION_TYPES[cond.type]?.operators || []).map(op => (
                            <option key={op.value} value={op.value}>{op.label}</option>
                          ))}
                        </select>
                        {/* Value */}
                        {(() => {
                          const ct = CONDITION_TYPES[cond.type];
                          if (!ct) return null;

                          if (cond.type === "directory") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.selectDirectory")}</option>
                              {dirs.map(d => <option key={d.path} value={d.path}>{d.label ? `${d.label} (${d.path})` : d.path}</option>)}
                            </select>;
                          }

                          if (cond.type === "source") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              {(condOpts.sources || []).map((s: string) => <option key={s} value={s}>{s}</option>)}
                            </select>;
                          }

                          if (cond.type === "resolution") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              {(condOpts.resolutions || []).map((r: string) => <option key={r} value={r}>{r}</option>)}
                            </select>;
                          }

                          if (cond.type === "video_codec") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              {(condOpts.video_codecs || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                            </select>;
                          }

                          if (cond.type === "audio_codec") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              {(condOpts.audio_codecs || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                            </select>;
                          }

                          if (cond.type === "media_type") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              <option value="movie">{t("settingsIntegrations:conditions.values.movie")}</option>
                              <option value="tv">{t("settingsIntegrations:conditions.values.tv")}</option>
                            </select>;
                          }

                          if (cond.type === "release_group") {
                            return <div style={{ display: "flex", gap: 6, flex: 1 }}>
                              <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                                <option value="">{t("settingsIntegrations:conditions.values.selectOrType")}</option>
                                {(condOpts.release_groups || []).map((g: string) => <option key={g} value={g}>{g}</option>)}
                              </select>
                              <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder={t("settingsIntegrations:conditions.values.typeGroupName")}
                                onChange={e => updateConditionValue(condIdx, e.target.value)} />
                            </div>;
                          }

                          if (cond.type === "label") {
                            return <div style={{ display: "flex", gap: 6, flex: 1 }}>
                              <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                                <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                                {(plexOpts.labels || []).map((l: string) => <option key={l} value={l}>{l}</option>)}
                              </select>
                              <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder={t("settingsIntegrations:conditions.values.typeManually")}
                                onChange={e => updateConditionValue(condIdx, e.target.value)} />
                            </div>;
                          }

                          if (cond.type === "collection") {
                            return <div style={{ display: "flex", gap: 6, flex: 1 }}>
                              <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                                <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                                {(plexOpts.collections || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                              </select>
                              <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder={t("settingsIntegrations:conditions.values.typeManually")}
                                onChange={e => updateConditionValue(condIdx, e.target.value)} />
                            </div>;
                          }

                          if (cond.type === "genre") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              {(plexOpts.genres || []).map((g: string) => <option key={g} value={g}>{g}</option>)}
                            </select>;
                          }

                          if (cond.type === "library") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              {(plexOpts.libraries || []).map((l: any) => <option key={l.title} value={l.title}>{l.title}</option>)}
                            </select>;
                          }

                          if (cond.type === "arr_tag") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.selectTag")}</option>
                              {(condOpts.arr_tags || []).map((t: any) => <option key={`${t.source}-${t.label}`} value={t.label}>{t.label} ({t.source})</option>)}
                            </select>;
                          }

                          if (cond.type === "jellyfin_tag") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.selectTag")}</option>
                              {(condOpts.jellyfin_tags || []).map((t: string) => <option key={t} value={t}>{t}</option>)}
                            </select>;
                          }

                          if (cond.type === "emby_tag") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.selectTag")}</option>
                              {(condOpts.emby_tags || []).map((t: string) => <option key={t} value={t}>{t}</option>)}
                            </select>;
                          }

                          if (cond.type === "emby_watched") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.select")}</option>
                              <option value="true">{t("settingsIntegrations:conditions.values.watched")}</option>
                              <option value="false">{t("settingsIntegrations:conditions.values.unwatched")}</option>
                            </select>;
                          }

                          if (cond.type === "nzbget_category") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">{t("settingsIntegrations:conditions.values.selectCategory")}</option>
                              {(condOpts.nzbget_categories || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                            </select>;
                          }

                          if (cond.type === "date_added") {
                            const m = (cond.value || "").match(/^(\d+)([hdw])$/);
                            const num = m ? m[1] : "";
                            const unit = m ? m[2] : "h";
                            const setBoth = (n: string, u: string) =>
                              updateConditionValue(condIdx, `${n || "0"}${u}`);
                            return (
                              <div style={{ display: "flex", gap: 6, flex: 1 }}>
                                <input style={{ ...inputStyle, width: 80 }}
                                       type="number" min="1" placeholder="24"
                                       value={num}
                                       onChange={e => setBoth(e.target.value, unit)} />
                                <select style={{ ...inputStyle, width: 110 }}
                                        value={unit}
                                        onChange={e => setBoth(num, e.target.value)}>
                                  <option value="h">{t("settingsIntegrations:conditions.values.hours")}</option>
                                  <option value="d">{t("settingsIntegrations:conditions.values.days")}</option>
                                  <option value="w">{t("settingsIntegrations:conditions.values.weeks")}</option>
                                </select>
                              </div>
                            );
                          }

                          if (ct.valueType === "number") {
                            return <input type="number" step="0.1" style={{ ...inputStyle, flex: 1 }} value={cond.value}
                              placeholder={t("settingsIntegrations:conditions.values.sizePlaceholder")} onChange={e => updateConditionValue(condIdx, e.target.value)} />;
                          }

                          // Default: text input
                          return <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder={t("settingsIntegrations:conditions.values.enterValue")}
                            onChange={e => updateConditionValue(condIdx, e.target.value)} />;
                        })()}
                        {/* Remove button */}
                        {ruleForm.conditions.length > 1 && (
                          <button style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", fontSize: 14, padding: 2 }}
                            onClick={() => {
                              const updated = ruleForm.conditions.filter((_, i) => i !== condIdx);
                              setRuleForm({ ...ruleForm, conditions: updated });
                            }}
                            title={t("settingsIntegrations:conditions.values.removeCondition")}>&times;</button>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 12 }}>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 12 }}>
                    <div>
                      <label style={labelStyle}>{t("settingsIntegrations:rules.form.action")}</label>
                      <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.action}
                        onChange={e => setRuleForm({ ...ruleForm, action: e.target.value })}>
                        <option value="encode">{t("settingsIntegrations:rules.form.actions.encode")}</option>
                        <option value="ignore">{t("settingsIntegrations:rules.form.actions.ignore")}</option>
                        <option value="skip">{t("settingsIntegrations:rules.form.actions.skip")}</option>
                      </select>
                    </div>
                    {ruleForm.action === "encode" && <>
                      <div>
                        <label style={labelStyle}>{t("settingsIntegrations:rules.form.encoder")}</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.encoder}
                          onChange={e => setRuleForm({ ...ruleForm, encoder: e.target.value })}>
                          <option value="">{t("settingsIntegrations:rules.form.useDefault")}</option>
                          {(encoderCaps?.nvenc ?? true) && <option value="nvenc">NVENC (NVIDIA GPU)</option>}
                          {encoderCaps?.qsv && <option value="qsv">Intel QSV</option>}
                          {encoderCaps?.vaapi && <option value="vaapi">VAAPI (Intel/AMD)</option>}
                          <option value="libx265">libx265 (CPU)</option>
                        </select>
                        {(ruleForm.encoder === "qsv" || ruleForm.encoder === "vaapi") && (
                          <div style={{ ...helpStyle, marginTop: 4 }}>
                            {t("settingsIntegrations:rules.form.qsvVaapiNote")}
                          </div>
                        )}
                      </div>
                      {/* Preset / quality controls don't apply to QSV / VAAPI in
                          v0.3.69 — those encoders inherit their global QSV /
                          VAAPI settings. NVENC and libx265 preset/CQ/CRF
                          fields shown only when one of those is selected
                          (or when no override is set, in which case the rule
                          inherits whichever encoder Settings → Encoding picks). */}
                      {(ruleForm.encoder === "" || ruleForm.encoder === "nvenc" || ruleForm.encoder === "libx265") && <>
                      <div>
                        <label style={labelStyle}>{t("settingsIntegrations:rules.form.preset")}</label>
                        {ruleForm.encoder === "libx265" ? (
                          <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.libx265_preset}
                            onChange={e => setRuleForm({ ...ruleForm, libx265_preset: e.target.value })}>
                            <option value="">{t("settingsIntegrations:rules.form.useDefault")}</option>
                            {["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"].map(p => <option key={p} value={p}>{p}</option>)}
                          </select>
                        ) : (
                          <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.nvenc_preset}
                            onChange={e => setRuleForm({ ...ruleForm, nvenc_preset: e.target.value })}>
                            <option value="">{t("settingsIntegrations:rules.form.useDefault")}</option>
                            {["p1","p2","p3","p4","p5","p6","p7"].map(p => <option key={p} value={p}>{p.toUpperCase()}</option>)}
                          </select>
                        )}
                      </div>
                      <div>
                        <label style={labelStyle}>{ruleForm.encoder === "libx265" ? "CRF" : "CQ"}</label>
                        <input type="number" style={{ ...inputStyle, width: "100%" }}
                          value={ruleForm.encoder === "libx265" ? ruleForm.libx265_crf : ruleForm.nvenc_cq}
                          placeholder={t("settingsIntegrations:rules.form.default")} min={15} max={ruleForm.encoder === "libx265" ? 28 : 40}
                          onChange={e => {
                            if (ruleForm.encoder === "libx265") {
                              setRuleForm({ ...ruleForm, libx265_crf: e.target.value });
                            } else {
                              setRuleForm({ ...ruleForm, nvenc_cq: e.target.value });
                            }
                          }}
                        />
                      </div>
                      </>}
                      <div>
                        <label style={labelStyle}>{t("settingsIntegrations:rules.form.resolution")}</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.target_resolution}
                          onChange={e => setRuleForm({ ...ruleForm, target_resolution: e.target.value })}>
                          <option value="">{t("settingsIntegrations:rules.form.useDefault")}</option>
                          <option value="copy">{t("settingsMedia:options.resolutions.copy.label")}</option>
                          <option value="1080p">1080p</option>
                          <option value="720p">720p</option>
                          <option value="480p">480p</option>
                        </select>
                      </div>
                    </>}
                    {ruleForm.action !== "skip" && <>
                      <div>
                        <label style={labelStyle}>{t("settingsIntegrations:rules.form.audioCodec")}</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.audio_codec}
                          onChange={e => setRuleForm({ ...ruleForm, audio_codec: e.target.value })}>
                          <option value="">{t("settingsIntegrations:rules.form.useDefault")}</option>
                          <option value="copy">{t("settingsIntegrations:rules.form.copyNoConversion")}</option>
                          <option value="eac3">EAC3</option>
                          <option value="ac3">AC3</option>
                          <option value="aac">AAC</option>
                          <option value="opus">Opus</option>
                          <option value="flac">FLAC</option>
                        </select>
                      </div>
                      <div>
                        <label style={labelStyle}>{t("settingsIntegrations:rules.form.audioBitrate")}</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.audio_bitrate}
                          onChange={e => setRuleForm({ ...ruleForm, audio_bitrate: e.target.value })}>
                          <option value="">{t("settingsIntegrations:rules.form.useDefault")}</option>
                          <option value="640">{t("settingsIntegrations:rules.form.bitrates.640")}</option>
                          <option value="448">{t("settingsIntegrations:rules.form.bitrates.448")}</option>
                          <option value="256">{t("settingsIntegrations:rules.form.bitrates.256")}</option>
                          <option value="128">{t("settingsIntegrations:rules.form.bitrates.128")}</option>
                        </select>
                      </div>
                    </>}
                    {ruleForm.action !== "skip" && (
                      <div>
                        <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("settingsIntegrations:rules.form.queuePriority")}</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.queue_priority}
                          onChange={e => setRuleForm({ ...ruleForm, queue_priority: e.target.value })}>
                          <option value="">{t("settingsIntegrations:rules.form.default")}</option>
                          <option value="0">{t("settingsIntegrations:priorities.normal")}</option>
                          <option value="1">{t("settingsIntegrations:priorities.high")}</option>
                          <option value="2">{t("settingsIntegrations:priorities.highest")}</option>
                        </select>
                      </div>
                    )}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="btn btn-primary" style={{ fontSize: 12, padding: "4px 12px" }}
                    onClick={async () => {
                      // Validation with feedback
                      if (!ruleForm.name.trim()) {
                        toast(t("settingsIntegrations:rules.toasts.nameRequired"));
                        return;
                      }
                      const validConditions = ruleForm.conditions.filter(c => c.value);
                      if (validConditions.length === 0) {
                        toast(t("settingsIntegrations:rules.toasts.conditionRequired"));
                        return;
                      }
                      const emptyConditions = ruleForm.conditions.filter(c => !c.value);
                      if (emptyConditions.length > 0 && validConditions.length > 0) {
                        // Has some empty conditions — just skip them silently
                      }
                      const data: any = {
                        name: ruleForm.name,
                        match_mode: ruleForm.match_mode,
                        match_conditions: validConditions.map(c => ({ type: c.type, operator: c.operator, value: c.value })),
                        action: ruleForm.action,
                      };
                      data.queue_priority = ruleForm.queue_priority ? parseInt(ruleForm.queue_priority) : null;
                      if (ruleForm.action === "encode") {
                        data.encoder = ruleForm.encoder || null;
                        data.nvenc_preset = ruleForm.nvenc_preset || null;
                        data.libx265_preset = ruleForm.libx265_preset || null;
                        data.nvenc_cq = ruleForm.nvenc_cq ? parseInt(ruleForm.nvenc_cq) : null;
                        data.libx265_crf = ruleForm.libx265_crf ? parseInt(ruleForm.libx265_crf) : null;
                        data.target_resolution = ruleForm.target_resolution || null;
                        data.audio_codec = ruleForm.audio_codec || null;
                        data.audio_bitrate = ruleForm.audio_bitrate ? parseInt(ruleForm.audio_bitrate) : null;
                      } else if (ruleForm.action === "ignore") {
                        data.encoder = null;
                        data.nvenc_preset = null;
                        data.nvenc_cq = null;
                        data.libx265_crf = null;
                        data.target_resolution = null;
                        data.audio_codec = ruleForm.audio_codec || null;
                        data.audio_bitrate = ruleForm.audio_bitrate ? parseInt(ruleForm.audio_bitrate) : null;
                      } else {
                        data.encoder = null;
                        data.nvenc_preset = null;
                        data.nvenc_cq = null;
                        data.libx265_crf = null;
                        data.target_resolution = null;
                        data.audio_codec = null;
                        data.audio_bitrate = null;
                        data.queue_priority = null;
                      }
                      try {
                        if (editingRuleId) {
                          await updateEncodingRule(editingRuleId, data);
                          toast(t("settingsIntegrations:rules.toasts.updated"), "success");
                        } else {
                          await createEncodingRule(data);
                          toast(t("settingsIntegrations:rules.toasts.created"), "success");
                        }
                        setShowAddRule(false);
                        setEditingRuleId(null);
                        // Small delay to let DB commit, then reload
                        setTimeout(loadRules, 200);
                      } catch (err: any) {
                        toast(err.message || t("settingsIntegrations:rules.toasts.saveFailed"));
                      }
                    }}
                  >
                    {editingRuleId ? t("settingsIntegrations:rules.form.update") : t("settingsIntegrations:rules.form.add")}
                  </button>
                  <button className="btn btn-secondary" style={{ fontSize: 12, padding: "4px 12px" }}
                    onClick={() => { setShowAddRule(false); setEditingRuleId(null); }}
                  >{t("common:actions.cancel")}</button>
                </div>
              </div>
            )}
          </div>

          <h2 id="renaming" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsSystem:renaming.title")}
          </h2>
          <RenamingSettings />

          <h2 id="automation" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsSystem:automation.title")}
          </h2>
          {/* Automation — at the bottom */}
          <div style={sectionStyle}>
            <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsSystem:automation.autoQueue.title")}</div>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input type="checkbox" checked={encoding?.auto_queue_new || false}
                onClick={() => setEncoding({ ...encoding, auto_queue_new: !encoding?.auto_queue_new })}
                readOnly
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsSystem:automation.autoQueue.newFiles")}</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26, marginBottom: 16 }}>
              {t("settingsSystem:automation.autoQueue.newFilesHelp")}
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10, marginBottom: 16,
                          opacity: encoding?.auto_queue_new ? 1 : 0.5 }}>
              <span style={labelStyle}>{t("settingsSystem:automation.autoQueue.priority")}</span>
              <select style={{ ...inputStyle, width: 130 }}
                value={String(encoding?.auto_queue_priority ?? 0)}
                disabled={!encoding?.auto_queue_new}
                onChange={e => setEncoding({
                  ...encoding,
                  auto_queue_priority: parseInt(e.target.value, 10),
                })}>
                <option value="0">{t("settingsIntegrations:priorities.normal")}</option>
                <option value="1">{t("settingsIntegrations:priorities.high")}</option>
                <option value="2">{t("settingsIntegrations:priorities.highest")}</option>
              </select>
              <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 6 }}>
                {t("settingsSystem:automation.autoQueue.priorityHelp")}
              </span>
            </div>

            {/* Conversion filters */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsSystem:automation.filters.title")}</div>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("settingsSystem:automation.filters.minFileSize")}</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.min_file_size_mb ?? 0}
                    onChange={e => setEncoding({ ...encoding, min_file_size_mb: e.target.value })} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("settingsSystem:automation.filters.minBitrate")}</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.min_bitrate_mbps ?? 0}
                    onChange={e => setEncoding({ ...encoding, min_bitrate_mbps: e.target.value })} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("settingsSystem:automation.filters.maxBitrate")}</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.max_bitrate_mbps ?? 0}
                    onChange={e => setEncoding({ ...encoding, max_bitrate_mbps: e.target.value })} />
                </div>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8, lineHeight: 1.6 }}>
                {t("settingsSystem:automation.filters.help")}
              </div>
            </div>

            {/* Output Filename */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsSystem:automation.filename.title")}</div>
              <div style={labelStyle}>{t("settingsSystem:automation.filename.suffix")}</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
                <input type="text" style={{ ...inputStyle, flex: "1 1 200px", maxWidth: 300 }}
                  value={encoding?.filename_suffix ?? ""}
                  onChange={e => setEncoding({ ...encoding, filename_suffix: e.target.value })}
                  placeholder={t("settingsSystem:automation.filename.suffixPlaceholder")} />
                {!encoding?.filename_suffix && (
                  <button className="btn btn-secondary" style={{ fontSize: 11, padding: "5px 10px", whiteSpace: "nowrap" }}
                    onClick={() => setEncoding({ ...encoding, filename_suffix: "-Shrinkerr" })}>
                    {t("settingsSystem:automation.filename.useSuffix", { suffix: "-Shrinkerr" })}
                  </button>
                )}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                <Trans
                  i18nKey="settingsSystem:automation.filename.help"
                  values={{ suffix: encoding?.filename_suffix || "" }}
                  components={{ code: <code style={{ fontSize: 10, padding: "1px 4px", background: "var(--bg-primary)", borderRadius: 2 }} /> }}
                />
              </div>
            </div>

            {/* Originals & Backups */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 12 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsSystem:automation.originals.title")}</div>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input type="checkbox" checked={encoding?.trash_original_after_conversion || false}
                onChange={() => setEncoding({ ...encoding, trash_original_after_conversion: !encoding?.trash_original_after_conversion })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsSystem:automation.originals.trash")}</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26, marginBottom: 12 }}>
              {t("settingsSystem:automation.originals.trashHelp")}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, paddingLeft: 26, marginBottom: 10 }}>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("settingsSystem:automation.originals.backupFor")}</span>
              <input type="number" min={0} style={{ ...inputStyle, width: 60 }}
                value={encoding?.backup_original_days ?? 0}
                onChange={e => setEncoding({ ...encoding, backup_original_days: e.target.value })} />
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("settingsSystem:automation.originals.days")}</span>
              <span style={{ fontSize: 11, color: "var(--text-muted)", opacity: 0.6 }}>{t("settingsSystem:automation.originals.daysHint")}</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, paddingLeft: 26, marginBottom: 10 }}>
              <span style={{ fontSize: 12, color: "var(--text-muted)", flexShrink: 0 }}>{t("settingsSystem:automation.originals.backupFolder")}</span>
              <input type="text" style={{ ...inputStyle, flex: 1 }}
                value={encoding?.backup_folder ?? ""}
                onChange={e => setEncoding({ ...encoding, backup_folder: e.target.value })}
                placeholder={t("settingsSystem:automation.originals.backupFolderPlaceholder")} />
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "5px 10px", whiteSpace: "nowrap", flexShrink: 0 }}
                onClick={() => setBackupBrowserOpen(true)}
              >{t("settingsSystem:automation.originals.browse")}</button>
            </div>
            <FolderBrowser
              isOpen={backupBrowserOpen}
              initialPath={encoding?.backup_folder || "/media"}
              onSelect={(path) => { setEncoding({ ...encoding, backup_folder: path }); setBackupBrowserOpen(false); }}
              onCancel={() => setBackupBrowserOpen(false)}
            />
            <div style={{ fontSize: 11, color: "var(--text-muted)", paddingLeft: 26, marginBottom: 6 }}>
              <Trans
                i18nKey="settingsSystem:automation.originals.help"
                components={{ code: <code style={{ fontSize: 10, padding: "1px 4px", background: "var(--bg-primary)", borderRadius: 2 }} />, b: <strong /> }}
              />
            </div>
            <div style={{ paddingLeft: 26, marginBottom: 16 }}>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "4px 12px", borderRadius: 4, color: "#e94560" }}
                onClick={async () => {
                  const { getBackups, deleteBackups } = await import("../api");
                  const data = await getBackups();
                  if (data.total_count === 0) {
                    alert(t("settingsSystem:automation.originals.noneFound"));
                    return;
                  }
                  const sizeGB = (data.total_size / (1024 ** 3)).toFixed(1);
                  if (confirm(t("settingsSystem:automation.originals.deleteAllConfirm", { count: data.total_count, size: sizeGB }))) {
                    const result = await deleteBackups();
                    alert(t("settingsSystem:automation.originals.deletedResult", { count: result.deleted, size: (result.freed / (1024 ** 3)).toFixed(1) }));
                  }
                }}
              >
                {t("settingsSystem:automation.originals.deleteAll")}
              </button>
            </div>

            {/* File Stability */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 12 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsSystem:automation.stability.title")}</div>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input type="checkbox" checked={encoding?.skip_files_newer_enabled || false}
                onChange={() => setEncoding({ ...encoding, skip_files_newer_enabled: !encoding?.skip_files_newer_enabled })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>{t("settingsSystem:automation.stability.delay")}</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
              {t("settingsSystem:automation.stability.delayHelp")}
            </div>
            {encoding?.skip_files_newer_enabled && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, paddingLeft: 26, marginTop: 8 }}>
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("settingsSystem:automation.stability.delayNewerThan")}</span>
                <input type="number" min={1} max={1440}
                  style={{ ...inputStyle, width: 70 }}
                  value={encoding?.skip_files_newer_than_minutes ?? 10}
                  onChange={e => setEncoding({ ...encoding, skip_files_newer_than_minutes: e.target.value })} />
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("settingsSystem:automation.stability.minutes")}</span>
              </div>
            )}

            {/* Health Checks */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 12 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsSystem:automation.health.title")}</div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <span style={{ ...labelStyle, flex: "0 0 240px" }}>{t("settingsSystem:automation.health.afterScan")}</span>
              <select
                style={{ ...inputStyle, width: 140 }}
                value={encoding?.health_check_on_scan ?? "off"}
                onChange={e => setEncoding({ ...encoding, health_check_on_scan: e.target.value })}
              >
                <option value="off">{t("settingsSystem:automation.health.off")}</option>
                <option value="quick">{t("settingsSystem:automation.health.quick")}</option>
                <option value="thorough">{t("settingsSystem:automation.health.thorough")}</option>
              </select>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 0, marginBottom: 8 }}>
              <Trans i18nKey="settingsSystem:automation.health.afterScanHelp" components={{ b: <strong /> }} />
            </div>
            <div style={{ marginBottom: 14, paddingLeft: 0 }}>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "4px 10px", color: "#e94560", borderColor: "rgba(233,69,96,0.4)" }}
                onClick={async () => {
                  if (!confirm(t("settingsSystem:automation.health.clearPendingConfirm"))) return;
                  const { clearPendingHealthChecks } = await import("../api");
                  const res = await clearPendingHealthChecks();
                  toast(t("settingsSystem:toasts.clearedPending", { count: res.deleted, formatted: res.deleted.toLocaleString() }), "success");
                }}
              >{t("settingsSystem:automation.health.clearPending")}</button>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <span style={{ ...labelStyle, flex: "0 0 240px" }}>{t("settingsSystem:automation.health.afterConversion")}</span>
              <select
                style={{ ...inputStyle, width: 140 }}
                value={encoding?.health_check_after_conversion ?? "off"}
                onChange={e => setEncoding({ ...encoding, health_check_after_conversion: e.target.value })}
              >
                <option value="off">{t("settingsSystem:automation.health.off")}</option>
                <option value="quick">{t("settingsSystem:automation.health.quick")}</option>
                <option value="thorough">{t("settingsSystem:automation.health.thorough")}</option>
              </select>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 0, marginBottom: 12 }}>
              <Trans i18nKey="settingsSystem:automation.health.afterConversionHelp" components={{ b: <strong /> }} />
            </div>

            {/* Advanced */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>{t("settingsSystem:automation.advanced.title")}</div>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 10 }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 300px" }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("settingsSystem:automation.advanced.customFlags")}</label>
                  <input type="text" style={{ ...inputStyle, width: "100%" }}
                    placeholder={t("settingsSystem:automation.advanced.customFlagsPlaceholder")}
                    value={encoding?.custom_ffmpeg_flags ?? ""}
                    onChange={e => setEncoding({ ...encoding, custom_ffmpeg_flags: e.target.value })} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("settingsSystem:automation.advanced.maxPlexCalls")}</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.max_plex_api_calls ?? 0}
                    onChange={e => setEncoding({ ...encoding, max_plex_api_calls: e.target.value })} />
                </div>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
                {t("settingsSystem:automation.advanced.help")}
              </div>
            </div>

            <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ marginTop: 16 }}>
              {t("common:actions.save")}
            </button>
          </div>

          {/* Webhook Endpoints */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>{t("settingsSystem:webhooks.title")}</h3>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              <Trans i18nKey="settingsSystem:webhooks.intro" components={{ code: <code style={{ color: "var(--accent)" }} /> }} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {[
                { method: "POST", path: "/api/webhooks/scan", desc: t("settingsSystem:webhooks.endpoints.scan") },
                { method: "POST", path: "/api/webhooks/queue", desc: t("settingsSystem:webhooks.endpoints.queue") },
                { method: "POST", path: "/api/webhooks/pause", desc: t("settingsSystem:webhooks.endpoints.pause") },
                { method: "POST", path: "/api/webhooks/resume", desc: t("settingsSystem:webhooks.endpoints.resume") },
                { method: "GET", path: "/api/webhooks/status", desc: t("settingsSystem:webhooks.endpoints.status") },
              ].map(ep => (
                <div key={ep.path} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                  <span style={{ color: ep.method === "GET" ? "#40ceff" : "var(--success)", fontWeight: 600, width: 40, flexShrink: 0 }}>{ep.method}</span>
                  <code style={{ color: "var(--text-secondary)", flex: 1 }}>{ep.path}</code>
                  <span style={{ color: "var(--text-muted)", fontSize: 11 }}>{ep.desc}</span>
                  <button
                    title={t("settingsSystem:webhooks.copyUrl")}
                    onClick={() => {
                      const url = `${window.location.origin}${ep.path}`;
                      const ta = document.createElement("textarea");
                      ta.value = url;
                      ta.style.position = "fixed";
                      ta.style.opacity = "0";
                      document.body.appendChild(ta);
                      ta.select();
                      document.execCommand("copy");
                      document.body.removeChild(ta);
                      toast(t("settingsSystem:toasts.urlCopied"), "success");
                    }}
                    style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 2, flexShrink: 0 }}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          </div>

          {/* Post-Conversion Script */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>{t("settingsSystem:script.title")}</h3>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              {t("settingsSystem:script.intro")}
            </div>
            <div style={{ marginBottom: 12 }}>
              <div style={labelStyle}>{t("settingsSystem:script.path")}</div>
              <input type="text" style={{ ...inputStyle, width: "100%", maxWidth: 500 }}
                value={encoding?.post_conversion_script || ""}
                onChange={e => setEncoding({ ...encoding, post_conversion_script: e.target.value })}
                placeholder="/path/to/script.sh" />
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                {t("settingsSystem:script.pathHelp")}
              </div>
            </div>
            <div style={{ marginBottom: 12 }}>
              <div style={labelStyle}>{t("settingsSystem:script.timeout")}</div>
              <input type="number" style={{ ...inputStyle, width: 100 }}
                value={encoding?.post_conversion_script_timeout || 300}
                onChange={e => setEncoding({ ...encoding, post_conversion_script_timeout: parseInt(e.target.value) || 300 })} />
            </div>
            <details style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>{t("settingsSystem:script.envReference")}</summary>
              <div style={{ backgroundColor: "var(--bg-primary)", padding: 12, borderRadius: 4, fontFamily: "monospace", fontSize: 11, lineHeight: 1.8 }}>
                {[
                  "SHRINKERR_EVENT=job_completed",
                  "SHRINKERR_JOB_ID=12345",
                  "SHRINKERR_FILE_PATH=/media/.../file.x265.mkv",
                  "SHRINKERR_ORIGINAL_PATH=/media/.../file.x264.mkv",
                  "SHRINKERR_JOB_TYPE=convert|audio|combined",
                  "SHRINKERR_SPACE_SAVED=1234567890 (bytes)",
                  "SHRINKERR_ORIGINAL_SIZE=5000000000 (bytes)",
                  "SHRINKERR_ENCODER=nvenc|libx265",
                  "SHRINKERR_PRESET=p3",
                  "SHRINKERR_CQ=27",
                  "SHRINKERR_FPS=195.5",
                  "SHRINKERR_VMAF_SCORE=96.2",
                  "SHRINKERR_STATUS=completed|failed",
                  `SHRINKERR_ERROR=${t("settingsSystem:script.envErrorValue")}`,
                  // The legacy SQUEEZARR_* variants are also set for backward
                  // compatibility with scripts written before the rename.
                ].map(v => <div key={v}>{v}</div>)}
              </div>
            </details>
            <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 16px" }}
              onClick={async () => {
                await updateEncodingSettings({
                  post_conversion_script: encoding?.post_conversion_script || "",
                  post_conversion_script_timeout: encoding?.post_conversion_script_timeout || 300,
                });
                toast(t("settingsSystem:toasts.scriptSaved"), "success");
              }}>{t("common:actions.save")}</button>
          </div>

          <h2 id="system" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsSystem:system.title")}
          </h2>
          {/* Authentication */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", margin: 0, marginBottom: 12 }}>{t("settingsSystem:auth.title")}</h3>

            {/* Enable toggle — left-aligned, prominent. Pre-v0.5.6 the
                toggle sat in the section header on the right; users in
                dark mode reported missing it entirely and locking
                themselves out. */}
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 14, padding: "8px 10px", backgroundColor: "var(--bg-secondary)", border: "1px solid var(--border)", borderRadius: 4, width: "fit-content" }}>
              <input type="checkbox" checked={encoding?.auth_enabled || false}
                onChange={() => setEncoding({ ...encoding, auth_enabled: !encoding?.auth_enabled })}
                style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
              <span style={{ fontSize: 14, color: "var(--text-primary)", fontWeight: 500 }}>
                {t("settingsSystem:auth.enable")}
              </span>
              <span style={{ fontSize: 12, color: encoding?.auth_enabled ? "var(--success)" : "var(--text-muted)", marginLeft: 6 }}>
                ({encoding?.auth_enabled ? t("settingsIntegrations:shared.enabled") : t("settingsIntegrations:shared.disabled")})
              </span>
            </label>

            {/* Active-auth banner. Pre-v0.5.6 there was no visible signal
                that auth was on except the toggle, which several users
                missed in dark mode. Make the state unambiguous. */}
            {encoding?.auth_enabled && (
              <div style={{
                padding: "10px 12px",
                marginBottom: 14,
                backgroundColor: "rgba(74, 222, 128, 0.08)",
                border: "1px solid rgba(74, 222, 128, 0.35)",
                borderRadius: 4,
                fontSize: 13,
                color: "var(--text-primary)",
                lineHeight: 1.5,
              }}>
                <Trans i18nKey="settingsSystem:auth.loginRequired" components={{ b: <strong style={{ color: "var(--success)" }} /> }} />
              </div>
            )}

            {/* Credentials — always rendered. Disabled when auth is off so
                users can see what fields exist (and pre-fill them) before
                flipping the toggle. v0.5.6: previously rendered
                conditionally, which surprised users who couldn't see the
                fields until after enabling. */}
            <div style={{ marginBottom: 12, opacity: encoding?.auth_enabled ? 1 : 0.55 }}>
              <div style={labelStyle}>{t("settingsSystem:auth.username")}</div>
              <input type="text" style={{ ...inputStyle, maxWidth: 300 }}
                value={encoding?.auth_username || ""}
                disabled={!encoding?.auth_enabled}
                onChange={e => setEncoding({ ...encoding, auth_username: e.target.value })}
                placeholder="admin" />
            </div>
            <div style={{ marginBottom: 12, opacity: encoding?.auth_enabled ? 1 : 0.55 }}>
              <div style={labelStyle}>{t("settingsSystem:auth.password")}</div>
              <input type="password" style={{ ...inputStyle, maxWidth: 300 }}
                value={encoding?.auth_password || ""}
                disabled={!encoding?.auth_enabled}
                onChange={e => setEncoding({ ...encoding, auth_password: e.target.value })}
                placeholder={t("settingsSystem:auth.passwordPlaceholder")} />
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{t("settingsSystem:auth.passwordHelp")}</div>
            </div>

            {/* API Key section — masked by default, reveal or copy pulls
                 the real value via a dedicated endpoint so the bulk GET
                 response can stay masked against session-hijack / XSS
                 exfil. */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12, marginTop: 12 }}>
              <div style={labelStyle}>{t("settingsIntegrations:shared.apiKey")}</div>
              <div style={{ display: "flex", gap: 0, alignItems: "center", marginBottom: 4, maxWidth: 500 }}>
                <input type="text" readOnly
                  value={encoding?.api_key || ""}
                  style={{
                    ...inputStyle, flex: 1, fontFamily: "monospace", fontSize: 13, letterSpacing: 0.5,
                    borderRadius: "4px 0 0 4px", borderRight: "none",
                  }} />
                <button
                  title={t("settingsSystem:auth.reveal")}
                  onClick={async () => {
                    try {
                      const r = await getApiKey();
                      if (r?.api_key) {
                        setEncoding({ ...encoding, api_key: r.api_key });
                      } else {
                        toast(t("settingsSystem:toasts.noApiKey"), "error");
                      }
                    } catch (e: any) {
                      toast(t("settingsSystem:toasts.fetchKeyFailed", { error: e?.message || e }), "error");
                    }
                  }}
                  style={{
                    height: 36, width: 40, display: "flex", alignItems: "center", justifyContent: "center",
                    backgroundColor: "var(--bg-secondary)", border: "1px solid var(--border)",
                    borderLeft: "none", borderRight: "none", cursor: "pointer", color: "var(--text-muted)",
                  }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
                  </svg>
                </button>
                <button
                  title={t("settingsSystem:auth.copy")}
                  onClick={async () => {
                    // Always pull the unmasked value from the server on
                    // click — the React state may be showing the masked
                    // form (`****xxxx`) which would be useless when
                    // pasted into a worker config or integration script.
                    let text = "";
                    try {
                      const r = await getApiKey();
                      text = r?.api_key || "";
                    } catch (e: any) {
                      toast(t("settingsSystem:toasts.fetchKeyFailed", { error: e?.message || e }), "error");
                      return;
                    }
                    if (!text) {
                      toast(t("settingsSystem:toasts.noApiKey"), "error");
                      return;
                    }
                    if (navigator.clipboard?.writeText) {
                      navigator.clipboard.writeText(text).then(() => toast(t("settingsSystem:toasts.apiKeyCopied"), "success")).catch(() => {
                        const ta = document.createElement("textarea");
                        ta.value = text;
                        ta.style.position = "fixed";
                        ta.style.opacity = "0";
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand("copy");
                        document.body.removeChild(ta);
                        toast(t("settingsSystem:toasts.apiKeyCopied"), "success");
                      });
                    } else {
                      const ta = document.createElement("textarea");
                      ta.value = text;
                      ta.style.position = "fixed";
                      ta.style.opacity = "0";
                      document.body.appendChild(ta);
                      ta.select();
                      document.execCommand("copy");
                      document.body.removeChild(ta);
                      toast(t("settingsSystem:toasts.apiKeyCopied"), "success");
                    }
                  }}
                  style={{
                    height: 36, width: 40, display: "flex", alignItems: "center", justifyContent: "center",
                    backgroundColor: "var(--bg-secondary)", border: "1px solid var(--border)",
                    borderLeft: "none", borderRight: "none", cursor: "pointer", color: "var(--text-muted)",
                  }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                  </svg>
                </button>
                <button
                  title={t("settingsSystem:auth.regenerate")}
                  onClick={async () => {
                    // Server-side generation: portable across browser
                    // contexts (no `crypto.randomUUID` requirement) and
                    // atomically persists. Pre-v0.3.75 this called
                    // `crypto.randomUUID()` and only updated React state
                    // — silently failed on plain-HTTP LAN access where
                    // randomUUID is undefined, AND lost on page refresh
                    // even when it did work. v0.3.75+.
                    try {
                      const r = await regenerateApiKey();
                      if (r?.api_key) {
                        setEncoding({ ...encoding, api_key: r.api_key });
                        toast(t("settingsSystem:toasts.apiKeyRegenerated"), "success");
                      } else {
                        toast(t("settingsSystem:toasts.regenerateEmpty"), "error");
                      }
                    } catch (e: any) {
                      toast(t("settingsSystem:toasts.regenerateFailed", { error: e?.message || e }), "error");
                    }
                  }}
                  style={{
                    height: 36, width: 40, display: "flex", alignItems: "center", justifyContent: "center",
                    backgroundColor: "#e94560", border: "1px solid #e94560",
                    borderRadius: "0 4px 4px 0", cursor: "pointer", color: "white",
                  }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                  </svg>
                </button>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                {t("settingsSystem:auth.apiKeyHelp")}
              </div>
            </div>

            <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 16px", marginTop: 12 }}
              onClick={async () => {
                const data: any = { auth_enabled: encoding?.auth_enabled };
                if (encoding?.auth_username) data.auth_username = encoding.auth_username;
                if (encoding?.auth_password) data.auth_password = encoding.auth_password;
                if (encoding?.api_key !== undefined) data.api_key = encoding.api_key;
                await updateEncodingSettings(data);
                // Clear the password field after saving
                setEncoding({ ...encoding, auth_password: "" });
                toast(t("settingsSystem:toasts.authSaved"), "success");
              }}>{t("settingsSystem:auth.save")}</button>
          </div>

          {/* Notifications */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>{t("settingsSystem:notifications.title")}</h3>

            {/* Notification language — server-side setting, independent of
                the per-browser UI language above. Saved immediately. */}
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 16 }}>
              <div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>{t("settingsSystem:notifications.language")}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, maxWidth: 480 }}>
                  {t("settingsSystem:notifications.languageHelp")}
                </div>
              </div>
              <select
                value={encoding?.notification_language || "en"}
                onChange={async (e) => {
                  const value = e.target.value;
                  try {
                    await updateEncodingSettings({ notification_language: value });
                    setEncoding((prev: any) => ({ ...prev, notification_language: value }));
                    toast(t("settingsSystem:toasts.notificationLanguageSaved"), "success");
                  } catch (err: any) {
                    toast(t("settingsSystem:toasts.notificationLanguageFailed", { error: err?.message || err }), "error");
                  }
                }}
                aria-label={t("settingsSystem:notifications.language")}
                style={{ ...inputStyle, width: "auto", minWidth: 140 }}
              >
                {LANGUAGES.map((l) => (
                  <option key={l.code} value={l.code}>{l.label}</option>
                ))}
              </select>
            </div>

            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
              {t("settingsSystem:notifications.intro")}
            </div>

            {/* Event toggles */}
            <div style={{ display: "flex", gap: 24, marginBottom: 16, flexWrap: "wrap" }}>
              {[
                ["notify_queue_complete", t("settingsSystem:notifications.events.queueComplete")],
                ["notify_job_failed", t("settingsSystem:notifications.events.jobFailed")],
                ["notify_disk_low", t("settingsSystem:notifications.events.diskLow")],
              ].map(([key, label]) => (
                <label key={key} style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13 }}>
                  <input type="checkbox" checked={encoding?.[key] ?? false}
                    onChange={e => setEncoding({ ...encoding, [key]: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={{ color: "var(--text-secondary)" }}>{label}</span>
                </label>
              ))}
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <label style={{ ...labelStyle, margin: 0 }}>{t("settingsSystem:notifications.diskThreshold")}</label>
                <input type="number" style={{ ...inputStyle, width: 70 }}
                  value={encoding?.disk_space_threshold_gb || "50"}
                  onChange={e => setEncoding({ ...encoding, disk_space_threshold_gb: e.target.value })} />
              </div>
            </div>

            {/* Provider configs */}
            <div className="notification-providers" style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 16, marginBottom: 16 }}>
              {/* Discord */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>Discord</div>
                <label style={labelStyle}>{t("settingsSystem:notifications.webhookUrl")}</label>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="https://discord.com/api/webhooks/..."
                  value={encoding?.discord_webhook_url || ""}
                  onChange={e => setEncoding({ ...encoding, discord_webhook_url: e.target.value })} />
              </div>

              {/* Telegram */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>Telegram</div>
                <div style={{ display: "flex", gap: 8 }}>
                  <div style={{ flex: 1 }}>
                    <label style={labelStyle}>{t("settingsSystem:notifications.botToken")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="123456:ABC-DEF..."
                      value={encoding?.telegram_bot_token || ""}
                      onChange={e => setEncoding({ ...encoding, telegram_bot_token: e.target.value })} />
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={labelStyle}>{t("settingsSystem:notifications.chatId")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="-100123456789"
                      value={encoding?.telegram_chat_id || ""}
                      onChange={e => setEncoding({ ...encoding, telegram_chat_id: e.target.value })} />
                  </div>
                </div>
              </div>

              {/* Email */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>{t("settingsSystem:notifications.email")}</div>
                <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 8, marginBottom: 8 }}>
                  <div>
                    <label style={labelStyle}>{t("settingsSystem:notifications.smtpHost")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="smtp.gmail.com"
                      value={encoding?.smtp_host || ""}
                      onChange={e => setEncoding({ ...encoding, smtp_host: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>{t("settingsSystem:notifications.port")}</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="587"
                      value={encoding?.smtp_port || "587"}
                      onChange={e => setEncoding({ ...encoding, smtp_port: e.target.value })} />
                  </div>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 8 }}>
                  <div>
                    <label style={labelStyle}>{t("settingsSystem:notifications.username")}</label>
                    <input style={{ ...inputStyle, width: "100%" }}
                      value={encoding?.smtp_user || ""}
                      onChange={e => setEncoding({ ...encoding, smtp_user: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>{t("settingsSystem:notifications.password")}</label>
                    <input type="password" style={{ ...inputStyle, width: "100%" }}
                      value={encoding?.smtp_pass || ""}
                      onChange={e => setEncoding({ ...encoding, smtp_pass: e.target.value })} />
                  </div>
                </div>
                <label style={labelStyle}>{t("settingsSystem:notifications.sendTo")}</label>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="you@email.com"
                  value={encoding?.email_to || ""}
                  onChange={e => setEncoding({ ...encoding, email_to: e.target.value })} />
              </div>

              {/* Generic Webhook */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>{t("settingsSystem:notifications.genericWebhook")}</div>
                <label style={labelStyle}>{t("settingsSystem:notifications.genericWebhookUrl")}</label>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="https://your-server.com/webhook"
                  value={encoding?.webhook_url || ""}
                  onChange={e => setEncoding({ ...encoding, webhook_url: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  {t("settingsSystem:notifications.payload")} {"{ event, title, message, fields }"}
                </div>
              </div>
            </div>

            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  await updateEncodingSettings(encoding);
                  toast(t("settingsSystem:toasts.notificationsSaved"), "success");
                }}
              >{t("settingsSystem:notifications.save")}</button>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  await updateEncodingSettings(encoding);
                  const res = await testNotifications();
                  const results = res.results || {};
                  const ok = Object.entries(results).filter(([, v]) => v).map(([k]) => k);
                  const fail = Object.entries(results).filter(([, v]) => !v).map(([k]) => k);
                  if (ok.length > 0) toast(t("settingsSystem:toasts.testSent", { providers: ok.join(", ") }), "success");
                  if (fail.length > 0) toast(t("settingsSystem:toasts.testFailed", { providers: fail.join(", ") }));
                  if (ok.length === 0 && fail.length === 0) toast(t("settingsSystem:toasts.noProviders"));
                }}
              >{t("settingsSystem:notifications.test")}</button>
            </div>
          </div>

          {/* Backups */}
          <div style={sectionStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ color: "white", margin: 0 }}>{t("settingsSystem:backups.title")}</h3>
              <button className="btn btn-primary" style={{ fontSize: 11, padding: "4px 12px" }}
                disabled={backupCreating}
                onClick={async () => {
                  setBackupCreating(true);
                  try {
                    await createBackup();
                    toast(t("settingsSystem:toasts.backupCreated"), "success");
                    loadBackups();
                  } catch { toast(t("settingsSystem:toasts.backupFailed")); }
                  setBackupCreating(false);
                }}>
                {backupCreating ? t("settingsSystem:backups.creating") : t("settingsSystem:backups.create")}
              </button>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              {t("settingsSystem:backups.intro")}
            </div>
            {backupList.length === 0 ? (
              <div style={{ textAlign: "center", padding: 20, color: "var(--text-muted)", fontSize: 12, opacity: 0.6 }}>
                {t("settingsSystem:backups.empty")}
              </div>
            ) : (
              <div style={{ borderRadius: 4, overflow: "hidden" }}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 100px 120px 60px", gap: 0, padding: "6px 12px", fontSize: 11, fontWeight: 600, color: "var(--text-muted)", borderBottom: "1px solid var(--border)" }}>
                  <span>{t("settingsSystem:backups.columns.name")}</span><span>{t("settingsSystem:backups.columns.size")}</span><span>{t("settingsSystem:backups.columns.time")}</span><span></span>
                </div>
                {backupList.map(b => (
                  <div key={b.name} style={{ display: "grid", gridTemplateColumns: "1fr 100px 120px 60px", gap: 0, padding: "8px 12px", fontSize: 12, borderBottom: "1px solid var(--bg-primary)", alignItems: "center" }}>
                    <a href={downloadBackupUrl(b.name)} download style={{ color: "var(--accent)", textDecoration: "none", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {b.name}
                    </a>
                    <span style={{ color: "var(--text-muted)" }}>{(b.size / (1024 * 1024)).toFixed(1)} MiB</span>
                    <span style={{ color: "var(--text-muted)" }}>
                      {new Date(b.created_at).toLocaleDateString(i18n.language, { day: "2-digit", month: "short", year: "numeric" })}
                    </span>
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <button title={t("settingsSystem:backups.restore")} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 2, display: "inline-flex", alignItems: "center" }}
                        onClick={async () => {
                          if (!confirm(t("settingsSystem:backups.restoreConfirm", { name: b.name }))) return;
                          try {
                            const resp = await fetch(downloadBackupUrl(b.name));
                            const blob = await resp.blob();
                            const file = new File([blob], b.name, { type: "application/zip" });
                            await restoreBackup(file);
                            toast(t("settingsSystem:toasts.backupRestored"), "success");
                          } catch { toast(t("settingsSystem:toasts.restoreFailed")); }
                        }}>
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 105.64-11.36L1 10"/>
                        </svg>
                      </button>
                      <button title={t("common:actions.delete")} style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", padding: 2, display: "inline-flex", alignItems: "center", opacity: 0.6 }}
                        onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                        onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.6")}
                        onClick={async () => {
                          if (!confirm(t("settingsSystem:backups.deleteConfirm", { name: b.name }))) return;
                          try {
                            await deleteBackup(b.name);
                            loadBackups();
                            toast(t("settingsSystem:toasts.backupDeleted"));
                          } catch { toast(t("settingsSystem:toasts.deleteFailed")); }
                        }}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
                        </svg>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ marginTop: 12 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12, color: "var(--text-muted)" }}>
                <input type="file" accept=".zip" style={{ display: "none" }}
                  onChange={async (e) => {
                    const file = e.target.files?.[0];
                    if (!file) return;
                    if (!confirm(t("settingsSystem:backups.restoreUploadConfirm", { name: file.name }))) { e.target.value = ""; return; }
                    try {
                      await restoreBackup(file);
                      toast(t("settingsSystem:toasts.backupRestored"), "success");
                      loadBackups();
                    } catch { toast(t("settingsSystem:toasts.restoreFailed")); }
                    e.target.value = "";
                  }} />
                <span style={{ border: "1px solid var(--border)", padding: "4px 10px", borderRadius: 4, cursor: "pointer" }}>
                  {t("settingsSystem:backups.restoreFromFile")}
                </span>
              </label>
            </div>
          </div>

          {/* User Interface */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>{t("settings:ui.title")}</h3>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
              <div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>{t("common:language.label")}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, maxWidth: 480 }}>
                  {t("common:language.help")}
                </div>
              </div>
              <select
                value={(LANGUAGES.find((l) => i18n.language?.startsWith(l.code))?.code) ?? "en"}
                onChange={(e) => setLanguage(e.target.value as LanguageCode)}
                aria-label={t("common:language.label")}
                style={{ ...inputStyle, width: "auto", minWidth: 140 }}
              >
                {LANGUAGES.map((l) => (
                  <option key={l.code} value={l.code}>{l.label}</option>
                ))}
              </select>
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>{t("settings:ui.theme")}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                  {t("settings:ui.themeHelp")}
                </div>
              </div>
              <button
                className="sort-pill"
                onClick={onToggleTheme}
                style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
              >
                {theme === "dark" ? (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
                ) : (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
                )}
                {theme === "dark" ? t("settings:ui.lightMode") : t("settings:ui.darkMode")}
              </button>
            </div>
          </div>

          {/* Keyboard Shortcuts */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>{t("settingsSystem:shortcuts.title")}</h3>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {[
                ["D", t("settingsSystem:shortcuts.actions.dashboard")],
                ["S", t("settingsSystem:shortcuts.actions.scanner")],
                ["Q", t("settingsSystem:shortcuts.actions.queue")],
                ["T", t("settingsSystem:shortcuts.actions.statistics")],
                ["L", t("settingsSystem:shortcuts.actions.logs")],
                ["H", t("settingsSystem:shortcuts.actions.schedule")],
                ["E", t("settingsSystem:shortcuts.actions.settings")],
                ["Space", t("settingsSystem:shortcuts.actions.startPause")],
              ].map(([key, action]) => (
                <div key={key} style={{ display: "flex", alignItems: "center", gap: 10, padding: "4px 0" }}>
                  <kbd style={{
                    background: "var(--bg-primary)", border: "1px solid var(--border)",
                    borderRadius: 4, padding: "2px 8px", fontSize: 12, fontFamily: "var(--font-mono)",
                    color: "var(--accent)", minWidth: 36, textAlign: "center", fontWeight: 600,
                  }}>{key === "Space" ? t("settingsSystem:shortcuts.space") : key}</kbd>
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{action}</span>
                </div>
              ))}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 10, opacity: 0.6 }}>
              {t("settingsSystem:shortcuts.note")}
            </div>
          </div>

          {/* ── Updates ───────────────────────────────────────────────── */}
          <h2 id="updates" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsSystem:updates.title")}
          </h2>

          {/* Version summary card */}
          <div style={{ ...sectionStyle, display: "flex", flexWrap: "wrap", alignItems: "center", gap: 18, justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
              <img src="/favicon.svg" alt="" width="28" height="28" />
              <div>
                <div style={{ fontSize: 13, color: "var(--text-muted)" }}>{t("settingsSystem:updates.runningVersion")}</div>
                <div style={{ fontSize: 22, fontWeight: 700, color: "var(--text-primary)", marginTop: 2 }}>
                  v{versionInfo?.current ?? "…"}
                </div>
                {versionInfo?.update_available && versionInfo.latest ? (
                  <div style={{ fontSize: 12, color: "var(--accent)", marginTop: 4 }}>
                    <Trans i18nKey="settingsSystem:updates.availableUpstream" values={{ version: versionInfo.latest }} components={{ b: <strong /> }} />
                  </div>
                ) : versionInfo?.latest ? (
                  <div style={{ fontSize: 12, color: "var(--success)", marginTop: 4 }}>
                    {t("settingsSystem:updates.latest")}
                  </div>
                ) : (
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>
                    {t("settingsSystem:updates.checkFailed")}
                  </div>
                )}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 12, padding: "7px 14px" }}
                disabled={updateCheckLoading}
                onClick={async () => {
                  setUpdateCheckLoading(true);
                  try {
                    // force=true bypasses the 30-min server cache so the
                    // user's click always produces a fresh GitHub check.
                    const v = await getVersion(true);
                    setVersionInfo(v);
                    if (v.update_available) {
                      toast(t("settingsSystem:toasts.updateAvailable", { version: v.latest }), "success");
                    } else {
                      toast(t("settingsSystem:toasts.upToDate"), "success");
                    }
                  } catch {
                    toast(t("settingsSystem:toasts.updateCheckFailed"), "error");
                  } finally {
                    setUpdateCheckLoading(false);
                  }
                }}
              >
                {updateCheckLoading ? t("settingsSystem:updates.checking") : t("settingsSystem:updates.check")}
              </button>
              {versionInfo?.update_available && (
                <button
                  className="btn btn-primary"
                  style={{ fontSize: 12, padding: "7px 14px" }}
                  onClick={() => setChangelogModalOpen(true)}
                >
                  {t("settingsSystem:updates.viewNotes", { version: versionInfo.latest })}
                </button>
              )}
            </div>
          </div>

          {/* Recent releases — the 4 most recent changelog entries inline.
              "View full history" link sits at the bottom to reach the
              full CHANGELOG.md on GitHub. */}
          <div style={{ marginTop: 10 }}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 10, padding: "0 2px" }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                {t("settingsSystem:updates.recent")}
              </div>
              <a
                href="https://github.com/I-IAL9000/shrinkerr/blob/main/CHANGELOG.md"
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: 11, color: "var(--text-muted)", textDecoration: "none" }}
              >
                {t("settingsSystem:updates.fullHistory")}
              </a>
            </div>
            {changelogEntries === null && (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 24 }}>
                <div className="spinner" style={{ width: 16, height: 16 }} />
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("settingsSystem:updates.loadingChangelog")}</span>
              </div>
            )}
            {changelogEntries && changelogEntries.length === 0 && (
              <div style={{ padding: 20, fontSize: 12, color: "var(--text-muted)", textAlign: "center", background: "var(--bg-card)", borderRadius: 6 }}>
                {t("settingsSystem:updates.noEntries")}
              </div>
            )}
            {changelogEntries && changelogEntries.map((e, i) => (
              // Highlight the card that matches the currently-running
              // version so users can orient themselves at a glance.
              <ChangelogEntryView
                key={e.version}
                entry={e}
                highlight={i === 0 && (versionInfo?.current === e.version || !versionInfo)}
              />
            ))}
          </div>

          {/* Full-history modal reused from the sidebar; the "View release
              notes" button above triggers it. */}
          <ChangelogModal
            open={changelogModalOpen}
            onClose={() => setChangelogModalOpen(false)}
            latestVersion={versionInfo?.latest ?? null}
            showLatestOnly
          />

          {/* ── Support ───────────────────────────────────────────────── */}
          <h2 id="support" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            {t("settingsSystem:support.title")}
          </h2>

          <div style={sectionStyle}>
            <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 14 }}>
              {t("settingsSystem:support.intro")}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {[
                {
                  href: "https://github.com/I-IAL9000/shrinkerr/tree/main/docs",
                  title: t("settingsSystem:support.docs.title"),
                  desc: t("settingsSystem:support.docs.desc"),
                  icon: (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                    </svg>
                  ),
                },
                {
                  href: "https://github.com/I-IAL9000/shrinkerr",
                  title: t("settingsSystem:support.repo.title"),
                  desc: t("settingsSystem:support.repo.desc"),
                  icon: (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                      <path d="M12 0.296C5.37 0.296 0 5.666 0 12.296c0 5.302 3.438 9.8 8.205 11.387 0.6 0.111 0.819-0.26 0.819-0.578 0-0.285-0.01-1.04-0.015-2.04-3.338 0.725-4.042-1.61-4.042-1.61-0.546-1.385-1.333-1.754-1.333-1.754-1.089-0.744 0.083-0.729 0.083-0.729 1.205 0.085 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492 0.997 0.108-0.775 0.418-1.305 0.762-1.605-2.665-0.302-5.466-1.332-5.466-5.93 0-1.31 0.469-2.381 1.236-3.221-0.124-0.303-0.535-1.524 0.117-3.176 0 0 1.008-0.322 3.3 1.23 0.957-0.266 1.983-0.399 3.003-0.404 1.02 0.005 2.047 0.138 3.006 0.404 2.29-1.552 3.296-1.23 3.296-1.23 0.653 1.653 0.242 2.874 0.118 3.176 0.77 0.84 1.235 1.911 1.235 3.221 0 4.609-2.805 5.624-5.478 5.921 0.43 0.371 0.814 1.103 0.814 2.222 0 1.604-0.014 2.898-0.014 3.292 0 0.321 0.217 0.696 0.826 0.578C20.565 22.092 24 17.596 24 12.296 24 5.666 18.628 0.296 12 0.296z"/>
                    </svg>
                  ),
                },
                {
                  href: "https://github.com/I-IAL9000/shrinkerr/issues/new",
                  title: t("settingsSystem:support.issue.title"),
                  desc: t("settingsSystem:support.issue.desc"),
                  icon: (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <circle cx="12" cy="12" r="10"/>
                      <line x1="12" y1="8" x2="12" y2="12"/>
                      <line x1="12" y1="16" x2="12.01" y2="16"/>
                    </svg>
                  ),
                },
              ].map(link => (
                <a
                  key={link.href}
                  href={link.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    display: "flex", alignItems: "flex-start", gap: 12,
                    padding: "12px 14px", borderRadius: 6,
                    background: "var(--bg-primary)", border: "1px solid var(--border)",
                    textDecoration: "none", color: "var(--text-secondary)",
                    transition: "border-color 0.15s, background 0.15s",
                  }}
                  onMouseOver={(e) => {
                    (e.currentTarget as HTMLAnchorElement).style.borderColor = "var(--accent)";
                  }}
                  onMouseOut={(e) => {
                    (e.currentTarget as HTMLAnchorElement).style.borderColor = "var(--border)";
                  }}
                >
                  <div style={{ flexShrink: 0, color: "var(--accent)", marginTop: 2 }}>{link.icon}</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                      {link.title}
                      <span style={{ marginLeft: 8, fontSize: 11, color: "var(--text-muted)", fontWeight: 400 }}>
                        ↗
                      </span>
                    </div>
                    <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>{link.desc}</div>
                  </div>
                </a>
              ))}
            </div>

            {/* TMDB attribution — required by TMDB's non-commercial API
                terms of use. Always rendered so the attribution doesn't
                depend on whether the user has their own key vs. the
                bundled one. */}
            <div style={{
              marginTop: 16, paddingTop: 14,
              borderTop: "1px solid var(--border)",
              fontSize: 11, color: "var(--text-muted)",
              display: "flex", alignItems: "center", gap: 8,
            }}>
              <span>
                {t("settingsSystem:support.tmdb")}
              </span>
              <a
                href="https://www.themoviedb.org/"
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: "var(--accent)", textDecoration: "none" }}
              >
                themoviedb.org ↗
              </a>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
