import { useState } from "react";
import { useTranslation } from "react-i18next";
import { researchFile } from "./api";
import { useToast } from "./useToast";
import { useConfirm } from "./components/ConfirmModal";

/** Request a replacement from Sonarr / Radarr — blocklist the release,
 *  delete the file, search again — once the user confirms: the file's
 *  details, and a failed conversion of a damaged source (v0.10.0). */
export function useRequestReplacement() {
  const { t } = useTranslation(["fileDetail"]);
  const toast = useToast();
  const confirm = useConfirm();
  const [requesting, setRequesting] = useState(false);

  const request = async (filePath: string, corrupt: boolean) => {
    const label = corrupt ? t("fileDetail:research.labelCorrupt") : t("fileDetail:research.labelReplace");
    const ok = await confirm({
      message: t("fileDetail:research.confirmMessage", { label }),
      confirmLabel: t("fileDetail:research.confirmLabel"),
      danger: true,
    });
    if (!ok) return;
    setRequesting(true);
    try {
      const r = await researchFile(filePath, true);
      if (r.success) {
        const parts = [
          r.service === "sonarr" ? `Sonarr: ${r.series || ""}` : `Radarr: ${r.movie || ""}`,
          r.blocklisted ? t("fileDetail:research.blocklisted") : t("fileDetail:research.notBlocklisted"),
          r.deleted ? t("fileDetail:research.deleted") : t("fileDetail:research.notDeleted"),
          r.searched ? t("fileDetail:research.searched") : t("fileDetail:research.notSearched"),
          ...(r.untracked ? [t("fileDetail:research.untracked")] : []),
        ];
        toast(t("fileDetail:research.requested", { parts: parts.join(", ") }), "success");
      } else {
        toast(t("fileDetail:research.failed", { error: r.error || t("fileDetail:unknownError") }), "error");
      }
    } catch (exc: any) {
      toast(t("fileDetail:research.error", { error: exc?.message || exc }), "error");
    } finally {
      setRequesting(false);
    }
  };
  return { request, requesting };
}
