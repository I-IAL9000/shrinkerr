import pytest
from backend.language_detection import detect_subtitle_language


def test_detect_english_subtitle():
    text = (
        "You need to understand what happened here. "
        "The lawyer said the case was closed months ago, "
        "but nobody told the family about the decision."
    )
    lang, conf = detect_subtitle_language(text)
    assert lang == "eng", f"got {lang!r} conf={conf}"
    assert conf >= 0.7


def test_detect_german_subtitle():
    text = (
        "Du musst verstehen, was hier passiert ist. "
        "Der Anwalt sagte, der Fall sei vor Monaten abgeschlossen worden, "
        "aber niemand hat der Familie von der Entscheidung erzählt."
    )
    lang, conf = detect_subtitle_language(text)
    assert lang == "ger", f"got {lang!r} conf={conf}"


def test_detect_spanish_subtitle():
    text = (
        "Tienes que entender lo que pasó aquí. "
        "El abogado dijo que el caso se cerró hace meses, "
        "pero nadie le contó a la familia sobre la decisión."
    )
    lang, conf = detect_subtitle_language(text)
    assert lang == "spa", f"got {lang!r} conf={conf}"


def test_empty_or_garbage_text_returns_none():
    assert detect_subtitle_language("") == (None, 0.0)
    assert detect_subtitle_language("   \n\n  ") == (None, 0.0)
    lang, conf = detect_subtitle_language("123 456 --- >>> 00:00:01,000")
    assert lang is None
