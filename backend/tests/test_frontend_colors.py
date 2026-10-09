"""Light-mode safety (v0.10.0): colours come from the theme.

Components had ~320 colour literals made for the dark theme — on light
backgrounds they read at 1.3–2.5:1 (logs, FPS, VMAF tiers, removal
counts). They now use theme.css tokens; this keeps new ones out. A brand or
data-visualisation colour that should look the same in both themes goes in
ALLOWED with the reason."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
HEX = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")

WHITE = {"#fff", "#ffffff"}  # text on solid accent / danger / brand fills
ALLOWED = {
    "pages/DesignPage.tsx": None,  # dev-only design reference (not in production builds)
    "App.tsx": {"#4920f0", "#6a64ff"},  # brand gradient behind white text
    "components/PosterCard.tsx": {"#0d54e4"},  # TV badge fill behind white text
    "components/PosterFixModal.tsx": {"#0d54e4"},
    "pages/SettingsPage.tsx": {"#e5a00d", "#1f1f1f"},  # Plex's own sign-in button colours
    "pages/DashboardPage.tsx": {  # chart palettes (fills, not text)
        "#6882ff", "#2cf4e8", "#7c5cff", "#54a8ff", "#5089f7",
        "#ff8fb0", "#ffc078", "#ffd8a8", "#ffe8cc",
    },
    "pages/LogsPage.tsx": {  # the log viewer is a dark terminal in both themes
        "#00d4ff", "#4caf50", "#fdd835", "#ffa726", "#ce93d8", "#64b5f6",
        "#78909c", "#4dd0e1", "#aed581", "#ef5350",
    },
}


def test_components_use_theme_colours():
    offenders = []
    for path in sorted(SRC.rglob("*.ts*")):
        rel = path.relative_to(SRC).as_posix()
        if path.suffix not in (".ts", ".tsx") or "/i18n/locales/" in rel:
            continue
        allowed = ALLOWED.get(rel, set())
        if allowed is None:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for m in HEX.finditer(line):
                value = m.group(0).lower()
                if value not in WHITE and value not in allowed:
                    offenders.append(f"{rel}:{n} {value}")
    assert not offenders, (
        "colour literals bypass the theme — use a token from frontend/src/theme.css "
        "(--danger, --caution, --info, --success, --warning, --pink, --accent-text, "
        "--text-muted, …) or, for a brand / chart colour, add it to ALLOWED:\n  " + "\n  ".join(offenders)
    )
