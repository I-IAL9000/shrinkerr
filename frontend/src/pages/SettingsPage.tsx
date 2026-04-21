import { useState, useEffect, useRef } from "react";
import { useRangeFill } from "../useRangeFill";
import FolderBrowser from "../components/FolderBrowser";
import RenamingSettings from "../components/RenamingSettings";
import {
  getMediaDirs, addMediaDir, removeMediaDir,
  getEncodingSettings, updateEncodingSettings, testApiKey,
  createEncodingRule, updateEncodingRule, deleteEncodingRule,
  reorderEncodingRules, syncPlexRuleMetadata, getPlexOptions,
  getConditionOptions, testNotifications, importSettings,
  listBackups, createBackup, deleteBackup, downloadBackupUrl, restoreBackup,
  plexAuthStart, plexAuthCheck, plexAuthResources, plexAuthSave,
  plexAuthDisconnect, plexAuthStatus,
  type PlexAuthStatus, type PlexServer,
} from "../api";
import { useToast } from "../useToast";

const PRESET_INFO: Record<string, { label: string; desc: string }> = {
  p1: { label: "Fastest", desc: "Lowest quality, highest speed. Good for quick tests." },
  p2: { label: "Very Fast", desc: "Slightly better quality than p1, still very quick." },
  p3: { label: "Fast", desc: "Reasonable quality with good speed. Good for batch processing." },
  p4: { label: "Medium", desc: "Balanced quality and speed. Good default for general use." },
  p5: { label: "Slow", desc: "Better compression efficiency. Noticeably slower." },
  p6: { label: "Very Slow", desc: "High quality with good compression. Recommended for storage." },
  p7: { label: "Slowest", desc: "Best quality NVENC offers. Maximum compression, slowest speed." },
};

const TARGET_CODECS = [
  { value: "hevc", label: "HEVC / H.265", desc: "Modern codec, excellent compression. Widely supported." },
  { value: "av1", label: "AV1 (future)", desc: "Next-gen codec, best compression. Requires AV1-capable GPU." },
];

const SOURCE_CODECS = [
  { value: "h264", label: "H.264 / AVC / x264", always: false, defaultOn: true },
  { value: "mpeg2", label: "MPEG-2", always: false, defaultOn: true },
  { value: "mpeg4", label: "MPEG-4 Part 2 / XviD / DivX", always: false, defaultOn: true },
  { value: "vc1", label: "VC-1 (WMV)", always: false, defaultOn: true },
  { value: "msmpeg4v3", label: "MS-MPEG4v3 (old DivX/AVI)", always: false, defaultOn: false },
  { value: "vp9", label: "VP9 (YouTube/WebM)", always: false, defaultOn: false },
  { value: "hevc", label: "H.265 / HEVC / x265", always: false, defaultOn: false },
  { value: "av1", label: "AV1", always: false, defaultOn: false },
];

const ALL_LANGUAGES = [
  { code: "eng", name: "English" }, { code: "isl", name: "Icelandic" }, { code: "ice", name: "Icelandic (alt)" },
  { code: "aar", name: "Afar" }, { code: "afr", name: "Afrikaans" }, { code: "aka", name: "Akan" },
  { code: "amh", name: "Amharic" }, { code: "ara", name: "Arabic" }, { code: "arg", name: "Aragonese" },
  { code: "asm", name: "Assamese" }, { code: "aze", name: "Azerbaijani" }, { code: "bak", name: "Bashkir" },
  { code: "bam", name: "Bambara" }, { code: "bel", name: "Belarusian" }, { code: "ben", name: "Bengali" },
  { code: "bos", name: "Bosnian" }, { code: "bre", name: "Breton" }, { code: "bul", name: "Bulgarian" },
  { code: "cat", name: "Catalan" }, { code: "ces", name: "Czech" }, { code: "cze", name: "Czech (alt)" },
  { code: "chi", name: "Chinese" }, { code: "zho", name: "Chinese (alt)" }, { code: "cmn", name: "Mandarin" },
  { code: "cor", name: "Cornish" }, { code: "cos", name: "Corsican" }, { code: "cre", name: "Cree" },
  { code: "cym", name: "Welsh" }, { code: "dan", name: "Danish" }, { code: "deu", name: "German" },
  { code: "ger", name: "German (alt)" }, { code: "div", name: "Divehi" }, { code: "dut", name: "Dutch (alt)" },
  { code: "nld", name: "Dutch" }, { code: "dzo", name: "Dzongkha" }, { code: "ell", name: "Greek" },
  { code: "gre", name: "Greek (alt)" }, { code: "epo", name: "Esperanto" }, { code: "est", name: "Estonian" },
  { code: "eus", name: "Basque" }, { code: "ewe", name: "Ewe" }, { code: "fao", name: "Faroese" },
  { code: "fas", name: "Persian" }, { code: "per", name: "Persian (alt)" }, { code: "fij", name: "Fijian" },
  { code: "fin", name: "Finnish" }, { code: "fra", name: "French" }, { code: "fre", name: "French (alt)" },
  { code: "fry", name: "Western Frisian" }, { code: "ful", name: "Fulah" }, { code: "gla", name: "Scottish Gaelic" },
  { code: "gle", name: "Irish" }, { code: "glg", name: "Galician" }, { code: "grn", name: "Guarani" },
  { code: "guj", name: "Gujarati" }, { code: "hat", name: "Haitian Creole" }, { code: "hau", name: "Hausa" },
  { code: "heb", name: "Hebrew" }, { code: "her", name: "Herero" }, { code: "hin", name: "Hindi" },
  { code: "hrv", name: "Croatian" }, { code: "hun", name: "Hungarian" }, { code: "hye", name: "Armenian" },
  { code: "arm", name: "Armenian (alt)" }, { code: "ibo", name: "Igbo" }, { code: "ido", name: "Ido" },
  { code: "ind", name: "Indonesian" }, { code: "ita", name: "Italian" }, { code: "jav", name: "Javanese" },
  { code: "jpn", name: "Japanese" }, { code: "kal", name: "Kalaallisut" }, { code: "kan", name: "Kannada" },
  { code: "kas", name: "Kashmiri" }, { code: "kat", name: "Georgian" }, { code: "geo", name: "Georgian (alt)" },
  { code: "kaz", name: "Kazakh" }, { code: "khm", name: "Khmer" }, { code: "kin", name: "Kinyarwanda" },
  { code: "kir", name: "Kirghiz" }, { code: "kor", name: "Korean" }, { code: "kur", name: "Kurdish" },
  { code: "lao", name: "Lao" }, { code: "lat", name: "Latin" }, { code: "lav", name: "Latvian" },
  { code: "lit", name: "Lithuanian" }, { code: "ltz", name: "Luxembourgish" }, { code: "mac", name: "Macedonian (alt)" },
  { code: "mkd", name: "Macedonian" }, { code: "mal", name: "Malayalam" }, { code: "mar", name: "Marathi" },
  { code: "may", name: "Malay (alt)" }, { code: "msa", name: "Malay" }, { code: "mlg", name: "Malagasy" },
  { code: "mlt", name: "Maltese" }, { code: "mon", name: "Mongolian" }, { code: "mri", name: "Maori" },
  { code: "mya", name: "Myanmar" }, { code: "bur", name: "Myanmar (alt)" }, { code: "nep", name: "Nepali" },
  { code: "nob", name: "Norwegian Bokmål" }, { code: "nor", name: "Norwegian" }, { code: "nno", name: "Norwegian Nynorsk" },
  { code: "oci", name: "Occitan" }, { code: "ori", name: "Oriya" }, { code: "orm", name: "Oromo" },
  { code: "pan", name: "Panjabi" }, { code: "pol", name: "Polish" }, { code: "por", name: "Portuguese" },
  { code: "pus", name: "Pashto" }, { code: "que", name: "Quechua" }, { code: "roh", name: "Romansh" },
  { code: "ron", name: "Romanian" }, { code: "rum", name: "Romanian (alt)" }, { code: "run", name: "Rundi" },
  { code: "rus", name: "Russian" }, { code: "sag", name: "Sango" }, { code: "san", name: "Sanskrit" },
  { code: "sin", name: "Sinhala" }, { code: "slk", name: "Slovak" }, { code: "slo", name: "Slovak (alt)" },
  { code: "slv", name: "Slovenian" }, { code: "sme", name: "Northern Sami" }, { code: "smo", name: "Samoan" },
  { code: "sna", name: "Shona" }, { code: "snd", name: "Sindhi" }, { code: "som", name: "Somali" },
  { code: "sot", name: "Southern Sotho" }, { code: "spa", name: "Spanish" }, { code: "sqi", name: "Albanian" },
  { code: "alb", name: "Albanian (alt)" }, { code: "srp", name: "Serbian" }, { code: "ssw", name: "Swati" },
  { code: "sun", name: "Sundanese" }, { code: "swa", name: "Swahili" }, { code: "swe", name: "Swedish" },
  { code: "tam", name: "Tamil" }, { code: "tat", name: "Tatar" }, { code: "tel", name: "Telugu" },
  { code: "tgk", name: "Tajik" }, { code: "tgl", name: "Tagalog" }, { code: "tha", name: "Thai" },
  { code: "tib", name: "Tibetan (alt)" }, { code: "bod", name: "Tibetan" }, { code: "tir", name: "Tigrinya" },
  { code: "ton", name: "Tonga" }, { code: "tsn", name: "Tswana" }, { code: "tso", name: "Tsonga" },
  { code: "tuk", name: "Turkmen" }, { code: "tur", name: "Turkish" }, { code: "twi", name: "Twi" },
  { code: "uig", name: "Uighur" }, { code: "ukr", name: "Ukrainian" }, { code: "urd", name: "Urdu" },
  { code: "uzb", name: "Uzbek" }, { code: "vie", name: "Vietnamese" }, { code: "vol", name: "Volapük" },
  { code: "wln", name: "Walloon" }, { code: "wol", name: "Wolof" }, { code: "xho", name: "Xhosa" },
  { code: "yid", name: "Yiddish" }, { code: "yor", name: "Yoruba" }, { code: "zul", name: "Zulu" },
];

const AUDIO_CODECS = [
  { value: "copy", label: "Copy (no re-encode)", desc: "Keep original audio codec. Fastest, no quality loss." },
  { value: "aac", label: "AAC", desc: "Widely compatible. Good quality at lower bitrates." },
  { value: "ac3", label: "AC3 (Dolby Digital)", desc: "Standard surround sound. Max 640 kbps." },
  { value: "eac3", label: "EAC3 (Dolby Digital+)", desc: "Enhanced AC3. Better quality at same bitrate." },
  { value: "opus", label: "Opus", desc: "Best quality per bitrate. Limited device support." },
  { value: "flac", label: "FLAC", desc: "Lossless compression. Large files, perfect quality." },
];

const RESOLUTION_OPTIONS = [
  { value: "copy", label: "Copy (keep original)", desc: "Keep the original resolution. No scaling applied." },
  { value: "1080p", label: "1080p (1920×1080)", desc: "Full HD. Good balance of quality and size for most content." },
  { value: "720p", label: "720p (1280×720)", desc: "HD. Significant size reduction, still good on smaller screens." },
  { value: "480p", label: "480p (854×480)", desc: "SD. Very small files. Best for mobile or low-bandwidth." },
];

const inputStyle: React.CSSProperties = {
  backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
  border: "1px solid var(--border)", padding: "8px 10px", borderRadius: 4, fontSize: 13,
  height: 36, boxSizing: "border-box",
};

const labelStyle = { color: "var(--text-muted)", fontSize: 13, marginBottom: 6 };
const helpStyle = { fontSize: 12, color: "var(--text-muted)", marginTop: 4, paddingLeft: 0 };
const sectionStyle = { background: "var(--bg-card)", padding: 20, borderRadius: 6, marginBottom: 12 };

