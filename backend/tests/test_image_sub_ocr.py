import pytest


def test_script_to_tesseract_langs():
    from backend.image_sub_ocr import _script_to_ocr_langs
    assert _script_to_ocr_langs("Latin") == "eng"
    assert _script_to_ocr_langs("Han") == "chi_sim+chi_tra"
    assert _script_to_ocr_langs("Japanese") == "jpn"
    assert _script_to_ocr_langs("Korean") == "kor"
    assert _script_to_ocr_langs("Cyrillic") == "rus"
    assert _script_to_ocr_langs("Arabic") == "ara"
    # Unknown script -> default to Latin (eng) rather than fail.
    assert _script_to_ocr_langs("Fraktur") == "eng"
    assert _script_to_ocr_langs(None) == "eng"


def test_parse_osd_script():
    from backend.image_sub_ocr import _parse_osd_script
    osd = ("Page number: 0\nOrientation in degrees: 0\n"
           "Script: Han\nScript confidence: 3.1\n")
    assert _parse_osd_script(osd) == "Han"
    assert _parse_osd_script("no script line here") is None
    assert _parse_osd_script("") is None
