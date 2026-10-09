// Settings sub-pages (v0.10.0): one URL each — /settings/<id> — instead of
// one 15,500 px page. `anchors` are the in-page ids each one holds, so old
// links like /settings#connections still land on the right page.
export const SETTINGS_SECTIONS = [
  { id: "media", labelKey: "nav:settingsSections.media", anchors: ["directories", "renaming"] },
  { id: "video", labelKey: "nav:settingsSections.video", anchors: ["video"] },
  { id: "audio", labelKey: "nav:settingsSections.audioSubtitles", anchors: ["audio", "subtitles"] },
  { id: "automation", labelKey: "nav:settingsSections.automation", anchors: ["automation", "rules", "rule-form"] },
  { id: "integrations", labelKey: "nav:settingsSections.integrations", anchors: ["connections"] },
  { id: "notifications", labelKey: "nav:settingsSections.notifications", anchors: ["notifications"] },
  { id: "system", labelKey: "nav:settingsSections.system", anchors: ["system", "updates", "support"] },
] as const;

export type SettingsSectionId = (typeof SETTINGS_SECTIONS)[number]["id"];

export function sectionForAnchor(anchor: string): SettingsSectionId | null {
  return SETTINGS_SECTIONS.find(s => (s.anchors as readonly string[]).includes(anchor))?.id ?? null;
}