export default function SettingsPage({ theme, onToggleTheme }: { theme: string; onToggleTheme: () => void }) {
  const toast = useToast();
  const pageRef = useRef<HTMLDivElement>(null);
  // Paint slider fills inside this page only. See useRangeFill.ts for why
  // this replaced the old document-body MutationObserver.
  useRangeFill(pageRef);

  const [dirs, setDirs] = useState<any[]>([]);
  const [newPath, setNewPath] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [encoding, setEncoding] = useState<any>(null);
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

  const CONDITION_TYPES: Record<string, { label: string; group: string; operators: { value: string; label: string }[]; valueType: "select" | "text" | "number" }> = {
    directory: { label: "Media Directory", group: "Path", operators: [{ value: "is", label: "is" }], valueType: "select" },
    source: { label: "Source", group: "File", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    resolution: { label: "Resolution", group: "File", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    video_codec: { label: "Video Codec", group: "File", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    audio_codec: { label: "Audio Codec", group: "File", operators: [{ value: "contains", label: "contains" }, { value: "does_not_contain", label: "does not contain" }], valueType: "select" },
    file_size: { label: "File Size (GB)", group: "File", operators: [{ value: "greater_than", label: "greater than" }, { value: "less_than", label: "less than" }], valueType: "number" },
    media_type: { label: "Type", group: "File", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    title: { label: "Title", group: "File", operators: [{ value: "contains", label: "contains" }, { value: "does_not_contain", label: "does not contain" }], valueType: "text" },
    release_group: { label: "Release Group", group: "File", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    label: { label: "Plex Label", group: "Plex", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    collection: { label: "Plex Collection", group: "Plex", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    genre: { label: "Plex Genre", group: "Plex", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    library: { label: "Plex Library", group: "Plex", operators: [{ value: "is", label: "is" }], valueType: "select" },
    arr_tag: { label: "Sonarr/Radarr Tag", group: "Arr", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    jellyfin_tag: { label: "Jellyfin Tag", group: "Jellyfin", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
    nzbget_category: { label: "Download Category", group: "Downloads", operators: [{ value: "is", label: "is" }, { value: "is_not", label: "is not" }], valueType: "select" },
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
        setPlexPickerError("Popup blocked. Please allow popups for this site and try again.");
        return;
      }
      setPlexAuthState("waiting");

      // Poll every 2s for up to 5 minutes.
      const deadline = Date.now() + 5 * 60 * 1000;
      const poll = async () => {
        if (Date.now() > deadline) {
          setPlexAuthState("idle");
          setPlexPickerError("Timed out waiting for Plex sign-in. Please try again.");
          try { popup.close(); } catch {}
          return;
        }
        try {
          const res = await plexAuthCheck(pin_id);
          if (res.expired) {
            setPlexAuthState("idle");
            setPlexPickerError("Plex sign-in PIN expired. Please try again.");
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
              setPlexPickerError(`Couldn't list your Plex servers: ${e?.message || e}`);
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
    await addMediaDir(newPath, newLabel);
    setNewPath("");
    setNewLabel("");
    loadDirs();
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

  const filteredLangs = langSearch.length > 0
    ? ALL_LANGUAGES.filter(l =>
        (l.name.toLowerCase().includes(langSearch.toLowerCase()) ||
         l.code.toLowerCase().includes(langSearch.toLowerCase())) &&
        !keepLangs.includes(l.code)
      ).slice(0, 8)
    : [];

  const subKeepLangs: string[] = encoding?.sub_keep_languages || [];
  const subFilteredLangs = subLangSearch.length > 0
    ? ALL_LANGUAGES.filter(l =>
        (l.name.toLowerCase().includes(subLangSearch.toLowerCase()) ||
         l.code.toLowerCase().includes(subLangSearch.toLowerCase())) &&
        !subKeepLangs.includes(l.code)
      ).slice(0, 8)
    : [];


  return (
    <div className="settings-page" ref={pageRef}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <h2 style={{ color: "white", fontSize: 20 }}>Settings</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <a href="/api/settings/export" download style={{ textDecoration: "none" }}>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}>Export</button>
          </a>
          <label className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px", cursor: "pointer" }}>
            Import
            <input type="file" accept=".json" style={{ display: "none" }}
              onChange={async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                try {
                  const text = await file.text();
                  const data = JSON.parse(text);
                  const res = await importSettings(data);
                  toast(`Imported ${res.settings_count} settings, ${res.dirs_count} dirs, ${res.rules_count} rules`, "success");
                  window.location.reload();
                } catch (err: any) {
                  toast(`Import failed: ${err.message}`);
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
          Settings saved!
        </div>
      )}


      <h2 id="directories" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 0, marginBottom: 12, scrollMarginTop: 20 }}>
        Media Directories
      </h2>
      {/* Media Directories */}
      <div style={sectionStyle}>
        <div style={{
          background: "var(--bg-primary)", borderRadius: 4, padding: 8,
          fontSize: 13, marginBottom: 8,
        }}>
          {dirs.map((d: any) => (
            <div key={d.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 0" }}>
              <span>
                {d.path}
                {d.label && <span style={{ fontSize: 10, fontFamily: "inherit", color: "var(--text-muted)", marginLeft: 8, padding: "2px 6px", borderRadius: 3, backgroundColor: "var(--border)" }}>{d.label}</span>}
              </span>
              <button onClick={() => handleRemoveDir(d.id)}
                style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>&times;</button>
            </div>
          ))}
          {dirs.length === 0 && <div style={{ opacity: 0.5 }}>No directories configured</div>}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn btn-secondary" onClick={() => setBrowserOpen(true)}
            style={{ padding: "8px 12px", fontSize: 12, whiteSpace: "nowrap", height: 36 }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: -2, marginRight: 4 }}>
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            </svg>
            Browse
          </button>
          <input placeholder="Path (e.g., /media/Movies/HD 2020)" value={newPath} onChange={(e) => setNewPath(e.target.value)}
            style={{ ...inputStyle, flex: "1 1 200px", minWidth: 150 }} />
          <select value={newLabel} onChange={(e) => setNewLabel(e.target.value)}
            style={{ ...inputStyle, width: 140 }}>
            <option value="">Type (optional)</option>
            <option value="Movies">Movies</option>
            <option value="TV Shows">TV Shows</option>
            <option value="Other">Other</option>
          </select>
          <button className="btn btn-secondary" onClick={handleAddDir} style={{ height: 36, whiteSpace: "nowrap" }}>+ Add</button>
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
            Video Settings
          </h2>
          {/* Encoding Defaults */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 16 }}>Encoding Defaults</h3>
            <div style={{ display: "flex", gap: 24, alignItems: "flex-start", flexWrap: "wrap" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 20, flex: "1 1 300px", minWidth: 0, maxWidth: 500 }}>

              {/* Default Encoder */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>Default Encoder</div>
                <select value={encoding.default_encoder}
                  onChange={(e) => setEncoding({ ...encoding, default_encoder: e.target.value })}
                  style={{ ...inputStyle, width: "100%" }}>
                  <option value="nvenc">NVENC (GPU — Hardware)</option>
                  <option value="libx265">libx265 (CPU — Software)</option>
                </select>
                <div style={helpStyle}>
                  {encoding.default_encoder === "nvenc"
                    ? "Hardware encoding using your NVIDIA GPU. Fast, lower power usage. Slightly larger files than CPU."
                    : "Software encoding using CPU. Slower but achieves better compression per bitrate."}
                </div>
              </div>

              {/* Target Codec */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>Target Codec</div>
                <select value={encoding.target_codec || "hevc"}
                  onChange={(e) => setEncoding({ ...encoding, target_codec: e.target.value })}
                  style={{ ...inputStyle, width: "100%" }}>
                  {TARGET_CODECS.map(c => (
                    <option key={c.value} value={c.value}>{c.label}</option>
                  ))}
                </select>
                <div style={helpStyle}>
                  {TARGET_CODECS.find(c => c.value === (encoding.target_codec || "hevc"))?.desc}
                </div>
              </div>

              {/* Parallel Jobs */}
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                  <span style={labelStyle}>Parallel Jobs</span>
                  <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding?.parallel_jobs ?? 8}</span>
                </div>
                <input type="range" min={1} max={16} value={encoding?.parallel_jobs ?? 8}
                  onChange={(e) => setEncoding({ ...encoding, parallel_jobs: parseInt(e.target.value) })}
                  style={{ width: "100%", accentColor: "var(--accent)" }} />
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                  <span>1</span><span>4</span><span>8</span><span>12</span><span>16</span>
                </div>
                <div style={helpStyle}>
                  Number of simultaneous encoding jobs. Higher = faster queue processing but more GPU/CPU load.
                </div>
              </div>

              {/* Target Resolution */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>Target Resolution</div>
                <select value={encoding.target_resolution || "copy"}
                  onChange={(e) => setEncoding({ ...encoding, target_resolution: e.target.value })}
                  style={{ ...inputStyle, width: "100%" }}>
                  {RESOLUTION_OPTIONS.map(r => (
                    <option key={r.value} value={r.value}>{r.label}</option>
                  ))}
                </select>
                <div style={helpStyle}>
                  {RESOLUTION_OPTIONS.find(r => r.value === (encoding.target_resolution || "copy"))?.desc}
                </div>
              </div>

              {/* Source Codecs to Convert */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>Convert From (source codecs)</div>
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
                      <span style={{ color: c.always ? "var(--success)" : "var(--text-secondary)" }}>{c.label}</span>
                      {c.always && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>(always)</span>}
                    </label>
                  ))}
                </div>
                <div style={helpStyle}>Select which source codecs should be converted to the target codec.</div>
              </div>

              {encoding.default_encoder !== "libx265" ? (
                <>
                  {/* NVENC Preset */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>NVENC Preset</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.nvenc_preset || "p6"}</span>
                    </div>
                    <input type="range" min={1} max={7} value={parseInt((encoding.nvenc_preset || "p6").replace("p", ""))}
                      onChange={(e) => setEncoding({ ...encoding, nvenc_preset: `p${e.target.value}` })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>p1 (Fastest)</span><span>p4</span><span>p7 (Best)</span>
                    </div>
                    <div style={{ ...helpStyle, padding: 8, background: "var(--bg-primary)", borderRadius: 4, marginTop: 8 }}>
                      <strong style={{ color: "var(--accent)" }}>{PRESET_INFO[encoding.nvenc_preset || "p6"]?.label}</strong>
                      {" — "}{PRESET_INFO[encoding.nvenc_preset || "p6"]?.desc}
                    </div>
                  </div>

                  {/* NVENC CQ */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>NVENC Constant Quality (CQ)</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.nvenc_cq}</span>
                    </div>
                    <input type="range" min={15} max={30} value={encoding.nvenc_cq}
                      onChange={(e) => setEncoding({ ...encoding, nvenc_cq: parseInt(e.target.value) })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>15 (Highest quality)</span><span>20</span><span>24</span><span>30 (Smallest file)</span>
                    </div>
                    <div style={helpStyle}>
                      Controls quality vs file size. Lower = higher quality, larger files.
                      <strong> 18-20:</strong> Transparent quality (recommended).
                      <strong> 21-24:</strong> Good quality, noticeable savings.
                      <strong> 25+:</strong> Visible quality loss, maximum compression.
                    </div>
                  </div>
                </>
              ) : (
                <>
                  {/* libx265 Preset */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>CPU Preset</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.libx265_preset || "medium"}</span>
                    </div>
                    <select value={encoding.libx265_preset || "medium"}
                      onChange={(e) => setEncoding({ ...encoding, libx265_preset: e.target.value })}
                      style={{ ...inputStyle, width: "100%" }}>
                      <option value="ultrafast">Ultrafast</option>
                      <option value="superfast">Superfast</option>
                      <option value="veryfast">Very Fast</option>
                      <option value="faster">Faster</option>
                      <option value="fast">Fast</option>
                      <option value="medium">Medium (default)</option>
                      <option value="slow">Slow</option>
                      <option value="slower">Slower</option>
                      <option value="veryslow">Very Slow (Best quality)</option>
                    </select>
                    <div style={helpStyle}>
                      Controls encoding speed vs compression efficiency. Slower presets produce smaller files at the same quality.
                      <strong> medium:</strong> Balanced speed and quality (recommended).
                      <strong> slow/slower:</strong> Better compression, significantly slower.
                      <strong> fast/veryfast:</strong> Quick encodes, larger files.
                    </div>
                  </div>

                  {/* libx265 CRF */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                      <span style={labelStyle}>Constant Rate Factor (CRF)</span>
                      <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{encoding.libx265_crf}</span>
                    </div>
                    <input type="range" min={15} max={28} value={encoding.libx265_crf}
                      onChange={(e) => setEncoding({ ...encoding, libx265_crf: parseInt(e.target.value) })}
                      style={{ width: "100%", accentColor: "var(--accent)" }} />
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                      <span>15 (Highest quality)</span><span>20</span><span>24</span><span>28 (Smallest file)</span>
                    </div>
                    <div style={helpStyle}>
                      Controls quality vs file size. Lower = higher quality, larger files.
                      CRF 18-20 is typically transparent to the original.
                    </div>
                  </div>
                </>
              )}

              {/* Smart Encoding */}
              <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
                <div style={{ fontSize: 14, fontWeight: 600, color: "white", marginBottom: 12 }}>Smart Encoding</div>

                {/* Content Type Detection Toggle */}
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, cursor: "pointer" }}>
                  <input type="checkbox"
                    checked={encoding.content_type_detection === true || encoding.content_type_detection === "true"}
                    onChange={(e) => setEncoding({ ...encoding, content_type_detection: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={labelStyle}>Content type detection</span>
                </label>
                <div style={helpStyle}>
                  Automatically detects content type from filenames (anime, grain, animation, remux) and applies
                  optimized CQ values. Anime compresses well (CQ 22), grain needs conservative settings (CQ 24).
                  Applied when no encoding rule sets a CQ value.
                </div>

                {/* VMAF Analysis Toggle */}
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 14, marginBottom: 8, cursor: "pointer" }}>
                  <input type="checkbox"
                    checked={encoding.vmaf_analysis_enabled === true || encoding.vmaf_analysis_enabled === "true" || (encoding.vmaf_analysis_enabled == null)}
                    onChange={(e) => setEncoding({ ...encoding, vmaf_analysis_enabled: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={labelStyle}>VMAF quality analysis</span>
                </label>
                <div style={helpStyle}>
                  <strong>VMAF</strong> (Video Multi-Method Assessment Fusion) is a perceptual video quality metric developed by Netflix.
                  It scores encoded video from 0-100 by comparing it against the original source, predicting how a human viewer would rate the quality.
                  When enabled, Shrinkerr runs a frame-accurate VMAF comparison between the original and encoded file after conversion.
                  This adds a few minutes per job but gives you confidence that your CQ settings produce acceptable quality.
                </div>
                <table style={{ fontSize: 12, borderCollapse: "collapse", marginTop: 8, width: "100%" }}>
                  <thead>
                    <tr style={{ borderBottom: "1px solid var(--border)" }}>
                      <th style={{ textAlign: "left", padding: "4px 0 4px 28px", color: "var(--text-secondary)", fontWeight: 600, width: 120 }}>Score</th>
                      <th style={{ textAlign: "left", padding: "4px 0", color: "var(--text-secondary)", fontWeight: 600 }}>Quality</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ["93+", "Transparent / indistinguishable", "#18ffa5"],
                      ["87–93", "High quality streaming", "var(--accent)"],
                      ["80–87", "Acceptable quality", "#ffa94d"],
                      ["< 80", "Noticeable degradation", "#e94560"],
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
                  // Tier colour mirrors the table above — gives quick visual
                  // feedback about how strict the user's threshold is.
                  const tierColor =
                    minScore >= 93 ? "#18ffa5"
                      : minScore >= 87 ? "var(--accent)"
                        : minScore >= 80 ? "#ffa94d"
                          : "#e94560";
                  return (
                    <div style={{ marginTop: 16, padding: 12, background: "var(--bg-primary)", borderRadius: 4 }}>
                      <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, cursor: "pointer" }}>
                        <input
                          type="checkbox"
                          checked={enabled}
                          onChange={(e) => setEncoding({ ...encoding, vmaf_min_score: e.target.checked ? 85 : 0 })}
                          style={{ accentColor: "var(--accent)" }}
                        />
                        <span style={labelStyle}>Reject encodes below a minimum VMAF score</span>
                      </label>
                      <div style={{ ...helpStyle, marginLeft: 26 }}>
                        When enabled, any encode whose measured VMAF score is below the threshold
                        is discarded — the encoded temp file is deleted, the original is left in
                        place, and the job is recorded as <strong>rejected</strong> with the score and
                        threshold visible in the job details. Useful as a safety net against
                        overly-aggressive CQ/CRF settings.
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
                            color: tierColor, fontWeight: 700, fontSize: 14,
                          }}>
                            {minScore.toFixed(0)}
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })()}

                {/* Resolution-Aware CQ Toggle + Table */}
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 14, marginBottom: 8, cursor: "pointer" }}>
                  <input type="checkbox"
                    checked={encoding.resolution_aware_cq === true || encoding.resolution_aware_cq === "true"}
                    onChange={(e) => setEncoding({ ...encoding, resolution_aware_cq: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={labelStyle}>Resolution-aware quality</span>
                </label>
                <div style={helpStyle}>
                  Use different CQ values per resolution. 4K benefits from higher CQ since downsampling during playback hides artifacts.
                  Fallback when no rule or content detection sets CQ.
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
                        <input key={`r-${key}`} type="range" min={15} max={30}
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
                <h4 style={{ color: "white", fontSize: 13, marginBottom: 12 }}>Timeouts</h4>
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={labelStyle}>ffmpeg timeout (hours)</span>
                    <input type="number" min={1} max={72} step={1}
                      value={Math.round((encoding.ffmpeg_timeout || 21600) / 3600)}
                      onChange={(e) => setEncoding({ ...encoding, ffmpeg_timeout: parseInt(e.target.value) * 3600 })}
                      style={{ ...inputStyle, width: 70, textAlign: "center" as const }} />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={labelStyle}>ffprobe timeout (seconds)</span>
                    <input type="number" min={5} max={300} step={5}
                      value={encoding.ffprobe_timeout || 30}
                      onChange={(e) => setEncoding({ ...encoding, ffprobe_timeout: parseInt(e.target.value) })}
                      style={{ ...inputStyle, width: 70, textAlign: "center" as const }} />
                  </div>
                </div>
              </div>

              <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start" }}>
                Save Encoding Settings
              </button>
            </div>

            {/* Conversion Guide */}
            <div style={{
              flex: "1 1 300px", minWidth: 0, background: "var(--bg-primary)", borderRadius: 6,
              padding: 16, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6,
            }}>
              <h4 style={{ color: "white", marginBottom: 12, fontSize: 14 }}>Conversion Guide</h4>

              {[
                {
                  title: "Understanding Presets",
                  desc: "Higher preset = slower encoding but better compression. The GPU works harder to find optimal ways to compress each frame.",
                  cols: ["Preset", "Speed", "Quality/Size"],
                  rows: [
                    ["p1-p2", "~400 fps", "Largest files"],
                    ["p3-p4", "~250 fps", "Balanced"],
                    ["p5", "~180 fps", "Good compression"],
                    ["p6", "~120 fps", "Great compression"],
                    ["p7", "~80 fps", "Best compression"],
                  ],
                  note: "Speeds are approximate for 1080p on Quadro P2200.",
                },
                {
                  title: "Understanding CQ / CRF",
                  desc: "CQ (NVENC) and CRF (libx265) control the quality target. Lower = higher quality, larger files. The encoder allocates more bits to complex scenes and fewer to simple ones.",
                  cols: ["CQ/CRF", "Quality", "Savings"],
                  rows: [
                    ["15-18", "Overkill", "5-15%"],
                    ["19-20", "Transparent", "20-30%"],
                    ["21-23", "Excellent", "30-45%"],
                    ["24-26", "Good", "45-60%"],
                    ["27-30", "Noticeable loss", "60%+"],
                  ],
                },
                {
                  title: "Recommended Combos",
                  cols: ["Priority", "Settings", "Savings"],
                  rows: [
                    ["Max quality", "p7 / CQ 20", "20-30%"],
                    ["Quality first", "p6 / CQ 21", "25-35%"],
                    ["Balanced", "p5 / CQ 23", "35-45%"],
                    ["Space saver", "p4 / CQ 25", "45-55%"],
                    ["Max compression", "p3 / CQ 27", "55-65%"],
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
                <div style={{ color: "var(--accent)", fontWeight: "bold", marginBottom: 4 }}>Tips</div>
                <ul style={{ paddingLeft: 16, margin: 0 }}>
                  <li style={{ marginBottom: 4 }}>Blu-ray rips (15-40 GB) typically see the biggest savings</li>
                  <li style={{ marginBottom: 4 }}>WEB-DL files (3-8 GB) are already well-compressed — expect smaller gains or use a higher CQ</li>
                  <li style={{ marginBottom: 4 }}>Grain-heavy content (film, older movies) benefits from lower CQ values to preserve detail</li>
                  <li style={{ marginBottom: 4 }}>Animation compresses extremely well — even CQ 25+ looks great</li>
                  <li style={{ marginBottom: 4 }}>NVENC CQ ≈ libx265 CRF + 2 for similar quality (e.g., CQ 22 ≈ CRF 20)</li>
                </ul>
              </div>

              <div style={{
                background: "var(--bg-card)", padding: 10, borderRadius: 4,
                border: "1px solid var(--border)", fontSize: 11,
              }}>
                <strong style={{ color: "var(--success)" }}>Current: {encoding.nvenc_preset || "p6"} / CQ {encoding.nvenc_cq}</strong>
                <span> — </span>
                {(() => {
                  const p = parseInt((encoding.nvenc_preset || "p6").replace("p", ""));
                  const cq = encoding.nvenc_cq || 20;
                  if (cq <= 20 && p >= 6) return "Maximum quality, conservative compression";
                  if (cq <= 20) return "High quality, moderate compression";
                  if (cq <= 23 && p >= 5) return "Great quality with good space savings";
                  if (cq <= 23) return "Good quality, solid compression";
                  if (cq <= 26) return "Good quality, aggressive compression";
                  return "Maximum compression, some quality tradeoff";
                })()}
              </div>
            </div>
            </div>
          </div>

          <h2 id="audio" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            Audio Settings
          </h2>
          {/* Audio Track Rules */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>Audio Track Rules</h3>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 16 }}>
              <input type="checkbox" checked={encoding?.audio_cleanup_enabled ?? true}
                readOnly
                onClick={() => setEncoding({ ...encoding, audio_cleanup_enabled: !(encoding?.audio_cleanup_enabled ?? true) })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Remove unwanted audio tracks</span>
            </label>
            {(encoding?.audio_cleanup_enabled ?? true) && (
            <div style={{ display: "flex", gap: 24, alignItems: "flex-start", flexWrap: "wrap" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 20, flex: "1 1 300px", minWidth: 0, maxWidth: 500 }}>

              {/* Always Keep Languages */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>Always Keep Languages</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                  {keepLangs.map((code: string) => {
                    const lang = ALL_LANGUAGES.find(l => l.code === code);
                    return (
                      <span key={code} style={{
                        background: "var(--border)", color: "var(--success)", padding: "4px 10px",
                        borderRadius: 16, fontSize: 12, display: "flex", alignItems: "center", gap: 6,
                      }}>
                        {lang ? `${lang.name} (${code})` : code}
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
                    placeholder="Search languages to add..."
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
                  Audio tracks in these languages will always be kept (locked). Only tracks in other languages will be suggested for removal.
                </div>
              </div>

              {/* Keep native language */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.keep_native_language !== false}
                  onChange={() => setEncoding({ ...encoding, keep_native_language: encoding.keep_native_language === false })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>Auto-keep native language tracks</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                Automatically keep audio and subtitle tracks matching each file's detected native language. Disable if you only want to keep dubbed/specified language tracks.
              </div>

              {/* Reorder native language first */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.reorder_native_audio !== false}
                  onChange={() => setEncoding({ ...encoding, reorder_native_audio: encoding.reorder_native_audio === false })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>Reorder native language to first audio stream</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                During conversion or audio cleanup, move native-language audio tracks to the first position so they become the default playback stream. Files where native isn't first will be tagged for audio cleanup.
              </div>

              {/* Ignore Unknown Tracks */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.ignore_unknown_tracks}
                  readOnly
                  onClick={() => setEncoding({ ...encoding, ignore_unknown_tracks: !encoding.ignore_unknown_tracks })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>Keep unknown/undefined tracks</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
                Tracks tagged as "und" or with no language metadata
              </div>

              {/* Audio Conversion */}
              <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16 }}>
                <h4 style={{ color: "white", fontSize: 13, marginBottom: 12 }}>Audio Conversion</h4>

                <div style={{ marginBottom: 16 }}>
                  <div style={{ ...labelStyle, marginBottom: 8 }}>Audio Codec</div>
                  <select value={encoding.audio_codec || "copy"}
                    onChange={(e) => setEncoding({ ...encoding, audio_codec: e.target.value })}
                    style={{ ...inputStyle, width: "100%" }}>
                    {AUDIO_CODECS.map(c => (
                      <option key={c.value} value={c.value}>{c.label}</option>
                    ))}
                  </select>
                  <div style={helpStyle}>
                    {AUDIO_CODECS.find(c => c.value === (encoding.audio_codec || "copy"))?.desc}
                  </div>
                </div>

                {(encoding.audio_codec && encoding.audio_codec !== "copy") && (
                  <>
                    <div style={{ marginBottom: 16 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                        <span style={labelStyle}>Audio Bitrate</span>
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
                        Higher bitrate = better audio quality, larger files.
                        <strong> 128 kbps:</strong> Good for stereo.
                        <strong> 256 kbps:</strong> Good for 5.1 surround.
                        <strong> 384-640 kbps:</strong> High quality surround.
                      </div>
                    </div>

                    <div>
                      <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                        <input type="checkbox" checked={encoding.audio_downmix || false}
                          readOnly
                          onClick={() => setEncoding({ ...encoding, audio_downmix: !encoding.audio_downmix })}
                          style={{ flexShrink: 0 }} />
                        <span style={labelStyle}>Downmix surround to stereo</span>
                      </label>
                      <div style={{ ...helpStyle, paddingLeft: 26 }}>
                        Convert 5.1/7.1 surround to stereo. Saves space but loses surround channels.
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
                    <span style={labelStyle}>Auto-convert lossless audio</span>
                  </label>
                  <div style={{ ...helpStyle, paddingLeft: 26 }}>
                    Automatically convert lossless audio tracks (DTS-HD MA, TrueHD, PCM, FLAC) to a smaller lossy codec during video conversion. Lossy tracks are left untouched.
                  </div>

                  {encoding.auto_convert_lossless && (
                    <div style={{ paddingLeft: 26, marginTop: 12, display: "flex", flexDirection: "column", gap: 12 }}>
                      <div>
                        <div style={{ ...labelStyle, marginBottom: 8 }}>Target Codec</div>
                        <select value={encoding.lossless_target_codec || "eac3"}
                          onChange={(e) => setEncoding({ ...encoding, lossless_target_codec: e.target.value })}
                          style={{ ...inputStyle, width: "100%" }}>
                          {AUDIO_CODECS.filter(c => c.value !== "copy" && c.value !== "flac").map(c => (
                            <option key={c.value} value={c.value}>{c.label}</option>
                          ))}
                        </select>
                      </div>
                      <div>
                        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                          <span style={labelStyle}>Target Bitrate</span>
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
                Save Audio Rules
              </button>
            </div>

            {/* Audio Guide */}
            <div style={{
              flex: "1 1 300px", minWidth: 0, background: "var(--bg-primary)", borderRadius: 6,
              padding: 16, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6,
            }}>
              <h4 style={{ color: "white", marginBottom: 12, fontSize: 14 }}>Audio Guide</h4>

              {[
                {
                  title: "Common Audio Codecs",
                  cols: ["Codec", "Type", "Typical Size"],
                  rows: [
                    ["AAC", "Lossy", "~50 MB/hr"],
                    ["AC3 (DD)", "Lossy 5.1", "~250 MB/hr"],
                    ["EAC3 (DD+)", "Lossy 5.1/7.1", "~350 MB/hr"],
                    ["DTS", "Lossy 5.1", "~550 MB/hr"],
                    ["DTS-HD MA", "Lossless 5.1/7.1", "~1.5 GB/hr"],
                    ["TrueHD", "Lossless 7.1", "~2 GB/hr"],
                    ["FLAC", "Lossless", "~1 GB/hr"],
                    ["PCM", "Uncompressed", "~3 GB/hr"],
                  ],
                },
                {
                  title: "Space Saved by Removing Tracks",
                  desc: "Each extra audio track adds significant file size. A 2-hour movie with 4 unnecessary audio tracks can waste 1-8 GB.",
                  cols: ["Tracks Removed", "Typical Savings", "Example"],
                  rows: [
                    ["1 × AC3", "~500 MB", "Commentary track"],
                    ["1 × DTS", "~1.1 GB", "Foreign dub"],
                    ["3 × AC3", "~1.5 GB", "3 foreign dubs"],
                    ["1 × DTS-HD MA", "~3 GB", "Lossless foreign"],
                    ["1 × TrueHD", "~4 GB", "Atmos foreign dub"],
                  ],
                },
                {
                  title: "Re-encoding vs Copy",
                  cols: ["Mode", "Speed", "Use When"],
                  rows: [
                    ["Copy", "Instant", "Always (recommended)"],
                    ["AAC 128k", "Fast", "Stereo, small files"],
                    ["AAC 256k", "Fast", "Stereo, good quality"],
                    ["AC3 384k", "Fast", "5.1, compatibility"],
                    ["EAC3 640k", "Fast", "5.1/7.1, modern"],
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
                <div style={{ color: "var(--accent)", fontWeight: "bold", marginBottom: 4 }}>Tips</div>
                <ul style={{ paddingLeft: 16, margin: 0 }}>
                  <li style={{ marginBottom: 4 }}>Use "Copy" mode unless you specifically need a different codec — it's instant and lossless</li>
                  <li style={{ marginBottom: 4 }}>Blu-ray discs often include 3-6 language dubs — removing them is the easiest space win</li>
                  <li style={{ marginBottom: 4 }}>DTS-HD MA and TrueHD are lossless and huge — consider keeping only for your primary language</li>
                  <li style={{ marginBottom: 4 }}>Commentary tracks are usually AC3 stereo (~250 MB each) — safe to remove unless you listen to them</li>
                  <li style={{ marginBottom: 4 }}>The native language is auto-detected per file, so foreign films keep their original audio</li>
                </ul>
              </div>

              <div style={{
                background: "var(--bg-card)", padding: 10, borderRadius: 4,
                border: "1px solid var(--border)", fontSize: 11,
              }}>
                <strong style={{ color: "var(--success)" }}>Current: Keep {keepLangs.join(", ").toUpperCase() || "none"} + native</strong>
                <span> — </span>
                {keepLangs.length === 0
                  ? "Only the native language track is kept"
                  : `${keepLangs.length} language${keepLangs.length > 1 ? "s" : ""} locked, plus native auto-detected per file`}
              </div>
            </div>
            </div>
            )}
          </div>

          <h2 id="subtitles" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            Subtitles
          </h2>
          {/* Subtitle Cleanup */}
          <div style={sectionStyle}>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 16 }}>
              <input type="checkbox" checked={encoding?.sub_cleanup_enabled ?? true}
                readOnly
                onClick={() => setEncoding({ ...encoding, sub_cleanup_enabled: !(encoding?.sub_cleanup_enabled ?? true) })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Remove unwanted subtitle tracks</span>
            </label>
            {(encoding?.sub_cleanup_enabled ?? true) && (
            <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 500 }}>

              {/* Subtitle Keep Languages */}
              <div>
                <div style={{ ...labelStyle, marginBottom: 8 }}>Keep Subtitle Languages</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                  {(encoding.sub_keep_languages || []).map((code: string) => {
                    const lang = ALL_LANGUAGES.find(l => l.code === code);
                    return (
                      <span key={code} style={{
                        background: "var(--border)", color: "var(--success)", padding: "4px 10px",
                        borderRadius: 16, fontSize: 12, display: "flex", alignItems: "center", gap: 6,
                      }}>
                        {lang ? `${lang.name} (${code})` : code}
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
                    placeholder="Search languages to add..."
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
                  Internal subtitle tracks in these languages will always be kept. Forced subtitles are always kept regardless of language. Only tracks in other languages will be marked for removal.
                </div>
              </div>

              {/* Keep Unknown Subs */}
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding.sub_keep_unknown ?? true}
                  readOnly
                  onClick={() => setEncoding({ ...encoding, sub_keep_unknown: !(encoding.sub_keep_unknown ?? true) })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>Keep unknown/undefined subtitles</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -14, paddingLeft: 26 }}>
                Subtitle tracks tagged as "und" or with no language metadata
              </div>

              <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start" }}>
                Save Subtitle Rules
              </button>
            </div>
            )}

            {/* External Subtitles */}
            <div style={{ borderTop: "1px solid var(--border)", marginTop: 16, paddingTop: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>External Subtitles</div>
              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 8 }}>
                <input type="checkbox" checked={encoding.merge_external_subs ?? false}
                  onChange={() => setEncoding({ ...encoding, merge_external_subs: !(encoding.merge_external_subs ?? false) })}
                  style={{ flexShrink: 0 }} />
                <span style={labelStyle}>Merge external subtitles into video</span>
              </label>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -4, paddingLeft: 26, marginBottom: 12 }}>
                During conversion or remux, embed external .srt, .ass, .ssa, .sub, .vtt files found alongside the video into the MKV container.
                Language is detected from the filename (e.g. <code>.eng.srt</code>, <code>.en.forced.srt</code>).
              </div>

              {(encoding.merge_external_subs ?? false) && (
                <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 8 }}>
                  <input type="checkbox" checked={encoding.delete_external_subs_after_merge ?? false}
                    onChange={() => setEncoding({ ...encoding, delete_external_subs_after_merge: !(encoding.delete_external_subs_after_merge ?? false) })}
                    style={{ flexShrink: 0 }} />
                  <span style={labelStyle}>Delete external subtitle files after merge</span>
                </label>
              )}
              {(encoding.merge_external_subs ?? false) && (encoding.delete_external_subs_after_merge ?? false) && (
                <div style={{ fontSize: 11, color: "var(--warning)", paddingLeft: 26, marginTop: -4 }}>
                  The original .srt/.ass files will be permanently deleted after a successful merge.
                </div>
              )}

              <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start", marginTop: 12 }}>
                Save External Subtitle Settings
              </button>
            </div>
          </div>

          <h2 id="connections" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            Connections
          </h2>
          {/* Metadata APIs */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 4 }}>Metadata APIs</h3>
            <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
              Connect to TMDB to fetch movie and TV show metadata, posters, ratings, and detect the original language for accurate audio track classification. TMDB also resolves TVDB IDs, so a separate TVDB key isn't required. Powers the poster grid view and improves foreign title handling.
            </div>

            {/* TMDB API Key */}
            <div style={{ marginBottom: 20 }}>
              <div style={{ ...labelStyle, marginBottom: 8 }}>
                TMDB API Key{" "}
                <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener noreferrer"
                  style={{ fontSize: 11, color: "var(--accent)" }}>(Get free key)</a>
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <div style={{ position: "relative", flex: 1, maxWidth: 400 }}>
                  <input
                    type={showTmdbKey ? "text" : "password"}
                    value={tmdbKey}
                    onChange={(e) => setTmdbKey(e.target.value)}
                    placeholder="Enter TMDB API key..."
                    style={{ ...inputStyle, width: "100%", paddingRight: 36 }}
                  />
                  <button
                    onClick={() => setShowTmdbKey(!showTmdbKey)}
                    style={{
                      position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)",
                      background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 14,
                    }}
                    title={showTmdbKey ? "Hide" : "Show"}
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
                        setTmdbTest({ status: "error", error: res.error || "Test failed" });
                      }
                    } catch (e: any) {
                      setTmdbTest({ status: "error", error: e.message || "Request failed" });
                    }
                  }}
                  disabled={tmdbTest.status === "loading"}
                  style={{ minWidth: 60 }}
                >
                  {tmdbTest.status === "loading" ? "..." : "Test"}
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
                  <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--success)", display: "inline-block" }} />
                  <span style={{ fontSize: 12, color: "var(--success)" }}>Connected</span>
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
              Save API Key
            </button>
          </div>

          {/* Plex */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 4 }}>Plex</h3>
            <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
              Connect your Plex server to automatically refresh your library after each conversion, create encoding rules based on Plex labels, collections, and genres, prioritize unwatched content in the queue, and pause encoding when someone is streaming.
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
                    Connected to <span style={{ color: "#e5a00d" }}>{plexConn.server_name || "Plex"}</span>
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
                  Disconnect
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
                  Connect to Plex
                </button>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
                  Opens Plex sign-in in a popup. After you sign in we'll list your servers so you can pick which one to connect.
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
                  <div style={{ fontSize: 13, color: "white" }}>Waiting for Plex sign-in…</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    Complete the sign-in in the popup window. This page will update automatically.
                  </div>
                </div>
                <div style={{ flex: 1 }} />
                <button
                  className="btn btn-secondary"
                  style={{ fontSize: 12, padding: "4px 10px" }}
                  onClick={() => { setPlexAuthState("idle"); setPlexPickerError(""); }}
                >
                  Cancel
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
                  Choose which Plex server to connect
                </div>
                {plexServers.length === 0 ? (
                  <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>
                    No Plex servers found for this account.
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
                                {server.owned && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(229,160,13,0.2)", color: "#e5a00d" }}>OWNED</span>}
                                {conn.local && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(0,200,100,0.15)", color: "var(--success)" }}>LOCAL</span>}
                                {conn.relay && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "var(--border)", color: "var(--text-muted)" }}>RELAY</span>}
                                {conn.reachable === true && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(0,200,100,0.15)", color: "var(--success)" }}>REACHABLE</span>}
                                {conn.reachable === false && <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background: "rgba(231,76,60,0.2)", color: "var(--danger, #e74c3c)" }}>UNREACHABLE</span>}
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
                    Use this server
                  </button>
                  <button
                    className="btn btn-secondary"
                    style={{ fontSize: 12, padding: "6px 14px" }}
                    onClick={() => { setPlexAuthState("idle"); setPlexPendingToken(""); setPlexServers([]); setPlexPickedUri(""); setPlexPickerError(""); }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {plexAuthState === "saving" && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>Saving…</div>
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
              {showManualPlex ? "Hide manual setup" : "Advanced: enter URL & token manually"}
            </button>

            {showManualPlex && (
              <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: 12, marginTop: 8 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={labelStyle}>Plex Server URL</span>
                </div>
                <input
                  type="text"
                  value={plexUrl}
                  onChange={(e) => setPlexUrl(e.target.value)}
                  placeholder="http://192.168.0.103:32400"
                  style={{ ...inputStyle, width: "100%", marginBottom: 12 }}
                />

                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span style={labelStyle}>Plex Auth Token</span>
                  <a href="https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/" target="_blank" rel="noopener noreferrer" style={{ fontSize: 11, color: "var(--accent)" }}>(How to find your token)</a>
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <div style={{ position: "relative", flex: 1 }}>
                    <input
                      type={showPlexToken ? "text" : "password"}
                      value={plexToken}
                      onChange={(e) => setPlexToken(e.target.value)}
                      placeholder="Your Plex auth token"
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
                          setPlexTest({ status: "error", error: (result as any).error || "Failed" });
                        }
                      } catch (e: any) {
                        setPlexTest({ status: "error", error: e.message });
                      }
                    }}
                  >
                    {plexTest.status === "loading" ? "Testing..." : "Test"}
                  </button>
                  {plexTest.status === "success" && (
                    <span style={{ color: "var(--success)", fontSize: 12 }}>&#10003; {plexTest.serverName} ({plexTest.libraryCount} libraries)</span>
                  )}
                  {plexTest.status === "error" && (
                    <span style={{ color: "var(--danger, #e74c3c)", fontSize: 12 }}>&#10007; {plexTest.error}</span>
                  )}
                </div>
              </div>
            )}

            <div style={{ marginTop: 16 }}>
              <span style={labelStyle}>Path Mapping (Container → Host)</span>
              <input
                type="text"
                value={plexPathMapping}
                onChange={(e) => setPlexPathMapping(e.target.value)}
                placeholder="/media=/srv/media"
                style={{ ...inputStyle, width: "100%", marginTop: 4 }}
              />
              <div style={helpStyle}>
                Maps Docker container paths to host paths that Plex can see.
                Format: <code>/container/path=/host/path</code>.
                Multiple mappings separated by <code>;</code>
              </div>
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginTop: 12 }}>
              <input type="checkbox" checked={encoding?.plex_scan_after_conversion !== false && encoding?.plex_scan_after_conversion !== "false"}
                onChange={() => setEncoding({ ...encoding, plex_scan_after_conversion: encoding?.plex_scan_after_conversion === false || encoding?.plex_scan_after_conversion === "false" })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Refresh library after conversion</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 28 }}>
              Trigger a partial Plex library scan after each file is converted so changes appear immediately.
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginTop: 12 }}>
              <input type="checkbox" checked={encoding?.plex_empty_trash_after_scan || false}
                readOnly
                onClick={() => setEncoding({ ...encoding, plex_empty_trash_after_scan: !encoding?.plex_empty_trash_after_scan })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Empty trash after scan</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
              Automatically empty the Plex library trash after each conversion scan completes.
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginTop: 12 }}>
              <input type="checkbox" checked={encoding?.plex_prioritize_unwatched || false}
                onChange={() => setEncoding({ ...encoding, plex_prioritize_unwatched: !encoding?.plex_prioritize_unwatched })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Prioritize unwatched content</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
              When adding files to the queue, unwatched content automatically gets High priority so it converts first. You're more likely to notice quality improvements on content you haven't watched yet. Requires syncing with Plex.
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
              Save Plex Settings
            </button>
          </div>

          {/* Jellyfin */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 4 }}>Jellyfin</h3>
            <div style={{ ...helpStyle, marginTop: 0, marginBottom: 16 }}>
              Connect your Jellyfin server to automatically refresh your library after each conversion, create encoding rules based on Jellyfin tags and genres, sync watched status for queue prioritization, and pause encoding during active streams.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
              <div>
                <div style={labelStyle}>Jellyfin URL</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="http://192.168.0.103:8096"
                  value={encoding?.jellyfin_url || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_url: e.target.value })} />
              </div>
              <div>
                <div style={labelStyle}>API Key</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="Your Jellyfin API key"
                  type="password"
                  value={encoding?.jellyfin_api_key || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_api_key: e.target.value })} />
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
              <div>
                <div style={labelStyle}>User ID (optional)</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="Auto-detected if empty"
                  value={encoding?.jellyfin_user_id || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_user_id: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  Used for watched status. Leave empty to auto-detect the admin user.
                </div>
              </div>
              <div>
                <div style={labelStyle}>Path mapping</div>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="/media=/mnt/media"
                  value={encoding?.jellyfin_path_mapping || ""}
                  onChange={(e) => setEncoding({ ...encoding, jellyfin_path_mapping: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  Maps container paths to Jellyfin paths. Format: /container=/jellyfin
                </div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 12 }}>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    const r = await testApiKey("jellyfin");
                    if (r.success) toast(`Connected to ${(r as any).server_name || "Jellyfin"} (${(r as any).library_count || 0} libraries)`, "success");
                    else toast(r.error || "Connection failed");
                  } catch { toast("Connection failed"); }
                }}>Test Connection</button>
              {encoding?.jellyfin_configured && (
                <span style={{ fontSize: 11, color: "var(--success)", display: "flex", alignItems: "center", gap: 4 }}>
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--success)" }} />
                  Connected
                </span>
              )}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <input type="checkbox" checked={encoding?.jellyfin_scan_after_conversion !== false && encoding?.jellyfin_scan_after_conversion !== "false"}
                  onChange={() => setEncoding({ ...encoding, jellyfin_scan_after_conversion: encoding?.jellyfin_scan_after_conversion === false || encoding?.jellyfin_scan_after_conversion === "false" })} />
                <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>Refresh library after conversion</span>
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
                toast("Jellyfin settings saved", "success");
              }}>Save Jellyfin Settings</button>
          </div>

          {/* Sonarr / Radarr Integration */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 16 }}>Sonarr / Radarr</h3>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
              After conversion, Shrinkerr can notify Sonarr/Radarr to rescan the title folder so they update their database with the new file.
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16, marginBottom: 16 }}>
              {/* Sonarr */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 10 }}>Sonarr</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <div>
                    <label style={labelStyle}>URL</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="http://localhost:8989"
                      value={encoding?.sonarr_url || ""}
                      onChange={e => setEncoding({ ...encoding, sonarr_url: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>API Key</label>
                    <input type="password" style={{ ...inputStyle, width: "100%" }} placeholder="From Sonarr Settings > General"
                      value={encoding?.sonarr_api_key || ""}
                      onChange={e => setEncoding({ ...encoding, sonarr_api_key: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>Path Mapping</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="/media=/  (container=sonarr)"
                      value={encoding?.sonarr_path_mapping || ""}
                      onChange={e => setEncoding({ ...encoding, sonarr_path_mapping: e.target.value })} />
                    <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                      Maps container paths to Sonarr paths. e.g. /media/TV1=/TV1
                    </div>
                  </div>
                </div>
              </div>
              {/* Radarr */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 10 }}>Radarr</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <div>
                    <label style={labelStyle}>URL</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="http://localhost:7878"
                      value={encoding?.radarr_url || ""}
                      onChange={e => setEncoding({ ...encoding, radarr_url: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>API Key</label>
                    <input type="password" style={{ ...inputStyle, width: "100%" }} placeholder="From Radarr Settings > General"
                      value={encoding?.radarr_api_key || ""}
                      onChange={e => setEncoding({ ...encoding, radarr_api_key: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>Path Mapping</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="/media/Movies=/  (container=radarr)"
                      value={encoding?.radarr_path_mapping || ""}
                      onChange={e => setEncoding({ ...encoding, radarr_path_mapping: e.target.value })} />
                    <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                      Maps container paths to Radarr paths. e.g. /media/Movies=/Movies
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
                    toast("Sonarr/Radarr settings saved", "success");
                  } catch (err: any) { toast(`Save failed: ${err.message}`); }
                }}
              >Save</button>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    await updateEncodingSettings(encoding);
                    const res = await testApiKey("sonarr") as any;
                    if (res.success) toast(`Sonarr connected (v${res.version})`, "success");
                    else toast(`Sonarr: ${res.error}`);
                  } catch (err: any) { toast(`Sonarr test failed: ${err.message}`); }
                }}
              >Test Sonarr</button>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  try {
                    await updateEncodingSettings(encoding);
                    const res = await testApiKey("radarr") as any;
                    if (res.success) toast(`Radarr connected (v${res.version})`, "success");
                    else toast(`Radarr: ${res.error}`);
                  } catch (err: any) { toast(`Radarr test failed: ${err.message}`); }
                }}
              >Test Radarr</button>
            </div>
          </div>

          {/* Download Client Integration */}
          <div style={sectionStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ color: "white", margin: 0 }}>NZBGet / SABnzbd</h3>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{encoding?.nzbget_enabled ? "Enabled" : "Disabled"}</span>
                <input type="checkbox" checked={encoding?.nzbget_enabled || false}
                  onChange={() => setEncoding({ ...encoding, nzbget_enabled: !encoding?.nzbget_enabled })}
                  style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
              </label>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
              Automatically convert downloads after NZBGet or SABnzbd completes. The script checks Sonarr/Radarr for matching tags before converting. Sonarr/Radarr connection settings are inherited from above.
            </div>

            {/* Tags */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelStyle}>Tags to match in Sonarr/Radarr</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                {(encoding?.nzbget_tags || []).map((tag: string) => (
                  <span key={tag} style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "var(--border)", padding: "4px 10px", borderRadius: 16, fontSize: 12, color: "var(--success)" }}>
                    {tag}
                    <button onClick={() => setEncoding({ ...encoding, nzbget_tags: (encoding?.nzbget_tags || []).filter((t: string) => t !== tag) })}
                      style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1 }}>&times;</button>
                  </span>
                ))}
                <input type="text" placeholder="Add tag..."
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
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Series/movies must have at least one of these tags. Press Enter to add.</div>
            </div>

            {/* Categories */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelStyle}>Categories to process</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
                {(encoding?.nzbget_categories || []).map((cat: string) => (
                  <span key={cat} style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "var(--border)", padding: "4px 10px", borderRadius: 16, fontSize: 12, color: "var(--success)" }}>
                    {cat}
                    <button onClick={() => setEncoding({ ...encoding, nzbget_categories: (encoding?.nzbget_categories || []).filter((c: string) => c !== cat) })}
                      style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 0, fontSize: 14, lineHeight: 1 }}>&times;</button>
                  </span>
                ))}
                <input type="text" placeholder="Add category..."
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
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Only downloads in these categories will be processed. Works with both NZBGet and SABnzbd. Press Enter to add.</div>
            </div>

            {/* Path Mappings */}
            <div style={{ marginBottom: 16 }}>
              <div style={labelStyle}>Path mappings (download client → Shrinkerr path)</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>Map your download client's completed folder to a path Shrinkerr can access. Make sure this path is added as a volume in your Shrinkerr docker-compose.</div>
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
                  + Add mapping
                </button>
              </div>
            </div>

            {/* Options row */}
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 16 }}>
              <div>
                <div style={labelStyle}>Priority</div>
                <select value={encoding?.nzbget_priority || "High"}
                  onChange={(e) => setEncoding({ ...encoding, nzbget_priority: e.target.value })}
                  style={{ ...inputStyle, width: 140 }}>
                  <option value="Normal">Normal</option>
                  <option value="High">High</option>
                  <option value="Highest">Highest</option>
                </select>
              </div>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 18 }}>
                <input type="checkbox" checked={encoding?.nzbget_wait_for_completion !== false}
                  onChange={() => setEncoding({ ...encoding, nzbget_wait_for_completion: encoding?.nzbget_wait_for_completion === false })}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>Wait for conversion to complete</span>
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 18 }}>
                <input type="checkbox" checked={encoding?.nzbget_check_sonarr_tags !== false}
                  onChange={() => setEncoding({ ...encoding, nzbget_check_sonarr_tags: encoding?.nzbget_check_sonarr_tags === false })}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>Check Sonarr tags</span>
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 18 }}>
                <input type="checkbox" checked={encoding?.nzbget_check_radarr_tags !== false}
                  onChange={() => setEncoding({ ...encoding, nzbget_check_radarr_tags: encoding?.nzbget_check_radarr_tags === false })}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>Check Radarr tags</span>
              </label>
            </div>

            {/* Save + Download */}
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 16 }}>
              <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 16px" }}
                onClick={async () => {
                  await updateEncodingSettings(encoding);
                  toast("Download client settings saved", "success");
                }}>Save</button>
              <a href="/api/settings/nzbget-script" download="Shrinkerr.py" style={{ textDecoration: "none" }}>
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 16px" }}>
                  Download NZBGet Script
                </button>
              </a>
              <a href="/api/settings/sabnzbd-script" download="shrinkerr.py" style={{ textDecoration: "none" }}>
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 16px" }}>
                  Download SABnzbd Script
                </button>
              </a>
            </div>

            {/* Installation instructions */}
            <details style={{ fontSize: 12, color: "var(--text-muted)" }}>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>NZBGet Installation</summary>
              <ol style={{ margin: 0, paddingLeft: 20, lineHeight: 2 }}>
                <li>Save settings above and click <b>Download NZBGet Script</b></li>
                <li>Place <code>Shrinkerr.py</code> in your NZBGet <b>ScriptDir</b> folder</li>
                <li>In NZBGet → Settings → Extension Scripts, enable <b>Shrinkerr</b></li>
                <li>Restart NZBGet or click <b>Reload</b> in the scripts section</li>
                <li>The script auto-configures from Shrinkerr — no NZBGet settings needed</li>
                <li>Add your configured tags to series in Sonarr / movies in Radarr</li>
              </ol>
            </details>
            <details style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8 }}>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>SABnzbd Installation</summary>
              <ol style={{ margin: 0, paddingLeft: 20, lineHeight: 2 }}>
                <li>Save settings above and click <b>Download SABnzbd Script</b></li>
                <li>Place <code>shrinkerr.py</code> in your SABnzbd <b>scripts</b> folder</li>
                <li>In SABnzbd → Config → Categories, set <b>shrinkerr.py</b> as the post-processing script for your TV/Movie categories</li>
                <li>The script auto-configures from Shrinkerr — your URL and API key are baked in</li>
                <li>Add your configured tags to series in Sonarr / movies in Radarr</li>
              </ol>
            </details>
            <div style={{ marginTop: 8, padding: "8px 12px", background: "rgba(104,96,254,0.1)", borderRadius: 4, fontSize: 12 }}>
              <b style={{ color: "var(--accent)" }}>Tip:</b> Use <b>Encoding Rules</b> in Shrinkerr to set different conversion profiles (CQ, preset, audio codec) based on Sonarr/Radarr tags. Tag-based downloads will follow your encoding rules automatically.
            </div>
          </div>

          <h2 id="rules" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            Encoding Rules
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
                        if (res.labels_synced) parts.push(`${res.labels_synced} labels`);
                        if (res.collections_synced) parts.push(`${res.collections_synced} collections`);
                        if (res.genres_synced) parts.push(`${res.genres_synced} genres`);
                        if (res.libraries_synced) parts.push(`${res.libraries_synced} libraries`);
                        if (res.watch_synced) parts.push(`${res.watch_synced} watch status`);
                        toast(`Synced: ${parts.join(", ") || "no changes"}`, "success");
                      } catch (err: any) { toast(err.message || "Sync failed"); }
                      setRuleSyncing(false);
                    }}
                  >
                    {ruleSyncing ? "Syncing..." : "Sync from Plex"}
                  </button>
                )}
                <button className="btn btn-primary" style={{ fontSize: 11, padding: "4px 10px" }}
                  onClick={() => {
                    setShowAddRule(true);
                    setEditingRuleId(null);
                    setRuleForm({ name: "", match_mode: "any", conditions: [{ type: "directory", operator: "is", value: "" }], action: "encode", encoder: "", nvenc_preset: "", nvenc_cq: "", libx265_crf: "", libx265_preset: "", target_resolution: "", audio_codec: "", audio_bitrate: "", queue_priority: "" });
                    if (plexOpts.labels.length === 0) loadPlexOpts();
                  }}
                >+ Add Rule</button>
              </div>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14, lineHeight: 1.6 }}>
              Apply different encoding settings based on where files are located. Match by media directory, or connect
              Plex to match by label, collection, or library. Rules are evaluated top-to-bottom &mdash; the first match wins.
              Files with no matching rule use the global defaults below.
              {encoding?.plex_configured && <span> Click <b>Sync from Plex</b> after creating label/collection rules to build the folder lookup cache.</span>}
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
                      <span style={{ cursor: "grab", opacity: 0.3, fontSize: 14, flexShrink: 0 }} title="Drag to reorder">&#x2807;</span>
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
                              {ci > 0 && <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{rule.match_mode === "all" ? "and" : "or"}</span>}
                              <span style={{ fontSize: 9, padding: "1px 4px", borderRadius: 6, fontWeight: "bold", background: bg, color: fg }}>
                                {c.type === "directory" ? "dir" : c.type.replace("_", " ")}
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
                        {rule.action === "encode" ? "Encode" : rule.action === "skip" ? "Skip all" : "Audio/sub only"}
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
                            rule.queue_priority != null ? ["Normal", "High", "Highest"][rule.queue_priority] : null,
                          ].filter(Boolean).join(" ") || "defaults"}
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
                          title="Edit"
                        >&#9998;</button>
                        <button style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", padding: 2, fontSize: 12 }}
                          onClick={async () => {
                            await deleteEncodingRule(rule.id);
                            loadRules();
                          }}
                          title="Delete"
                        >&times;</button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {rules.length === 0 && !showAddRule && (
              <div style={{ textAlign: "center", padding: 20, opacity: 0.4, fontSize: 13 }}>
                No encoding rules yet. Add a rule to apply different settings per media directory{encoding?.plex_configured ? ", Plex label, collection, or library" : ""}.
              </div>
            )}

            {/* Add/Edit Rule form */}
            {showAddRule && (
              <div style={{ background: "var(--bg-primary)", borderRadius: 4, padding: 16, marginTop: rules.length > 0 ? 0 : 8 }}>
                {/* Rule name */}
                <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 12 }}>
                  <label style={labelStyle}>Name</label>
                  <input style={{ ...inputStyle, width: 300 }} value={ruleForm.name} placeholder="e.g. 4K Max Quality"
                    onChange={e => setRuleForm({ ...ruleForm, name: e.target.value })} />
                </div>

                {/* Match conditions */}
                <div style={{ marginBottom: 12 }}>
                  <label style={labelStyle}>Match conditions</label>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4, marginBottom: 12 }}>
                    <select value={ruleForm.match_mode} onChange={e => setRuleForm({...ruleForm, match_mode: e.target.value})}
                      style={{ ...inputStyle, width: 260, fontWeight: 500 }}>
                      <option value="any">Match any of the following</option>
                      <option value="all">Match all of the following</option>
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
                          <optgroup label="Path">
                            <option value="directory">Media Directory</option>
                          </optgroup>
                          <optgroup label="File">
                            <option value="source">Source</option>
                            <option value="resolution">Resolution</option>
                            <option value="video_codec">Video Codec</option>
                            <option value="audio_codec">Audio Codec</option>
                            <option value="file_size">File Size (GB)</option>
                            <option value="media_type">Type</option>
                            <option value="title">Title</option>
                            <option value="release_group">Release Group</option>
                          </optgroup>
                          <optgroup label="Plex">
                            <option value="label">Label</option>
                            <option value="collection">Collection</option>
                            <option value="genre">Genre</option>
                            <option value="library">Library</option>
                          </optgroup>
                          <optgroup label="Arr">
                            <option value="arr_tag">Sonarr/Radarr Tag</option>
                          </optgroup>
                          <optgroup label="Jellyfin">
                            <option value="jellyfin_tag">Jellyfin Tag</option>
                          </optgroup>
                          <optgroup label="Downloads">
                            <option value="nzbget_category">Download Category</option>
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
                              <option value="">Select directory...</option>
                              {dirs.map(d => <option key={d.path} value={d.path}>{d.label ? `${d.label} (${d.path})` : d.path}</option>)}
                            </select>;
                          }

                          if (cond.type === "source") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select...</option>
                              {(condOpts.sources || []).map((s: string) => <option key={s} value={s}>{s}</option>)}
                            </select>;
                          }

                          if (cond.type === "resolution") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select...</option>
                              {(condOpts.resolutions || []).map((r: string) => <option key={r} value={r}>{r}</option>)}
                            </select>;
                          }

                          if (cond.type === "video_codec") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select...</option>
                              {(condOpts.video_codecs || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                            </select>;
                          }

                          if (cond.type === "audio_codec") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select...</option>
                              {(condOpts.audio_codecs || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                            </select>;
                          }

                          if (cond.type === "media_type") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select...</option>
                              <option value="movie">Movie</option>
                              <option value="tv">TV Show</option>
                            </select>;
                          }

                          if (cond.type === "release_group") {
                            return <div style={{ display: "flex", gap: 6, flex: 1 }}>
                              <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                                <option value="">Select or type...</option>
                                {(condOpts.release_groups || []).map((g: string) => <option key={g} value={g}>{g}</option>)}
                              </select>
                              <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder="Or type group name..."
                                onChange={e => updateConditionValue(condIdx, e.target.value)} />
                            </div>;
                          }

                          if (cond.type === "label") {
                            return <div style={{ display: "flex", gap: 6, flex: 1 }}>
                              <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                                <option value="">Select...</option>
                                {(plexOpts.labels || []).map((l: string) => <option key={l} value={l}>{l}</option>)}
                              </select>
                              <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder="Or type manually..."
                                onChange={e => updateConditionValue(condIdx, e.target.value)} />
                            </div>;
                          }

                          if (cond.type === "collection") {
                            return <div style={{ display: "flex", gap: 6, flex: 1 }}>
                              <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                                <option value="">Select...</option>
                                {(plexOpts.collections || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                              </select>
                              <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder="Or type manually..."
                                onChange={e => updateConditionValue(condIdx, e.target.value)} />
                            </div>;
                          }

                          if (cond.type === "genre") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select...</option>
                              {(plexOpts.genres || []).map((g: string) => <option key={g} value={g}>{g}</option>)}
                            </select>;
                          }

                          if (cond.type === "library") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select...</option>
                              {(plexOpts.libraries || []).map((l: any) => <option key={l.title} value={l.title}>{l.title}</option>)}
                            </select>;
                          }

                          if (cond.type === "arr_tag") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select tag...</option>
                              {(condOpts.arr_tags || []).map((t: any) => <option key={`${t.source}-${t.label}`} value={t.label}>{t.label} ({t.source})</option>)}
                            </select>;
                          }

                          if (cond.type === "jellyfin_tag") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select tag...</option>
                              {(condOpts.jellyfin_tags || []).map((t: string) => <option key={t} value={t}>{t}</option>)}
                            </select>;
                          }

                          if (cond.type === "nzbget_category") {
                            return <select style={{ ...inputStyle, flex: 1 }} value={cond.value} onChange={e => updateConditionValue(condIdx, e.target.value)}>
                              <option value="">Select category...</option>
                              {(condOpts.nzbget_categories || []).map((c: string) => <option key={c} value={c}>{c}</option>)}
                            </select>;
                          }

                          if (ct.valueType === "number") {
                            return <input type="number" step="0.1" style={{ ...inputStyle, flex: 1 }} value={cond.value}
                              placeholder="Size in GB..." onChange={e => updateConditionValue(condIdx, e.target.value)} />;
                          }

                          // Default: text input
                          return <input style={{ ...inputStyle, flex: 1 }} value={cond.value} placeholder="Enter value..."
                            onChange={e => updateConditionValue(condIdx, e.target.value)} />;
                        })()}
                        {/* Remove button */}
                        {ruleForm.conditions.length > 1 && (
                          <button style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", fontSize: 14, padding: 2 }}
                            onClick={() => {
                              const updated = ruleForm.conditions.filter((_, i) => i !== condIdx);
                              setRuleForm({ ...ruleForm, conditions: updated });
                            }}
                            title="Remove condition">&times;</button>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 12 }}>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 12 }}>
                    <div>
                      <label style={labelStyle}>Action</label>
                      <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.action}
                        onChange={e => setRuleForm({ ...ruleForm, action: e.target.value })}>
                        <option value="encode">Encode (apply settings)</option>
                        <option value="ignore">Skip conversion (audio/sub cleanup only)</option>
                        <option value="skip">Skip entirely (do nothing)</option>
                      </select>
                    </div>
                    {ruleForm.action === "encode" && <>
                      <div>
                        <label style={labelStyle}>Encoder</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.encoder}
                          onChange={e => setRuleForm({ ...ruleForm, encoder: e.target.value })}>
                          <option value="">Use default</option>
                          <option value="nvenc">NVENC (GPU)</option>
                          <option value="libx265">libx265 (CPU)</option>
                        </select>
                      </div>
                      <div>
                        <label style={labelStyle}>Preset</label>
                        {ruleForm.encoder === "libx265" ? (
                          <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.libx265_preset}
                            onChange={e => setRuleForm({ ...ruleForm, libx265_preset: e.target.value })}>
                            <option value="">Use default</option>
                            {["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"].map(p => <option key={p} value={p}>{p}</option>)}
                          </select>
                        ) : (
                          <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.nvenc_preset}
                            onChange={e => setRuleForm({ ...ruleForm, nvenc_preset: e.target.value })}>
                            <option value="">Use default</option>
                            {["p1","p2","p3","p4","p5","p6","p7"].map(p => <option key={p} value={p}>{p.toUpperCase()}</option>)}
                          </select>
                        )}
                      </div>
                      <div>
                        <label style={labelStyle}>{ruleForm.encoder === "libx265" ? "CRF" : "CQ"}</label>
                        <input type="number" style={{ ...inputStyle, width: "100%" }}
                          value={ruleForm.encoder === "libx265" ? ruleForm.libx265_crf : ruleForm.nvenc_cq}
                          placeholder="Default" min={15} max={30}
                          onChange={e => {
                            if (ruleForm.encoder === "libx265") {
                              setRuleForm({ ...ruleForm, libx265_crf: e.target.value });
                            } else {
                              setRuleForm({ ...ruleForm, nvenc_cq: e.target.value });
                            }
                          }}
                        />
                      </div>
                      <div>
                        <label style={labelStyle}>Resolution</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.target_resolution}
                          onChange={e => setRuleForm({ ...ruleForm, target_resolution: e.target.value })}>
                          <option value="">Use default</option>
                          <option value="copy">Copy (keep original)</option>
                          <option value="1080p">1080p</option>
                          <option value="720p">720p</option>
                          <option value="480p">480p</option>
                        </select>
                      </div>
                    </>}
                    {ruleForm.action !== "skip" && <>
                      <div>
                        <label style={labelStyle}>Audio codec</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.audio_codec}
                          onChange={e => setRuleForm({ ...ruleForm, audio_codec: e.target.value })}>
                          <option value="">Use default</option>
                          <option value="copy">Copy (no conversion)</option>
                          <option value="eac3">EAC3</option>
                          <option value="ac3">AC3</option>
                          <option value="aac">AAC</option>
                          <option value="opus">Opus</option>
                          <option value="flac">FLAC</option>
                        </select>
                      </div>
                      <div>
                        <label style={labelStyle}>Audio bitrate</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.audio_bitrate}
                          onChange={e => setRuleForm({ ...ruleForm, audio_bitrate: e.target.value })}>
                          <option value="">Use default</option>
                          <option value="640">640k (Blu-ray)</option>
                          <option value="448">448k (streaming)</option>
                          <option value="256">256k (compact)</option>
                          <option value="128">128k (low)</option>
                        </select>
                      </div>
                    </>}
                    {ruleForm.action !== "skip" && (
                      <div>
                        <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Queue priority</label>
                        <select style={{ ...inputStyle, width: "100%" }} value={ruleForm.queue_priority}
                          onChange={e => setRuleForm({ ...ruleForm, queue_priority: e.target.value })}>
                          <option value="">Default</option>
                          <option value="0">Normal</option>
                          <option value="1">High</option>
                          <option value="2">Highest</option>
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
                        toast("Please enter a rule name");
                        return;
                      }
                      const validConditions = ruleForm.conditions.filter(c => c.value);
                      if (validConditions.length === 0) {
                        toast("Please add at least one match condition with a value");
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
                          toast("Rule updated", "success");
                        } else {
                          await createEncodingRule(data);
                          toast("Rule created", "success");
                        }
                        setShowAddRule(false);
                        setEditingRuleId(null);
                        // Small delay to let DB commit, then reload
                        setTimeout(loadRules, 200);
                      } catch (err: any) {
                        toast(err.message || "Failed to save rule");
                      }
                    }}
                  >
                    {editingRuleId ? "Update Rule" : "Add Rule"}
                  </button>
                  <button className="btn btn-secondary" style={{ fontSize: 12, padding: "4px 12px" }}
                    onClick={() => { setShowAddRule(false); setEditingRuleId(null); }}
                  >Cancel</button>
                </div>
              </div>
            )}
          </div>

          <h2 id="renaming" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            Renaming
          </h2>
          <RenamingSettings />

          <h2 id="automation" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            Automation
          </h2>
          {/* Automation — at the bottom */}
          <div style={sectionStyle}>
            <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>Auto-Queue</div>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input type="checkbox" checked={encoding?.auto_queue_new || false}
                onClick={() => setEncoding({ ...encoding, auto_queue_new: !encoding?.auto_queue_new })}
                readOnly
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Auto-queue new files</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26, marginBottom: 16 }}>
              New files detected in scanned folders by the watcher that need conversion or audio cleanup will be automatically added to the queue using your default encoding settings.
            </div>

            {/* Conversion filters */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>Conversion Filters</div>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Min file size (MB)</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.min_file_size_mb ?? 0}
                    onChange={e => setEncoding({ ...encoding, min_file_size_mb: e.target.value })} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Min bitrate (Mbps)</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.min_bitrate_mbps ?? 0}
                    onChange={e => setEncoding({ ...encoding, min_bitrate_mbps: e.target.value })} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Max bitrate (Mbps)</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.max_bitrate_mbps ?? 0}
                    onChange={e => setEncoding({ ...encoding, max_bitrate_mbps: e.target.value })} />
                </div>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8, lineHeight: 1.6 }}>
                Set to 0 to disable. Typical bitrates: DVD ~4-5 Mbps, 720p WEB ~3-5 Mbps, 1080p WEB ~5-10 Mbps, 1080p Blu-ray ~20-40 Mbps, 4K SDR ~40-60 Mbps, 4K HDR Remux ~60-100+ Mbps. Set min bitrate to 3 to skip already-compressed files. Set max bitrate to 80 to preserve 4K HDR remuxes.
              </div>
            </div>

            {/* Output Filename */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>Output Filename</div>
              <div style={labelStyle}>Filename suffix after conversion</div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
                <input type="text" style={{ ...inputStyle, flex: "1 1 200px", maxWidth: 300 }}
                  value={encoding?.filename_suffix ?? ""}
                  onChange={e => setEncoding({ ...encoding, filename_suffix: e.target.value })}
                  placeholder="e.g. -Shrinkerr" />
                {!encoding?.filename_suffix && (
                  <button className="btn btn-secondary" style={{ fontSize: 11, padding: "5px 10px", whiteSpace: "nowrap" }}
                    onClick={() => setEncoding({ ...encoding, filename_suffix: "-Shrinkerr" })}>
                    Use "-Shrinkerr"
                  </button>
                )}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                Appended to the filename before the extension after conversion.
                Example: <code style={{ fontSize: 10, padding: "1px 4px", background: "var(--bg-primary)", borderRadius: 2 }}>Movie x265{encoding?.filename_suffix || ""}.mkv</code>.
                Leave empty for no suffix.
              </div>
            </div>

            {/* Originals & Backups */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 12 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>Originals & Backups</div>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input type="checkbox" checked={encoding?.trash_original_after_conversion || false}
                onChange={() => setEncoding({ ...encoding, trash_original_after_conversion: !encoding?.trash_original_after_conversion })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Move originals to trash after conversion</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26, marginBottom: 12 }}>
              After a successful conversion, the original file is moved to the system trash instead of being permanently deleted.
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, paddingLeft: 26, marginBottom: 10 }}>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Or backup originals for</span>
              <input type="number" min={0} style={{ ...inputStyle, width: 60 }}
                value={encoding?.backup_original_days ?? 0}
                onChange={e => setEncoding({ ...encoding, backup_original_days: e.target.value })} />
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>days</span>
              <span style={{ fontSize: 11, color: "var(--text-muted)", opacity: 0.6 }}>(0 = disabled, kept indefinitely when blank)</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, paddingLeft: 26, marginBottom: 10 }}>
              <span style={{ fontSize: 12, color: "var(--text-muted)", flexShrink: 0 }}>Backup folder</span>
              <input type="text" style={{ ...inputStyle, flex: 1 }}
                value={encoding?.backup_folder ?? ""}
                onChange={e => setEncoding({ ...encoding, backup_folder: e.target.value })}
                placeholder=".shrinkerr_backup (default, same dir as file)" />
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "5px 10px", whiteSpace: "nowrap", flexShrink: 0 }}
                onClick={() => setBackupBrowserOpen(true)}
              >Browse</button>
            </div>
            <FolderBrowser
              isOpen={backupBrowserOpen}
              initialPath={encoding?.backup_folder || "/media"}
              onSelect={(path) => { setEncoding({ ...encoding, backup_folder: path }); setBackupBrowserOpen(false); }}
              onCancel={() => setBackupBrowserOpen(false)}
            />
            <div style={{ fontSize: 11, color: "var(--text-muted)", paddingLeft: 26, marginBottom: 6 }}>
              Leave empty to use <code style={{ fontSize: 10, padding: "1px 4px", background: "var(--bg-primary)", borderRadius: 2 }}>.shrinkerr_backup</code> in the same directory.
              Set an absolute path (e.g. <code style={{ fontSize: 10, padding: "1px 4px", background: "var(--bg-primary)", borderRadius: 2 }}>/media/backups</code>) for centralized storage.
              Backup files are required for the <strong>Undo Conversion</strong> feature — without backups, conversions cannot be reverted.
            </div>
            <div style={{ paddingLeft: 26, marginBottom: 16 }}>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "4px 12px", borderRadius: 4, color: "#e94560" }}
                onClick={async () => {
                  const { getBackups, deleteBackups } = await import("../api");
                  const data = await getBackups();
                  if (data.total_count === 0) {
                    alert("No backup files found.");
                    return;
                  }
                  const sizeGB = (data.total_size / (1024 ** 3)).toFixed(1);
                  if (confirm(`Delete all ${data.total_count} backup file(s) (${sizeGB} GB)? This cannot be undone.`)) {
                    const result = await deleteBackups();
                    alert(`Deleted ${result.deleted} file(s), freed ${(result.freed / (1024 ** 3)).toFixed(1)} GB`);
                  }
                }}
              >
                Delete all backups
              </button>
            </div>

            {/* File Stability */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 12 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>File Stability</div>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input type="checkbox" checked={encoding?.skip_files_newer_enabled || false}
                onChange={() => setEncoding({ ...encoding, skip_files_newer_enabled: !encoding?.skip_files_newer_enabled })}
                style={{ flexShrink: 0 }} />
              <span style={labelStyle}>Delay recently modified files</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 26 }}>
              Files modified within this window are postponed during scanning and auto-queue. They'll be picked up on the next scan once the window has passed. This prevents converting files that are still being downloaded, transferred, or processed by other tools like Sonarr/Radarr. Recommended for shared systems.
            </div>
            {encoding?.skip_files_newer_enabled && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, paddingLeft: 26, marginTop: 8 }}>
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Delay files newer than</span>
                <input type="number" min={1} max={1440}
                  style={{ ...inputStyle, width: 70 }}
                  value={encoding?.skip_files_newer_than_minutes ?? 10}
                  onChange={e => setEncoding({ ...encoding, skip_files_newer_than_minutes: e.target.value })} />
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>minutes</span>
              </div>
            )}

            {/* Health Checks */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 12 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>Health Checks</div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <span style={{ ...labelStyle, flex: "0 0 240px" }}>Auto-check after scans</span>
              <select
                style={{ ...inputStyle, width: 140 }}
                value={encoding?.health_check_on_scan ?? "off"}
                onChange={e => setEncoding({ ...encoding, health_check_on_scan: e.target.value })}
              >
                <option value="off">Off</option>
                <option value="quick">Quick</option>
                <option value="thorough">Thorough</option>
              </select>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 0, marginBottom: 8 }}>
              After each scan, queue a health check for files <strong>newly detected in the last 24h</strong> (capped at 1000 per scan to protect the queue).
              <strong> Quick</strong> parses headers (&lt;1s per file). <strong>Thorough</strong> decodes every frame (slow — minutes per file).
              To check files retroactively, use the manual <strong>Quick check</strong> / <strong>Thorough check</strong> buttons on the Scanner.
            </div>
            <div style={{ marginBottom: 14, paddingLeft: 0 }}>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "4px 10px", color: "#e94560", borderColor: "rgba(233,69,96,0.4)" }}
                onClick={async () => {
                  if (!confirm("Delete ALL pending health-check jobs? Running jobs and other job types are not affected.")) return;
                  const { clearPendingHealthChecks } = await import("../api");
                  const res = await clearPendingHealthChecks();
                  toast(`Removed ${res.deleted.toLocaleString()} pending health-check job${res.deleted === 1 ? "" : "s"}`, "success");
                }}
              >Clear pending health-check jobs</button>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
              <span style={{ ...labelStyle, flex: "0 0 240px" }}>Auto-check after conversion</span>
              <select
                style={{ ...inputStyle, width: 140 }}
                value={encoding?.health_check_after_conversion ?? "off"}
                onChange={e => setEncoding({ ...encoding, health_check_after_conversion: e.target.value })}
              >
                <option value="off">Off</option>
                <option value="quick">Quick</option>
                <option value="thorough">Thorough</option>
              </select>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, paddingLeft: 0, marginBottom: 12 }}>
              Verify each converted output. <strong>Quick</strong> confirms the container/streams parse. <strong>Thorough</strong> catches visual artifacts (decoder errors) at the cost of roughly duration/10 per file.
            </div>

            {/* Advanced */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 16, marginTop: 8, marginBottom: 16 }}>
              <div style={{ ...labelStyle, fontWeight: 600, marginBottom: 10 }}>Advanced</div>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 10 }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 300px" }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Custom ffmpeg flags</label>
                  <input type="text" style={{ ...inputStyle, width: "100%" }}
                    placeholder="e.g. -movflags +faststart"
                    value={encoding?.custom_ffmpeg_flags ?? ""}
                    onChange={e => setEncoding({ ...encoding, custom_ffmpeg_flags: e.target.value })} />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <label style={{ fontSize: 11, color: "var(--text-muted)" }}>Max Plex API calls</label>
                  <input type="number" min={0} style={{ ...inputStyle, width: 90 }}
                    value={encoding?.max_plex_api_calls ?? 0}
                    onChange={e => setEncoding({ ...encoding, max_plex_api_calls: e.target.value })} />
                </div>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
                Custom flags are inserted before the output path in the ffmpeg command. Max Plex API calls limits concurrent requests to your Plex server (0 = unlimited).
              </div>
            </div>

            <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ marginTop: 16 }}>
              Save
            </button>
          </div>

          {/* Webhook Endpoints */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>Webhook Endpoints</h3>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              External tools can call these endpoints to control Shrinkerr. Authenticate with <code style={{ color: "var(--accent)" }}>?api_key=YOUR_KEY</code> or <code style={{ color: "var(--accent)" }}>X-Api-Key</code> header.
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {[
                { method: "POST", path: "/api/webhooks/scan", desc: "Trigger library scan" },
                { method: "POST", path: "/api/webhooks/queue", desc: "Add files to queue (body: {paths: [...]})" },
                { method: "POST", path: "/api/webhooks/pause", desc: "Pause the queue" },
                { method: "POST", path: "/api/webhooks/resume", desc: "Resume the queue" },
                { method: "GET", path: "/api/webhooks/status", desc: "Get current status" },
              ].map(ep => (
                <div key={ep.path} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                  <span style={{ color: ep.method === "GET" ? "#40ceff" : "var(--success)", fontWeight: 600, width: 40, flexShrink: 0 }}>{ep.method}</span>
                  <code style={{ color: "var(--text-secondary)", flex: 1 }}>{ep.path}</code>
                  <span style={{ color: "var(--text-muted)", fontSize: 11 }}>{ep.desc}</span>
                  <button
                    title="Copy full URL"
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
                      toast("URL copied", "success");
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
            <h3 style={{ color: "white", marginBottom: 12 }}>Post-Conversion Script</h3>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              Run a custom script after each conversion completes. The script receives job details as environment variables.
            </div>
            <div style={{ marginBottom: 12 }}>
              <div style={labelStyle}>Script path</div>
              <input type="text" style={{ ...inputStyle, width: "100%", maxWidth: 500 }}
                value={encoding?.post_conversion_script || ""}
                onChange={e => setEncoding({ ...encoding, post_conversion_script: e.target.value })}
                placeholder="/path/to/script.sh" />
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                Absolute path to an executable script inside the container. Leave empty to disable.
              </div>
            </div>
            <div style={{ marginBottom: 12 }}>
              <div style={labelStyle}>Timeout (seconds)</div>
              <input type="number" style={{ ...inputStyle, width: 100 }}
                value={encoding?.post_conversion_script_timeout || 300}
                onChange={e => setEncoding({ ...encoding, post_conversion_script_timeout: parseInt(e.target.value) || 300 })} />
            </div>
            <details style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              <summary style={{ cursor: "pointer", color: "var(--text-secondary)", marginBottom: 8 }}>Environment Variables Reference</summary>
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
                  "SHRINKERR_ERROR=(error message if failed)",
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
                toast("Post-conversion script settings saved", "success");
              }}>Save</button>
          </div>

          <h2 id="system" style={{ color: "var(--text-primary)", fontSize: 18, marginTop: 24, marginBottom: 12, scrollMarginTop: 20 }}>
            System
          </h2>
          {/* Authentication */}
          <div style={sectionStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ color: "white", margin: 0 }}>Authentication</h3>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{encoding?.auth_enabled ? "Enabled" : "Disabled"}</span>
                <input type="checkbox" checked={encoding?.auth_enabled || false}
                  onChange={() => setEncoding({ ...encoding, auth_enabled: !encoding?.auth_enabled })}
                  style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
              </label>
            </div>

            {encoding?.auth_enabled && (
              <>
                <div style={{ marginBottom: 12 }}>
                  <div style={labelStyle}>Username</div>
                  <input type="text" style={{ ...inputStyle, maxWidth: 300 }}
                    value={encoding?.auth_username || ""}
                    onChange={e => setEncoding({ ...encoding, auth_username: e.target.value })}
                    placeholder="admin" />
                </div>
                <div style={{ marginBottom: 12 }}>
                  <div style={labelStyle}>Password</div>
                  <input type="password" style={{ ...inputStyle, maxWidth: 300 }}
                    value={encoding?.auth_password || ""}
                    onChange={e => setEncoding({ ...encoding, auth_password: e.target.value })}
                    placeholder="Enter new password..." />
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Leave empty to keep current password</div>
                </div>
              </>
            )}

            {/* API Key section — always visible */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 12, marginTop: 12 }}>
              <div style={labelStyle}>API Key</div>
              <div style={{ display: "flex", gap: 0, alignItems: "center", marginBottom: 4, maxWidth: 500 }}>
                <input type="text" readOnly
                  value={encoding?.api_key || ""}
                  style={{
                    ...inputStyle, flex: 1, fontFamily: "monospace", fontSize: 13, letterSpacing: 0.5,
                    borderRadius: "4px 0 0 4px", borderRight: "none",
                  }} />
                <button
                  title="Copy to clipboard"
                  onClick={() => {
                    const text = encoding?.api_key || "";
                    if (navigator.clipboard?.writeText) {
                      navigator.clipboard.writeText(text).then(() => toast("API key copied", "success")).catch(() => {
                        // Fallback for non-HTTPS
                        const ta = document.createElement("textarea");
                        ta.value = text;
                        ta.style.position = "fixed";
                        ta.style.opacity = "0";
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand("copy");
                        document.body.removeChild(ta);
                        toast("API key copied", "success");
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
                      toast("API key copied", "success");
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
                  title="Regenerate API key"
                  onClick={() => {
                    const key = crypto.randomUUID().replace(/-/g, "");
                    setEncoding({ ...encoding, api_key: key });
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
                Used by NZBGet, SABnzbd, and other external integrations. Not required for browser login.
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
                toast("Authentication settings saved", "success");
              }}>Save Authentication</button>
          </div>

          {/* Notifications */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>Notifications</h3>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
              Get notified when the queue completes, a job fails, or disk space is low.
            </div>

            {/* Event toggles */}
            <div style={{ display: "flex", gap: 24, marginBottom: 16, flexWrap: "wrap" }}>
              {[
                ["notify_queue_complete", "Queue complete"],
                ["notify_job_failed", "Job failed"],
                ["notify_disk_low", "Disk space low"],
              ].map(([key, label]) => (
                <label key={key} style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13 }}>
                  <input type="checkbox" checked={encoding?.[key] ?? false}
                    onChange={e => setEncoding({ ...encoding, [key]: e.target.checked })}
                    style={{ accentColor: "var(--accent)" }} />
                  <span style={{ color: "var(--text-secondary)" }}>{label}</span>
                </label>
              ))}
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <label style={{ ...labelStyle, margin: 0 }}>Disk threshold (GB)</label>
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
                <label style={labelStyle}>Webhook URL</label>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="https://discord.com/api/webhooks/..."
                  value={encoding?.discord_webhook_url || ""}
                  onChange={e => setEncoding({ ...encoding, discord_webhook_url: e.target.value })} />
              </div>

              {/* Telegram */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>Telegram</div>
                <div style={{ display: "flex", gap: 8 }}>
                  <div style={{ flex: 1 }}>
                    <label style={labelStyle}>Bot Token</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="123456:ABC-DEF..."
                      value={encoding?.telegram_bot_token || ""}
                      onChange={e => setEncoding({ ...encoding, telegram_bot_token: e.target.value })} />
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={labelStyle}>Chat ID</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="-100123456789"
                      value={encoding?.telegram_chat_id || ""}
                      onChange={e => setEncoding({ ...encoding, telegram_chat_id: e.target.value })} />
                  </div>
                </div>
              </div>

              {/* Email */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>Email (SMTP)</div>
                <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 8, marginBottom: 8 }}>
                  <div>
                    <label style={labelStyle}>SMTP Host</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="smtp.gmail.com"
                      value={encoding?.smtp_host || ""}
                      onChange={e => setEncoding({ ...encoding, smtp_host: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>Port</label>
                    <input style={{ ...inputStyle, width: "100%" }} placeholder="587"
                      value={encoding?.smtp_port || "587"}
                      onChange={e => setEncoding({ ...encoding, smtp_port: e.target.value })} />
                  </div>
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 8 }}>
                  <div>
                    <label style={labelStyle}>Username</label>
                    <input style={{ ...inputStyle, width: "100%" }}
                      value={encoding?.smtp_user || ""}
                      onChange={e => setEncoding({ ...encoding, smtp_user: e.target.value })} />
                  </div>
                  <div>
                    <label style={labelStyle}>Password</label>
                    <input type="password" style={{ ...inputStyle, width: "100%" }}
                      value={encoding?.smtp_pass || ""}
                      onChange={e => setEncoding({ ...encoding, smtp_pass: e.target.value })} />
                  </div>
                </div>
                <label style={labelStyle}>Send to</label>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="you@email.com"
                  value={encoding?.email_to || ""}
                  onChange={e => setEncoding({ ...encoding, email_to: e.target.value })} />
              </div>

              {/* Generic Webhook */}
              <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 4 }}>
                <div style={{ fontSize: 13, fontWeight: 500, color: "white", marginBottom: 8 }}>Generic Webhook</div>
                <label style={labelStyle}>URL (receives JSON POST)</label>
                <input style={{ ...inputStyle, width: "100%" }} placeholder="https://your-server.com/webhook"
                  value={encoding?.webhook_url || ""}
                  onChange={e => setEncoding({ ...encoding, webhook_url: e.target.value })} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  Payload: {"{ event, title, message, fields }"}
                </div>
              </div>
            </div>

            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  await updateEncodingSettings(encoding);
                  toast("Notification settings saved", "success");
                }}
              >Save Notification Settings</button>
              <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
                onClick={async () => {
                  await updateEncodingSettings(encoding);
                  const res = await testNotifications();
                  const results = res.results || {};
                  const ok = Object.entries(results).filter(([, v]) => v).map(([k]) => k);
                  const fail = Object.entries(results).filter(([, v]) => !v).map(([k]) => k);
                  if (ok.length > 0) toast(`Test sent: ${ok.join(", ")}`, "success");
                  if (fail.length > 0) toast(`Failed: ${fail.join(", ")}`);
                  if (ok.length === 0 && fail.length === 0) toast("No notification providers configured");
                }}
              >Test Notifications</button>
            </div>
          </div>

          {/* Backups */}
          <div style={sectionStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ color: "white", margin: 0 }}>Backups</h3>
              <button className="btn btn-primary" style={{ fontSize: 11, padding: "4px 12px" }}
                disabled={backupCreating}
                onClick={async () => {
                  setBackupCreating(true);
                  try {
                    await createBackup();
                    toast("Backup created", "success");
                    loadBackups();
                  } catch { toast("Backup failed"); }
                  setBackupCreating(false);
                }}>
                {backupCreating ? "Creating..." : "Backup Now"}
              </button>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
              Backups include the full database (scan results, jobs, settings, rules) as a zip file.
            </div>
            {backupList.length === 0 ? (
              <div style={{ textAlign: "center", padding: 20, color: "var(--text-muted)", fontSize: 12, opacity: 0.6 }}>
                No backups yet
              </div>
            ) : (
              <div style={{ borderRadius: 4, overflow: "hidden" }}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 100px 120px 60px", gap: 0, padding: "6px 12px", fontSize: 11, fontWeight: 600, color: "var(--text-muted)", borderBottom: "1px solid var(--border)" }}>
                  <span>Name</span><span>Size</span><span>Time</span><span></span>
                </div>
                {backupList.map(b => (
                  <div key={b.name} style={{ display: "grid", gridTemplateColumns: "1fr 100px 120px 60px", gap: 0, padding: "8px 12px", fontSize: 12, borderBottom: "1px solid var(--bg-primary)", alignItems: "center" }}>
                    <a href={downloadBackupUrl(b.name)} download style={{ color: "var(--accent)", textDecoration: "none", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {b.name}
                    </a>
                    <span style={{ color: "var(--text-muted)" }}>{(b.size / (1024 * 1024)).toFixed(1)} MiB</span>
                    <span style={{ color: "var(--text-muted)" }}>
                      {new Date(b.created_at).toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "numeric" })}
                    </span>
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <button title="Restore" style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 2, display: "inline-flex", alignItems: "center" }}
                        onClick={async () => {
                          if (!confirm(`Restore from ${b.name}? This will replace your current database. A safety backup will be created first.`)) return;
                          try {
                            const resp = await fetch(downloadBackupUrl(b.name));
                            const blob = await resp.blob();
                            const file = new File([blob], b.name, { type: "application/zip" });
                            await restoreBackup(file);
                            toast("Backup restored. Restart the container for full effect.", "success");
                          } catch { toast("Restore failed"); }
                        }}>
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 105.64-11.36L1 10"/>
                        </svg>
                      </button>
                      <button title="Delete" style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", padding: 2, display: "inline-flex", alignItems: "center", opacity: 0.6 }}
                        onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                        onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.6")}
                        onClick={async () => {
                          if (!confirm(`Delete backup ${b.name}?`)) return;
                          try {
                            await deleteBackup(b.name);
                            loadBackups();
                            toast("Backup deleted");
                          } catch { toast("Delete failed"); }
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
                    if (!confirm(`Restore from uploaded file ${file.name}? This will replace your current database.`)) { e.target.value = ""; return; }
                    try {
                      await restoreBackup(file);
                      toast("Backup restored. Restart the container for full effect.", "success");
                      loadBackups();
                    } catch { toast("Restore failed"); }
                    e.target.value = "";
                  }} />
                <span style={{ border: "1px solid var(--border)", padding: "4px 10px", borderRadius: 4, cursor: "pointer" }}>
                  Restore from file...
                </span>
              </label>
            </div>
          </div>

          {/* User Interface */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>User Interface</h3>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>Theme</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                  Switch between dark and light mode
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
                {theme === "dark" ? "Light mode" : "Dark mode"}
              </button>
            </div>
          </div>

          {/* Keyboard Shortcuts */}
          <div style={sectionStyle}>
            <h3 style={{ color: "white", marginBottom: 12 }}>Keyboard Shortcuts</h3>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {[
                ["D", "Dashboard"],
                ["S", "Scanner"],
                ["Q", "Queue"],
                ["T", "Statistics"],
                ["L", "Logs"],
                ["H", "Schedule"],
                ["E", "Settings"],
                ["Space", "Start / Pause queue"],
              ].map(([key, action]) => (
                <div key={key} style={{ display: "flex", alignItems: "center", gap: 10, padding: "4px 0" }}>
                  <kbd style={{
                    background: "var(--bg-primary)", border: "1px solid var(--border)",
                    borderRadius: 4, padding: "2px 8px", fontSize: 12, fontFamily: "var(--font-mono)",
                    color: "var(--accent)", minWidth: 36, textAlign: "center", fontWeight: 600,
                  }}>{key}</kbd>
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{action}</span>
                </div>
              ))}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 10, opacity: 0.6 }}>
              Shortcuts are disabled when typing in input fields.
            </div>
          </div>
        </>
      )}
    </div>
  );
}
