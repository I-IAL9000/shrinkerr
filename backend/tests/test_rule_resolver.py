"""Unit tests for backend/rule_resolver.py — focused on the date_added
condition type added in v0.5.1.

These tests exercise the parser + handler directly via _check_condition.
Integration with the rules engine (resolve_rules_for_batch) is covered
by test_watcher.py."""
from datetime import datetime, timedelta, timezone
import pytest
from backend.rule_resolver import _parse_age_hours, _check_condition


def _row(detected_at):
    """scan_row-shaped dict the handler reads."""
    return {
        "file_path": "/media/x.mkv",
        "file_size": 1_000_000_000,
        "video_codec": "h264",
        "video_height": 1080,
        "audio_tracks_json": None,
        "new_detected_at": detected_at,
    }


def _cond(op, value):
    return {"type": "date_added", "operator": op, "value": value}


def _iso_n_hours_ago(n):
    """Return ISO-8601 string for now − n hours, UTC."""
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def test_parse_age_hours_units():
    assert _parse_age_hours("1h") == 1
    assert _parse_age_hours("24h") == 24
    assert _parse_age_hours("1d") == 24
    assert _parse_age_hours("7d") == 168
    assert _parse_age_hours("1w") == 168
    assert _parse_age_hours("4w") == 672


def test_parse_age_hours_rejects_zero():
    """0h/0d/0w are semantically nonsense (always-false/always-true
    tautologies), so the parser rejects them like malformed input."""
    assert _parse_age_hours("0h") is None
    assert _parse_age_hours("0d") is None
    assert _parse_age_hours("0w") is None


def test_parse_age_hours_rejects_malformed():
    assert _parse_age_hours("") is None
    assert _parse_age_hours("foo") is None
    assert _parse_age_hours("24") is None      # missing unit
    assert _parse_age_hours("h24") is None     # wrong order
    assert _parse_age_hours("24x") is None     # bad unit
    assert _parse_age_hours("-5h") is None     # signed
    assert _parse_age_hours("1.5h") is None    # decimal


def test_date_added_newer_than_within_window():
    """less_than 24h with detected_at = 2h ago → True (newer than 24h)."""
    row = _row(_iso_n_hours_ago(2))
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row, [], None) is True


def test_date_added_newer_than_outside_window():
    """less_than 24h with detected_at = 48h ago → False."""
    row = _row(_iso_n_hours_ago(48))
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row, [], None) is False


def test_date_added_older_than_within_window():
    """greater_than 7d with detected_at = 14d ago → True (older than 7d)."""
    row = _row(_iso_n_hours_ago(14 * 24))
    assert _check_condition(_cond("greater_than", "7d"), "/media/x.mkv", row, [], None) is True


def test_date_added_null_treated_as_ancient_for_older_than():
    """NULL detected_at + greater_than → True (treat as ancient).
    Matches pre-watcher rows, scanner-added rows, bypass paths."""
    row = _row(None)
    assert _check_condition(_cond("greater_than", "7d"), "/media/x.mkv", row, [], None) is True


def test_date_added_null_returns_false_for_newer_than():
    """NULL detected_at + less_than → False (no fresh-arrival evidence)."""
    row = _row(None)
    assert _check_condition(_cond("less_than", "7d"), "/media/x.mkv", row, [], None) is False


def test_date_added_malformed_value_returns_false():
    """Top-level guard at rule_resolver.py:197 catches empty value.
    The handler itself catches malformed-but-non-empty via _parse_age_hours."""
    row = _row(_iso_n_hours_ago(2))
    assert _check_condition(_cond("less_than", "foo"), "/media/x.mkv", row, [], None) is False


def test_date_added_zero_value_returns_false():
    """'0h'/'0d' produce surprising tautologies; parser rejects them."""
    row = _row(_iso_n_hours_ago(2))
    assert _check_condition(_cond("less_than", "0h"), "/media/x.mkv", row, [], None) is False
    assert _check_condition(_cond("less_than", "0d"), "/media/x.mkv", row, [], None) is False


def test_date_added_units_conversion():
    """less_than 1d must behave identically to less_than 24h."""
    row = _row(_iso_n_hours_ago(12))
    assert _check_condition(_cond("less_than", "1d"), "/media/x.mkv", row, [], None) is True
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row, [], None) is True
    row_old = _row(_iso_n_hours_ago(36))
    assert _check_condition(_cond("less_than", "1d"), "/media/x.mkv", row_old, [], None) is False
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row_old, [], None) is False
