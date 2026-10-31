"""What the VMAF target shows (v0.10.0): how long searches take, in the job
report and in Add to Queue; that savings for target files are an estimate at
the starting quality; and the live step's quality named as the encoder names
it. The frontend has no test runner: these check its catalogs (en and es) and
that the components use them."""
import json
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
PLURALS = {"en": ("_one", "_other"), "es": ("_one", "_many", "_other")}


def _text(lang: str, dotted: str) -> str | None:
    ns, _, key = dotted.partition(":")
    node = json.loads((FRONTEND / "i18n" / "locales" / lang / f"{ns}.json").read_text(encoding="utf-8"))
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, str) else None


def _forms(lang: str, dotted: str) -> list[str]:
    """A key's text, or each of its plural forms in `lang` (all must exist)."""
    if _text(lang, dotted) is not None:
        return [_text(lang, dotted)]
    forms = [_text(lang, dotted + suffix) for suffix in PLURALS[lang]]
    assert all(forms), (lang, dotted)
    return forms


def _placeholders(lang: str, dotted: str) -> list[set[str]]:
    return [set(PLACEHOLDER.findall(text)) for text in _forms(lang, dotted)]


def _source(name: str) -> str:
    return next(FRONTEND.rglob(name)).read_text(encoding="utf-8")


def test_the_report_says_how_long_the_search_took():
    for lang in PLURALS:
        for key in ("vmafTargetFound", "vmafTargetMissed"):
            assert all("search" in p for p in _placeholders(lang, f"queue:item.stats.{key}")), (lang, key)
        assert all(p == {"count", "time"} for p in _placeholders(lang, "queue:item.stats.vmafTargetSearch")), lang
    src = _source("JobListItem.tsx")
    assert re.search(r'"queue:item\.stats\.vmafTargetSearch",\s*\{\s*count: vt\.rounds, time: fmtDuration\(vt\.seconds\)',
                     src)


def test_add_to_queue_says_how_long_each_search_takes():
    for lang in PLURALS:
        assert all("time" in p for p in _placeholders(lang, "scannerModals:estimate.whatHappens.vmafTarget")), lang
    src = _source("EstimateModal.tsx")
    assert re.search(r'"scannerModals:estimate\.whatHappens\.vmafTarget",\s*\{[^}]*time: searchTime', src)
    assert "estimate?.vmaf_target_search_seconds" in src


def test_add_to_queue_says_target_savings_are_at_the_starting_quality():
    for lang in PLURALS:
        assert _text(lang, "scannerModals:estimate.savingsAtStartingQuality"), lang
    src = _source("EstimateModal.tsx")
    assert re.search(r'vmafTargets\.length > 0 && \([^)]*\n?.*"scannerModals:estimate\.savingsAtStartingQuality"',
                     src, re.S)


def test_the_live_step_names_the_quality_as_the_encoder_does():
    for lang in PLURALS:
        for key in ("vmafTargetRound", "vmafTargetReencode"):
            [names] = _placeholders(lang, f"serverJobs:steps.{key}")
            assert "quality" in names and "cq" not in names, (lang, key)
    assert "qualityLabelForCq(params.encoder" in _source("server.ts")


def test_jobs_from_before_searches_were_timed_read_without_the_search_part():
    """Completed jobs from earlier 0.10 builds have no rounds or seconds: the
    line used to end in the raw key "item.stats.vmafTargetSearch"."""
    for lang in ("en", "es"):
        for key in ("vmafTargetFound_untimed", "vmafTargetMissed_untimed"):
            assert _placeholders(lang, f"queue:item.stats.{key}") and all(
                "search" not in p for p in _placeholders(lang, f"queue:item.stats.{key}")), (lang, key)
    src = (Path(__file__).resolve().parents[2] / "frontend/src/components/JobListItem.tsx").read_text()
    assert 'context: vt.rounds == null ? "untimed" : undefined' in src
    assert 'search: vt.rounds == null ? "" : t("queue:item.stats.vmafTargetSearch"' in src


def test_the_report_says_when_no_quality_could_be_tested():
    """A search that couldn't test anything (a disc that can't be sampled, a
    sample that couldn't be cut) used to leave no trace in the report."""
    for lang in ("en", "es"):
        assert all(p == {"target"} for p in _placeholders(lang, "queue:item.stats.vmafTargetUntested")), lang
    src = (Path(__file__).resolve().parents[2] / "frontend/src/components/JobListItem.tsx").read_text()
    assert "logData.encoding_stats.vmaf_target_untested != null" in src
    assert re.search(r'"queue:item\.stats\.vmafTargetUntested",\s*\{\s*target: logData\.encoding_stats\.vmaf_target_untested',
                     src)


def test_the_report_says_when_the_samples_werent_trusted():
    """Samples that scored too low at every quality to be trusted (a misaligned
    or resized compare) are named as such, with how low, not as a sample that
    couldn't be cut or scored."""
    for lang in ("en", "es"):
        assert all(p == {"target", "best"} for p in _placeholders(lang, "queue:item.stats.vmafTargetUntrusted")), lang
    src = (Path(__file__).resolve().parents[2] / "frontend/src/components/JobListItem.tsx").read_text()
    assert re.search(r'"queue:item\.stats\.vmafTargetUntrusted",\s*\{\s*target: logData\.encoding_stats\.vmaf_target_untested,'
                     r'\s*best: logData\.encoding_stats\.vmaf_target_untrusted', src)


def test_the_search_says_which_sample_it_is_scoring():
    for lang in ("en", "es"):
        assert all({"quality", "round", "sample", "samples"} <= p
                   for p in _placeholders(lang, "serverJobs:steps.vmafTargetSample")), lang
