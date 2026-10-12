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
