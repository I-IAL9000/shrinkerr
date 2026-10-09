"""Every text colour token meets WCAG AA (4.5:1) on every background of its
theme (v0.10.0). The light theme had --success at 3.7:1, and the colours
components hard-coded were 1.3–2.5:1 there (test_frontend_colors)."""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "theme.css").read_text()

TEXT = ["text-primary", "text-secondary", "text-muted", "text-dim", "accent-text",
        "success", "warning", "danger", "caution", "info", "pink", "plex", "imdb"]
BACKGROUNDS = ["bg-primary", "bg-secondary", "bg-tertiary", "bg-card"]


def _block(selector: str) -> dict[str, str]:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", CSS, re.S)
    assert m, selector
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", m.group(1)))


def _luminance(hex_colour: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


DARK = _block(":root")
THEMES = {"dark": DARK, "light": {**DARK, **_block('[data-theme="light"]')}}


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_text_tokens_meet_aa(theme):
    tokens = THEMES[theme]
    failures = [
        f"--{fg} {tokens[fg]} on --{bg} {tokens[bg]}: {_ratio(tokens[fg], tokens[bg]):.2f}"
        for fg in TEXT for bg in BACKGROUNDS if _ratio(tokens[fg], tokens[bg]) < 4.5
    ]
    assert not failures, failures


def test_log_terminal_text_meets_aa():
    term = _block(".log-terminal")
    for fg in ("text-secondary", "text-dim"):
        assert _ratio(term[fg], term["bg-primary"]) >= 4.5, fg
