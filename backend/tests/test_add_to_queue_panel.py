"""Add to Queue's encoding settings (v0.10.0), as the maintainer asked:
the VMAF target sits above the quality it decides, with the preset beside
the quality; the quality can't be moved while the VMAF target finds every
file's; each "Auto" says what it comes to (estimate["auto"]); and
converting ignored files keeps the encode rules. The frontend has no test
runner: these check the component's source and its catalogs."""
import re

from backend.tests.test_vmaf_target_ui import PLURALS, _placeholders, _source, _text


def test_the_target_comes_before_the_quality_and_the_preset_after_it():
    src = _source("EstimateModal.tsx")
    rows = [src.index(f'{{t("scannerModals:overrides.{key}"') for key in ("encoder", "vmafTarget")]
    rows.insert(2, src.index('{t("scannerModals:overrides.quality", { mode'))
    rows.append(src.index('{t("scannerModals:overrides.preset")}</label>'))
    assert rows == sorted(rows), rows


def test_the_quality_waits_while_the_target_finds_every_files():
    src = _source("EstimateModal.tsx")
    assert "const cqBySearch = searching > 0 && unsearched === 0" in src
    assert "disabled={cqBySearch}" in src
    assert "if (cqUsed !== null)" in src and "if (cq !== null)" not in src  # not sent while unused
    for lang in PLURALS:
        assert _text(lang, "scannerModals:overrides.qualityBySearch"), lang
        assert all(p == {"num"} for p in _placeholders(lang, "scannerModals:overrides.qualityForSome")), lang


def test_each_auto_says_what_it_comes_to():
    src = _source("EstimateModal.tsx")
    for field in ("encoder", "preset", "audio", "resolution"):
        assert re.search(rf'<option value="">\{{autoLabel\("{field}"', src), field
    for lang in PLURALS:
        assert all(p == {"value"} for p in _placeholders(lang, "scannerModals:overrides.autoSettings")), lang
        assert all(p == {"value"} for p in _placeholders(lang, "scannerModals:overrides.autoRule")), lang


def test_converting_ignored_files_says_the_rules_still_apply():
    for lang in PLURALS:
        for key in ("rulesWarningHelp", "overrideRules", "includeIgnored"):
            assert _text(lang, f"scannerModals:estimate.{key}"), (lang, key)
    assert "default settings" not in _text("en", "scannerModals:estimate.rulesWarningHelp")
    assert "override" not in _text("en", "scannerModals:estimate.includeIgnored").lower()
