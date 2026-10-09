/**
 * Copy text to the clipboard. navigator.clipboard only exists on HTTPS or
 * localhost — Shrinkerr is usually opened over plain HTTP on the LAN — so
 * fall back to a hidden textarea and execCommand("copy").
 */
export async function copyText(text: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    /* fall through to the textarea */
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}
