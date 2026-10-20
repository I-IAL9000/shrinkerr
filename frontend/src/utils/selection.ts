// What stays ticked when one item is unticked under "Select all" (v0.10.0).
// For every other folder, the branch where its path leaves the unticked
// item's — another show, another season of the same show — so a row at any
// level (the filtered tree's titles, the poster cards, a season) stays
// ticked: the selection was every folder's own path, which didn't cover the
// titles above them, so every other title showed unticked. The server applies
// the filters to these paths. A folder above the unticked item keeps its own
// files when they're loaded (`loadedFiles`), else they go with it.
export function selectAllExcept(
  path: string,
  folders: { path: string; is_file?: boolean }[],
  loadedFiles: (folder: string) => string[] | undefined,
): Set<string> {
  const next = new Set<string>();
  const target = path.split("/").filter(Boolean);
  const unticked = (p: string) => p === path || (path.endsWith("/") && p.startsWith(path));
  for (const folder of folders) {
    const parts = folder.path.split("/").filter(Boolean);
    let common = 0;
    while (common < parts.length && common < target.length && parts[common] === target[common]) common++;
    if (unticked(folder.is_file ? folder.path : folder.path + "/")) continue;
    if (common < parts.length) {
      // Its own branch: up to and including where it leaves the unticked path.
      const branch = "/" + parts.slice(0, common + 1).join("/");
      next.add(folder.is_file && common === parts.length - 1 ? branch : branch + "/");
    } else {
      // A folder above the unticked item: its own files, one by one.
      for (const file of loadedFiles(folder.path) ?? []) {
        if (!unticked(file)) next.add(file);
      }
    }
  }
  return next;
}
