"""The job report names the original's codec (v0.10.0). Its Codec row said
"x264" for every original since the first version — Stolen Kisses' HEVC
remux too. The finished job records the original's codec (encoding_stats
source_codec), named as the Scanner names codecs (codecLabels.ts); a job
from before it was recorded says nothing. The frontend has no test runner:
this checks the component's source."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_the_original_codec_is_the_files_own():
    item = (SRC / "components" / "JobListItem.tsx").read_text(encoding="utf-8")
    assert not re.search(r">\s*x264\s*<", item)  # the hard-coded label
    assert re.search(r"CODEC_LABELS\[\s*\(?\s*logData\.encoding_stats\.source_codec", item)
    assert re.search(r'import \{[^}]*\bCODEC_LABELS\b[^}]*\} from "\.\./codecLabels"', item)
