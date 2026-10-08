// A folder disc is queued by its marker file (.../<Title>/BDMV/index.bdmv or
// .../<Title>/VIDEO_TS/VIDEO_TS.IFO); show the disc folder's name instead,
// like the Scanner does. Anything else (ISOs included) shows its file name.
// Mirrors backend scanner.display_name_for_path (v0.9.154).
export function displayNameForPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  const [name, parent, title] = [parts.at(-1) || path, parts.at(-2) || "", parts.at(-3)];
  const marker = `${parent}/${name}`.toLowerCase();
  if (title && (marker === "bdmv/index.bdmv" || marker === "video_ts/video_ts.ifo")) return title;
  return name;
}
