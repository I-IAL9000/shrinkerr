import { createContext, useContext } from "react";
import type { ScannedFile } from "./types";

// The Scanner's actions for the file open in an expanded row (FileDetail),
// from ScannerPage. FileDetail renders inside PosterGrid and FileTree's
// memoised rows; a context saves passing these through both. Elsewhere
// (no provider) the actions aren't offered.
export interface ScannerItemActions {
  addToQueue: (file: ScannedFile) => void;
  // Bazarr's URL and API key are set: "Search for subtitles" is offered.
  bazarrReady: boolean;
}

export const ScannerItemActionsContext = createContext<ScannerItemActions | null>(null);

export const useScannerItemActions = () => useContext(ScannerItemActionsContext);
