"""Every button of a style has the same size: a .btn's size and shape come
from its classes (.btn-sm, .btn-xs, .btn-pill …, theme.css), never from an
inline style. Settings' Export and Import were different sizes, and 75
buttons had set their own, in about 20 combinations."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "frontend/src"
# Size and shape. Layout (margins, width, a height matching an input beside
# it) and one-off colours stay allowed.
FORBIDDEN = ("fontSize", "fontWeight", "lineHeight", "padding", "paddingLeft", "paddingRight",
             "paddingTop", "paddingBottom", "borderRadius", "border")
_COMMENTS = re.compile(r"/\*.*?\*/|^\s*//.*?$", re.S | re.M)


def _tag_end(s: str, i: int):
    depth, quote = 0, None
    while i < len(s):
        c = s[i]
        if quote:
            if c == quote and s[i - 1] != "\\":
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return i
        i += 1
    return None


def _btn_tags():
    for path in sorted(ROOT.rglob("*.tsx")):
        source = _COMMENTS.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), path.read_text())
        for m in re.finditer(r"<([a-zA-Z][\w.]*)\b", source):
            end = _tag_end(source, m.end())
            if end is None:
                continue
            tag = source[m.start():end + 1]
            cls = re.search(r'className=(\{`[^`]*`\}|"[^"]*"|\{[^}]*\})', tag)
            if cls and re.search(r"(?<![\w-])btn(?![\w-])", cls.group(1)):
                yield f"{path.relative_to(ROOT)}:{source[:m.start()].count(chr(10)) + 1}", tag


def test_the_scan_finds_the_buttons():
    tags = list(_btn_tags())
    assert len(tags) > 150  # the scan works (164 on 12 Oct 2026)
    assert any("Export" in t or "settings/export" in t for _, t in tags)


def test_no_button_sets_its_own_size_or_shape():
    offenders = []
    for where, tag in _btn_tags():
        style = re.search(r"style=\{(.*?)\}\s*(?:[a-zA-Z]+=|/?>)", tag, re.S)
        if not style:
            continue
        keys = set(re.findall(r"\b(\w+)\s*:", style.group(1)))
        if style.group(1).strip().isidentifier():  # style={SOME_CONSTANT}
            offenders.append(f"{where}: style={{{style.group(1).strip()}}}")
        elif keys & set(FORBIDDEN):
            offenders.append(f"{where}: {sorted(keys & set(FORBIDDEN))}")
    assert not offenders, "use .btn-sm / .btn-xs / .btn-pill / .btn-ghost / .btn-danger:\n" + "\n".join(offenders)


def test_no_button_is_nested_in_a_link():
    # Export was a <button> in an <a>: invalid, and a box of its own.
    for path in sorted(ROOT.rglob("*.tsx")):
        source = path.read_text()
        assert not re.search(r"<a\b[^>]*>\s*<button\b", source), path.name


def test_an_icon_only_button_is_as_tall_as_one_with_a_label():
    """A pill's height is its line of text: the Scanner's ⋯, an icon alone,
    was 26 px beside 31 px pills. .btn-icon makes the icon a line tall."""
    css = (ROOT / "theme.css").read_text()
    assert re.search(r"\.btn-icon svg \{[^}]*height: 1lh", css)
    icon_menus = []  # ActionMenu's button: buttonClassName, which _btn_tags doesn't see
    for path in sorted(ROOT.rglob("*.tsx")):
        source = path.read_text()
        for m in re.finditer(r"<ActionMenu\b", source):
            tag = source[m.start():_tag_end(source, m.end())]
            cls = re.search(r'buttonClassName="([^"]*)"', tag)
            if "icon=" in tag and cls and "btn" in cls.group(1).split():
                icon_menus.append((path.name, cls.group(1).split()))
    assert icon_menus
    assert all("btn-icon" in classes for _, classes in icon_menus), icon_menus


def test_fix_matchs_fields_are_as_tall_as_its_search_button():
    """The title and year were 29 px beside a 32 px Search: the row centred
    them. Stretched, they take the button's height."""
    src = (ROOT / "components/PosterFixModal.tsx").read_text()
    row = src[src.index("{/* Search bar */}"):src.index('t("library:fix.search")')]
    assert re.search(r'<div style=\{\{[^}]*display: "flex"[^}]*alignItems: "stretch"', row)


def test_the_queues_selection_bar_looks_like_the_scanners():
    """The Queue's selection bar had a look of its own: small square buttons,
    a red Ignore and Remove, underlined links, the arrows in a well. It takes
    the Scanner bar's: pills, the count in the accent, the same box."""
    bar = (ROOT / "components/QueueControlPanel.tsx").read_text()
    scanner = (ROOT / "pages/ScannerPage.tsx").read_text()
    buttons = [bar[m.start():_tag_end(bar, m.end()) + 1] for m in re.finditer(r"<button\b", bar)]
    assert len(buttons) == 9  # select all, deselect all, four arrows, edit, ignore, remove
    for tag in buttons:
        assert re.search(r'className="btn btn-sm btn-pill btn-(primary|secondary)( btn-icon)?"', tag), tag
        assert "style=" not in tag, tag
        if "btn-icon" in tag:  # an arrow: named, as the ⋯ is
            assert "aria-label=" in tag and "title=" in tag, tag
    assert 'buttonClassName="btn btn-sm btn-pill btn-secondary"' in bar  # Priority
    count = '<span style={{ fontSize: 11, color: "var(--accent-text)", fontWeight: 600 }}>'
    assert count in bar and count in scanner
    for box in ('gap: 6', 'padding: "8px 12px"', 'background: "var(--bg-card)"'):
        assert box in bar and box in scanner, box
