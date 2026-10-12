/** The dropdown arrow, the one every <select> shows (theme.css), in the
 *  text's colour: on the filter bar's menus and the Views button. */
export default function Chevron() {
  return (
    <svg className="chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}
