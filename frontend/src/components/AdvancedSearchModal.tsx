import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { advancedSearch, getSearchProperties, type SearchProperty, type SearchPredicate } from "../api";
import { serverText } from "../i18n/server";

// Symbol operators render as-is; word operators are looked up under
// scannerModals:operators.<op> at render time.
const OP_SYMBOLS: Record<string, string> = {
  gt: ">",
  gte: "≥",
  lt: "<",
  lte: "≤",
};
const OP_WORDS = new Set(["eq", "ne", "between", "in", "contains", "regex", "exists"]);

// Server property groups -> serverSearch:groups.<slug>. Unknown groups fall
// back to the server's English name.
const GROUP_SLUGS: Record<string, string> = {
  Video: "video",
  Size: "size",
  Audio: "audio",
  Subtitles: "subtitles",
  Filename: "filename",
  State: "state",
  Type: "type",
  Other: "other",
};

const propLabel = (key: string, p: SearchProperty) =>
  serverText("serverSearch", `properties.${key}.label`, null, p.label);
const groupLabel = (group: string) =>
  serverText("serverSearch", GROUP_SLUGS[group] ? `groups.${GROUP_SLUGS[group]}` : null, null, group);
// Only word-like options have catalog entries; codec/language tokens and
// numbers fall back to the server label (or the raw value).
const optionLabel = (propKey: string, p: SearchProperty, value: string) =>
  serverText("serverSearch", `properties.${propKey}.options.${value}`, null, p.option_labels?.[value] ?? value);

const SAVED_VIEWS_KEY = "shrinkerr_saved_search_views";

interface SavedView { name: string; predicates: SearchPredicate[]; }

function loadSavedViews(): SavedView[] {
  try {
    return JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || "[]");
  } catch {
    return [];
  }
}
function persistSavedViews(views: SavedView[]) {
  localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views));
}

interface Props {
  initial: SearchPredicate[];
  onApply: (predicates: SearchPredicate[], filePaths: string[]) => void;
  onClose: () => void;
}

