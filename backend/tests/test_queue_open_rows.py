"""Open rows stay mounted when the Queue scrolls (v0.10.0). The lists are
virtualized: a row out of view unmounted, and an open one that came back
showed "Loading…" (79 px) until its details reloaded (387 px). Each change
above the view moved the page: with many rows open, scrolling up jumped
308 px one way and back. The open rows' indexes are kept in the rendered
range, so their details (and an open ffmpeg output or before / after) stay.
Measured in the browser; the frontend has no test runner, so this checks
the components' source."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_the_list_keeps_the_rows_it_is_given_mounted():
    src = (SRC / "components" / "VirtualJobList.tsx").read_text(encoding="utf-8")
    assert re.search(r"keepMounted\?: number\[\]", src)
    assert "defaultRangeExtractor" in src
    assert src.count("rangeExtractor") >= 3  # both lists use it


def test_the_queue_keeps_its_open_rows_mounted():
    src = (SRC / "pages" / "QueuePage.tsx").read_text(encoding="utf-8")
    assert re.search(r"expandedIds\.has\(j\.id\)", src)
    lists = re.findall(r"<VirtualJobList [^>]*/>", src)
    assert len(lists) == 3 and all("keepMounted={openRows}" in l for l in lists), lists
