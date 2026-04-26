import { useState, useEffect, useRef } from "react";
import {
  getRenameSettings, saveRenameSettings, getRenameTokens,
  previewRenamePattern,
} from "../api";
import type { RenameSettings, RenameTokenCategory } from "../api";
import { useToast } from "../useToast";

const sectionStyle: React.CSSProperties = {
  background: "var(--bg-card)",
  padding: 20,
  borderRadius: 6,
  marginBottom: 12,
};

const labelStyle: React.CSSProperties = {
  fontSize: 12,
  color: "var(--text-secondary)",
  fontWeight: 600,
  marginBottom: 6,
};

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 10px", fontSize: 13,
  background: "var(--bg-primary)", border: "1px solid var(--border)",
  borderRadius: 4, color: "var(--text-primary)", fontFamily: "var(--font-mono)",
};

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  fontFamily: "inherit",
  appearance: "none",
  backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath fill='%23999' d='M0 0l5 6 5-6z'/%3E%3C/svg%3E")`,
  backgroundRepeat: "no-repeat",
  backgroundPosition: "right 10px center",
  paddingRight: 26,
};

export default function RenamingSettings() {
  const toast = useToast();
  const [settings, setSettings] = useState<RenameSettings | null>(null);
  const [tokens, setTokens] = useState<RenameTokenCategory[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  // Active input for click-to-insert — which pattern field is focused
  const [activeField, setActiveField] = useState<"movie_file_pattern" | "tv_file_pattern" | "movie_folder_pattern" | "tv_folder_pattern" | "season_folder_pattern">("movie_file_pattern");
  const refs = {
    movie_file_pattern: useRef<HTMLInputElement>(null),
    tv_file_pattern: useRef<HTMLInputElement>(null),
    movie_folder_pattern: useRef<HTMLInputElement>(null),
    tv_folder_pattern: useRef<HTMLInputElement>(null),
    season_folder_pattern: useRef<HTMLInputElement>(null),
  };

  // Live preview for each pattern
  const [previews, setPreviews] = useState<Record<string, string>>({});

  useEffect(() => {
    Promise.all([getRenameSettings(), getRenameTokens()])
      .then(([s, t]) => {
        setSettings(s);
        setTokens(t.categories);
      })
      .finally(() => setLoading(false));
  }, []);

  // Debounced live preview when settings change
  useEffect(() => {
    if (!settings) return;
    const t = setTimeout(async () => {
      try {
        const fields: (keyof RenameSettings)[] = [
          "movie_file_pattern", "tv_file_pattern",
          "movie_folder_pattern", "tv_folder_pattern", "season_folder_pattern",
        ];
        const next: Record<string, string> = {};
        for (const f of fields) {
          const pattern = settings[f] as string;
          // Pick a sample that matches the pattern's expected context
          const isTv = f.startsWith("tv") || f.startsWith("season");
          const sample = isTv
            ? "/media/TV/Firefly (2002) [tvdb-78874]/Season 01/Firefly.S01E01.Serenity.2002.1080p.BluRay.HDR.x265.DTS.5.1-DiMEPiECE.mkv"
            : "/media/Movies/Dragonfly (2002) [tmdb-10497]/Dragonfly.2002.1080p.BluRay.HDR.x265.DTS.5.1-DiMEPiECE.mkv";
          const res = await previewRenamePattern(pattern, sample, settings);
          next[f] = res.rendered;
        }
        setPreviews(next);
      } catch {}
    }, 300);
    return () => clearTimeout(t);
  }, [settings]);

  if (loading || !settings) {
    return (
      <div style={sectionStyle}>
        <div className="spinner" style={{ width: 16, height: 16 }} />
      </div>
    );
  }

  const update = (patch: Partial<RenameSettings>) => {
    setSettings({ ...settings, ...patch });
  };

  const save = async () => {
    if (!settings) return;
    setSaving(true);
    try {
      const updated = await saveRenameSettings(settings);
      setSettings(updated);
      toast("Renaming settings saved", "success");
    } catch (e: any) {
      toast(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const insertToken = (token: string) => {
    const ref = refs[activeField].current;
    if (!ref) return;
    const start = ref.selectionStart ?? ref.value.length;
    const end = ref.selectionEnd ?? ref.value.length;
    const before = ref.value.slice(0, start);
    const after = ref.value.slice(end);
    const insert = `{${token}}`;
    const newValue = before + insert + after;
    update({ [activeField]: newValue } as any);
    // Restore focus + cursor position after state update
    requestAnimationFrame(() => {
      ref.focus();
      const pos = start + insert.length;
      ref.setSelectionRange(pos, pos);
    });
  };

  const field = (
    label: string,
    key: keyof RenameSettings,
  ) => (
    <div style={{ marginBottom: 16 }}>
      <div style={labelStyle}>{label}</div>
      <input
        type="text"
        ref={refs[key as keyof typeof refs] as any}
        value={(settings[key] ?? "") as string}
        onChange={e => update({ [key]: e.target.value } as any)}
        onFocus={() => setActiveField(key as any)}
        style={inputStyle}
      />
      <div style={{
        marginTop: 4, fontSize: 11, color: "var(--text-muted)",
        fontFamily: "var(--font-mono)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
      }}>
        → {previews[key as string] || <i style={{ opacity: 0.5 }}>loading preview…</i>}
      </div>
    </div>
  );

  return (
    <div>
      {/* Token picker */}
      <div style={{ ...sectionStyle, position: "sticky", top: 0, zIndex: 5 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 8 }}>
          Tokens — click to insert into: <span style={{ color: "var(--accent)" }}>{
            activeField.replace(/_/g, " ")
          }</span>
        </div>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
          Focus any pattern field below, then click a token to insert it at the cursor. Add <code>:00</code> to numbers for zero-padding, e.g. <code>{"{season:00}"}</code>.
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {tokens.map(cat => (
            <div key={cat.category}>
              <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4 }}>
                {cat.category}
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {cat.tokens.map(t => (
                  <button
                    key={t.token}
                    onClick={() => insertToken(t.token)}
                    title={`${t.desc} (e.g. "${t.example}")`}
                    style={{
                      padding: "3px 8px", fontSize: 11, fontFamily: "var(--font-mono)",
                      background: "var(--bg-primary)", border: "1px solid var(--border)",
                      borderRadius: 12, cursor: "pointer", color: "var(--accent)",
                    }}
                  >{`{${t.token}}`}</button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Toggles */}
      <div style={sectionStyle}>
        <h3 style={{ color: "white", marginTop: 0, marginBottom: 16, fontSize: 14 }}>Behavior</h3>
        <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 12 }}>
          <input type="checkbox" checked={settings.enabled_auto} onChange={e => update({ enabled_auto: e.target.checked })} />
          <div>
            <div style={{ fontSize: 12, color: "var(--text-primary)" }}>Auto-rename after conversion</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>When enabled, Shrinkerr applies these patterns automatically after each successful conversion.</div>
          </div>
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 12 }}>
          <input type="checkbox" checked={settings.rename_folders} onChange={e => update({ rename_folders: e.target.checked })} />
          <div>
            <div style={{ fontSize: 12, color: "var(--text-primary)" }}>Also rename folders</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Rename the movie/series/season folder using the folder patterns below (disabled by default — safer).</div>
          </div>
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
          <input type="checkbox" checked={settings.remove_illegal} onChange={e => update({ remove_illegal: e.target.checked })} />
          <div>
            <div style={{ fontSize: 12, color: "var(--text-primary)" }}>Remove illegal characters</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Strip characters that break filesystems: &lt; &gt; : " | ? * and control chars.</div>
          </div>
        </label>
      </div>

      {/* Formatting */}
      <div style={sectionStyle}>
        <h3 style={{ color: "white", marginTop: 0, marginBottom: 16, fontSize: 14 }}>Formatting</h3>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <div>
            <div style={labelStyle}>Separator</div>
            <select style={selectStyle} value={settings.separator} onChange={e => update({ separator: e.target.value as any })}>
              <option value="space">Spaces (default)</option>
              <option value="dot">Dots (.)</option>
              <option value="dash">Dashes (-)</option>
              <option value="underscore">Underscores (_)</option>
            </select>
          </div>
          <div>
            <div style={labelStyle}>Case</div>
            <select style={selectStyle} value={settings.case_mode} onChange={e => update({ case_mode: e.target.value as any })}>
              <option value="default">Default (as-is)</option>
              <option value="lower">lowercase</option>
              <option value="upper">UPPERCASE</option>
            </select>
          </div>
        </div>
      </div>

      {/* Patterns */}
      <div style={sectionStyle}>
        <h3 style={{ color: "white", marginTop: 0, marginBottom: 16, fontSize: 14 }}>Movie patterns</h3>
        {field("Movie file", "movie_file_pattern")}
        {settings.rename_folders && field("Movie folder", "movie_folder_pattern")}
      </div>

      <div style={sectionStyle}>
        <h3 style={{ color: "white", marginTop: 0, marginBottom: 16, fontSize: 14 }}>TV patterns</h3>
        {field("Episode file", "tv_file_pattern")}
        {settings.rename_folders && field("Series folder", "tv_folder_pattern")}
        {settings.rename_folders && field("Season folder", "season_folder_pattern")}
      </div>

      {/* Left-align the Save button to match every other section in
          Settings (encoding, audio, lossless, etc. all use
          `alignSelf: "flex-start"`). v0.3.48+. */}
      <div style={{ display: "flex", justifyContent: "flex-start", marginTop: 20 }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving..." : "Save Renaming Settings"}
        </button>
      </div>
    </div>
  );
}