export default function AdvancedSearchModal({ initial, onApply, onClose }: Props) {
  const { t } = useTranslation(["scannerModals", "common"]);
  const [props, setProps] = useState<Record<string, SearchProperty>>({});
  const [predicates, setPredicates] = useState<SearchPredicate[]>(
    initial.length ? initial : [{ property: "video_codec", op: "eq", value: "" }],
  );
  const [matchMode, setMatchMode] = useState<"all" | "any">("all");
  const [previewing, setPreviewing] = useState(false);
  const [previewCount, setPreviewCount] = useState<number | null>(null);
  const [previewCap, setPreviewCap] = useState<number>(0);
  const [savedViews, setSavedViews] = useState<SavedView[]>(loadSavedViews());
  const [newViewName, setNewViewName] = useState("");

  useEffect(() => {
    getSearchProperties().then(setProps).catch(() => setProps({}));
  }, []);

  // Group properties for the dropdown
  const grouped = useMemo(() => {
    const g: Record<string, [string, SearchProperty, string][]> = {};
    Object.entries(props).forEach(([key, p]) => {
      const group = groupLabel(p.group);
      if (!g[group]) g[group] = [];
      g[group].push([key, p, propLabel(key, p)]);
    });
    Object.values(g).forEach(arr => arr.sort((a, b) => a[2].localeCompare(b[2])));
    return g;
  }, [props, t]);

  const updatePred = (i: number, patch: Partial<SearchPredicate>) => {
    setPredicates(prev => prev.map((p, idx) => idx === i ? { ...p, ...patch } : p));
    setPreviewCount(null);
  };

  const defaultValueForProp = (propKey: string): any => {
    const p = props[propKey];
    if (!p) return "";
    if (p.type === "bool") return true;
    if (p.type === "enum" && p.options?.length) return p.options[0];
    return "";
  };

  const addRow = () => {
    const firstKey = Object.keys(props)[0] || "video_codec";
    setPredicates(prev => [...prev, { property: firstKey, op: props[firstKey]?.ops?.[0] || "eq", value: defaultValueForProp(firstKey) }]);
    setPreviewCount(null);
  };
  const removeRow = (i: number) => {
    setPredicates(prev => prev.filter((_, idx) => idx !== i));
    setPreviewCount(null);
  };

  const runPreview = async () => {
    setPreviewing(true);
    try {
      const res = await advancedSearch(predicates, matchMode);
      setPreviewCount(res.total);
      setPreviewCap(res.limit);
    } catch (e) {
      setPreviewCount(-1);
    } finally {
      setPreviewing(false);
    }
  };

  const applyAndClose = async () => {
    const res = await advancedSearch(predicates, matchMode);
    onApply(predicates, res.file_paths);
  };

  const saveView = () => {
    const name = newViewName.trim();
    if (!name) return;
    const next = [...savedViews.filter(v => v.name !== name), { name, predicates: [...predicates] }];
    persistSavedViews(next);
    setSavedViews(next);
    setNewViewName("");
  };
  const loadView = (v: SavedView) => {
    setPredicates(v.predicates);
    setPreviewCount(null);
  };
  const deleteView = (name: string) => {
    const next = savedViews.filter(v => v.name !== name);
    persistSavedViews(next);
    setSavedViews(next);
  };

  const renderValueInput = (pred: SearchPredicate, i: number) => {
    const prop = props[pred.property];
    if (!prop) return null;
    if (pred.op === "exists") return null;

    if (prop.type === "bool") {
      return (
        <select
          value={String(pred.value ?? "true")}
          onChange={e => updatePred(i, { value: e.target.value === "true" })}
          style={selectStyle}
        >
          <option value="true">{t("scannerModals:search.true")}</option>
          <option value="false">{t("scannerModals:search.false")}</option>
        </select>
      );
    }

    // Enum: render a real dropdown using the property's options
    if (prop.type === "enum" && prop.options && prop.options.length > 0) {
      // For "in" we let the user pick multiple via a multi-select
      if (pred.op === "in") {
        const selected: string[] = Array.isArray(pred.value)
          ? pred.value.map(String)
          : (typeof pred.value === "string" && pred.value.trim()
              ? pred.value.split(",").map(s => s.trim()).filter(Boolean)
              : []);
        return (
          <select
            multiple
            value={selected}
            onChange={e => {
              const vals = Array.from(e.target.selectedOptions).map(o => o.value);
              updatePred(i, { value: vals });
            }}
            style={{ ...selectStyle, minWidth: 180, height: 80 }}
          >
            {prop.options.map(o => {
              const key = String(o);
              const label = optionLabel(pred.property, prop, key);
              return <option key={key} value={key}>{label}</option>;
            })}
          </select>
        );
      }
      return (
        <select
          value={pred.value ?? ""}
          onChange={e => updatePred(i, { value: e.target.value })}
          style={{ ...selectStyle, minWidth: 140 }}
        >
          <option value="">{t("scannerModals:search.pickValue")}</option>
          {prop.options.map(o => {
            const key = String(o);
            const label = optionLabel(pred.property, prop, key);
            return <option key={key} value={key}>{label}</option>;
          })}
        </select>
      );
    }

    if (pred.op === "between") {
      return (
        <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
          <input
            type={prop.type === "number" ? "number" : "text"}
            value={pred.value ?? ""}
            onChange={e => updatePred(i, { value: e.target.value })}
            placeholder={t("scannerModals:search.min")}
            style={{ ...inputStyle, width: 80 }}
          />
          <span style={{ color: "var(--text-muted)", fontSize: 11 }}>{t("scannerModals:search.betweenAnd")}</span>
          <input
            type={prop.type === "number" ? "number" : "text"}
            value={pred.value2 ?? ""}
            onChange={e => updatePred(i, { value2: e.target.value })}
            placeholder={t("scannerModals:search.max")}
            style={{ ...inputStyle, width: 80 }}
          />
        </span>
      );
    }

    if (pred.op === "in" || pred.op === "contains" || pred.op === "regex") {
      return (
        <input
          type="text"
          value={pred.value ?? ""}
          onChange={e => updatePred(i, { value: e.target.value })}
          placeholder={pred.op === "in" ? t("scannerModals:search.commaSeparated") : "..."}
          style={{ ...inputStyle, minWidth: 180 }}
        />
      );
    }

    return (
      <input
        type={prop.type === "number" ? "number" : "text"}
        value={pred.value ?? ""}
        onChange={e => updatePred(i, { value: e.target.value })}
        placeholder={prop.examples?.[0] != null ? t("scannerModals:search.example", { example: prop.examples[0] }) : ""}
        list={`prop-examples-${pred.property}`}
        style={{ ...inputStyle, minWidth: 140 }}
      />
    );
  };

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={modalStyle} onClick={e => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <h2 style={{ color: "var(--text-primary)", fontSize: 18, margin: 0 }}>{t("scannerModals:search.title")}</h2>
          <button onClick={onClose} style={closeBtnStyle}>&times;</button>
        </div>

        {savedViews.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.5 }}>{t("scannerModals:search.savedViews")}</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {savedViews.map(v => (
                <span key={v.name} style={{ display: "inline-flex", gap: 4, alignItems: "center", background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, padding: "3px 4px 3px 10px", fontSize: 11 }}>
                  <button onClick={() => loadView(v)} style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 11 }}>{v.name}</button>
                  <button onClick={() => deleteView(v.name)} title={t("common:actions.delete")} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 12, padding: "0 4px" }}>&times;</button>
                </span>
              ))}
            </div>
          </div>
        )}

        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("scannerModals:search.filesMatching")}</span>
          <select
            value={matchMode}
            onChange={e => { setMatchMode(e.target.value as "all" | "any"); setPreviewCount(null); }}
            style={{ ...selectStyle, width: 170 }}
          >
            <option value="all">{t("scannerModals:search.matchAll")}</option>
            <option value="any">{t("scannerModals:search.matchAny")}</option>
          </select>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 12 }}>
          {predicates.map((pred, i) => {
            const prop = props[pred.property];
            return (
              <div key={i} style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", padding: 6, background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 4 }}>
                {i > 0 && <span style={{ fontSize: 11, color: matchMode === "any" ? "var(--warning)" : "var(--text-muted)", fontWeight: 600, paddingRight: 4 }}>{matchMode === "any" ? t("scannerModals:search.or") : t("scannerModals:search.and")}</span>}
                <select value={pred.property} onChange={e => updatePred(i, { property: e.target.value, op: props[e.target.value]?.ops?.[0] || "eq", value: defaultValueForProp(e.target.value), value2: undefined })} style={{ ...selectStyle, minWidth: 200 }}>
                  {Object.entries(grouped).sort((a, b) => a[0].localeCompare(b[0])).map(([group, items]) => (
                    <optgroup key={group} label={group}>
                      {items.map(([key, , label]) => <option key={key} value={key}>{label}</option>)}
                    </optgroup>
                  ))}
                </select>
                <select value={pred.op} onChange={e => updatePred(i, { op: e.target.value })} style={{ ...selectStyle, width: 110 }}>
                  {(prop?.ops || []).map(op => <option key={op} value={op}>{OP_SYMBOLS[op] ?? (OP_WORDS.has(op) ? t(`scannerModals:operators.${op}`) : op)}</option>)}
                </select>
                {renderValueInput(pred, i)}
                {prop?.examples && prop.examples.length > 0 && (
                  <datalist id={`prop-examples-${pred.property}`}>
                    {prop.examples.map(ex => <option key={String(ex)} value={String(ex)} />)}
                  </datalist>
                )}
                <button onClick={() => removeRow(i)} title={t("common:actions.remove")} style={{ marginLeft: "auto", background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 14 }}>&times;</button>
              </div>
            );
          })}
          <button onClick={addRow} className="btn btn-secondary" style={{ alignSelf: "flex-start", fontSize: 11, padding: "4px 10px" }}>{t("scannerModals:search.addCondition")}</button>
        </div>

        {/* Save view */}
        <div style={{ display: "flex", gap: 6, alignItems: "center", padding: "8px 0 12px 0", borderTop: "1px solid var(--border)" }}>
          <input
            type="text"
            value={newViewName}
            onChange={e => setNewViewName(e.target.value)}
            placeholder={t("scannerModals:search.saveViewPlaceholder")}
            style={{ ...inputStyle, flex: "1 1 200px", maxWidth: 260 }}
            onKeyDown={e => { if (e.key === "Enter") saveView(); }}
          />
          <button onClick={saveView} disabled={!newViewName.trim()} className="btn btn-secondary" style={{ fontSize: 11, padding: "5px 12px", opacity: newViewName.trim() ? 1 : 0.5 }}>{t("scannerModals:search.saveView")}</button>
        </div>

        {/* Footer */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingTop: 8, borderTop: "1px solid var(--border)" }}>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {previewing ? t("scannerModals:search.previewing") : previewCount === null ? "" : previewCount < 0 ? t("scannerModals:search.failed") : (
              previewCap > 0 && previewCount > previewCap
                ? t("scannerModals:search.matchesCapped", { count: previewCount, num: previewCount.toLocaleString(), cap: previewCap.toLocaleString() })
                : t("scannerModals:search.matches", { count: previewCount, num: previewCount.toLocaleString() })
            )}
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <button onClick={runPreview} className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}>{t("scannerModals:search.preview")}</button>
            <button onClick={onClose} className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}>{t("common:actions.cancel")}</button>
            <button onClick={applyAndClose} className="btn btn-primary" style={{ fontSize: 12, padding: "6px 14px" }}>{t("scannerModals:search.apply")}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

const overlayStyle: React.CSSProperties = {
  position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)",
  display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
};
const modalStyle: React.CSSProperties = {
  background: "var(--bg-secondary)", border: "1px solid var(--border)", borderRadius: 8,
  padding: 20, width: "min(900px, 95vw)", maxHeight: "90vh", overflowY: "auto",
  boxShadow: "0 20px 60px rgba(0,0,0,0.5)",
};
const inputStyle: React.CSSProperties = {
  background: "var(--bg-primary)", color: "var(--text-primary)", border: "1px solid var(--border)",
  padding: "5px 10px", borderRadius: 4, fontSize: 12, outline: "none",
};
const selectStyle: React.CSSProperties = {
  ...inputStyle,
  paddingRight: 28,
  appearance: "none" as const,
  WebkitAppearance: "none" as const,
  backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239080a8' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E\")",
  backgroundRepeat: "no-repeat",
  backgroundPosition: "right 8px center",
};
const closeBtnStyle: React.CSSProperties = {
  background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 22, lineHeight: 1, padding: 0,
};
