"""v0.6.5 pre-tag verification gate.

Runs INSIDE the production container against the two reference discs:
  - Fast-Walking (1982) DVD  → expect audio[0]='eng'
  - Elephant (2003) BDMV     → expect audio[0]='fre', audio[1]='eng'
                                expect subtitle[0]='fre', subtitle[1]='eng'

Per spec acceptance criterion 10: exits 0 only if all assertions pass.
A non-zero exit blocks `git tag v0.6.5`.

Usage from host:
  docker cp scripts/verify_disc_languages.py shrinkerr:/tmp/
  docker exec shrinkerr python3 /tmp/verify_disc_languages.py

Both reference paths can be overridden via env vars
(SHRINKERR_TEST_DVD, SHRINKERR_TEST_BDMV) for flexibility.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


DEFAULT_DVD = "/media/Misc/Movies2/Fast-Walking (1982) [tt0083930]"
DEFAULT_BDMV = "/media/Misc/Elephant (2003) [tt0363589]"

EXPECTED_DVD_AUDIO_FIRST = "eng"
EXPECTED_BDMV_AUDIO = ["fre", "eng"]
EXPECTED_BDMV_SUBTITLE = ["fre", "eng"]


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}", flush=True)


async def main() -> int:
    failures: list[str] = []

    dvd_root = Path(os.environ.get("SHRINKERR_TEST_DVD", DEFAULT_DVD))
    bdmv_root = Path(os.environ.get("SHRINKERR_TEST_BDMV", DEFAULT_BDMV))

    print(f"=== Layer-2 verification: v0.6.5 disc language metadata ===", flush=True)
    print(f"DVD reference : {dvd_root}", flush=True)
    print(f"BDMV reference: {bdmv_root}", flush=True)
    print()

    from backend.disc_metadata import parse_disc_languages
    from backend.scanner import probe_file

    # --- DVD: parse only ---
    print("[1/4] DVD parse_disc_languages against Fast-Walking IFO", flush=True)
    if not dvd_root.is_dir():
        failures.append(f"DVD reference missing: {dvd_root}")
        _fail(f"DVD root not found")
    else:
        dvd_langs = parse_disc_languages(dvd_root, "dvd")
        if dvd_langs["audio"] and dvd_langs["audio"][0] == EXPECTED_DVD_AUDIO_FIRST:
            _ok(f"audio[0]={dvd_langs['audio'][0]!r} (expected {EXPECTED_DVD_AUDIO_FIRST!r})")
        else:
            failures.append(f"DVD audio[0] wrong: got {dvd_langs['audio']!r}, expected first={EXPECTED_DVD_AUDIO_FIRST!r}")
            _fail(f"audio = {dvd_langs['audio']!r}")
        print(f"    full result: {dvd_langs}", flush=True)

    # --- DVD: full probe_file → patched audio_tracks ---
    print("[2/4] DVD probe_file integration (Fast-Walking)", flush=True)
    if dvd_root.is_dir():
        marker = dvd_root / "VIDEO_TS" / "VIDEO_TS.IFO"
        if not marker.is_file():
            failures.append(f"DVD marker missing: {marker}")
            _fail("marker file not found")
        else:
            probe = await probe_file(str(marker))
            if probe is None:
                failures.append("DVD probe_file returned None")
                _fail("probe failed")
            else:
                audio = probe.get("audio_tracks", [])
                if audio and audio[0].get("language") == EXPECTED_DVD_AUDIO_FIRST:
                    _ok(f"audio_tracks[0].language={audio[0].get('language')!r}")
                else:
                    failures.append(f"DVD probe audio[0].language wrong: {audio[0] if audio else 'no tracks'}")
                    _fail(f"audio_tracks[0]={audio[0] if audio else None}")

    # --- BDMV: parse only ---
    print("[3/4] BDMV parse_disc_languages against Elephant mpls", flush=True)
    if not bdmv_root.is_dir():
        failures.append(f"BDMV reference missing: {bdmv_root}")
        _fail("BDMV root not found")
    else:
        bdmv_langs = parse_disc_languages(bdmv_root, "bdmv")
        # Audio: first two slots must match
        audio = bdmv_langs["audio"]
        if len(audio) >= 2 and audio[0] == EXPECTED_BDMV_AUDIO[0] and audio[1] == EXPECTED_BDMV_AUDIO[1]:
            _ok(f"audio[:2]={audio[:2]!r}")
        else:
            failures.append(f"BDMV audio[:2] wrong: got {audio!r}, expected {EXPECTED_BDMV_AUDIO!r}")
            _fail(f"audio = {audio!r}")
        # Subtitle: first two slots
        sub = bdmv_langs["subtitle"]
        if len(sub) >= 2 and sub[0] == EXPECTED_BDMV_SUBTITLE[0] and sub[1] == EXPECTED_BDMV_SUBTITLE[1]:
            _ok(f"subtitle[:2]={sub[:2]!r}")
        else:
            failures.append(f"BDMV subtitle[:2] wrong: got {sub!r}, expected {EXPECTED_BDMV_SUBTITLE!r}")
            _fail(f"subtitle = {sub!r}")
        print(f"    full result: {bdmv_langs}", flush=True)

    # --- BDMV: full probe_file → patched tracks (stream-order assertion) ---
    print("[4/4] BDMV probe_file stream-order correlation (Elephant)", flush=True)
    if bdmv_root.is_dir():
        marker = bdmv_root / "BDMV" / "index.bdmv"
        if not marker.is_file():
            failures.append(f"BDMV marker missing: {marker}")
            _fail("marker file not found")
        else:
            probe = await probe_file(str(marker))
            if probe is None:
                failures.append("BDMV probe_file returned None")
                _fail("probe failed")
            else:
                audio = probe.get("audio_tracks", [])
                sub = probe.get("subtitle_tracks", [])
                if len(audio) >= 2 and audio[0].get("language") == "fre" and audio[1].get("language") == "eng":
                    _ok("audio_tracks[0]=fre, audio_tracks[1]=eng (correct stream order)")
                else:
                    failures.append(f"BDMV probe audio stream order wrong: {[t.get('language') for t in audio]}")
                    _fail(f"audio langs = {[t.get('language') for t in audio]}")
                if len(sub) >= 2 and sub[0].get("language") == "fre" and sub[1].get("language") == "eng":
                    _ok("subtitle_tracks[0]=fre, subtitle_tracks[1]=eng (correct stream order)")
                else:
                    failures.append(f"BDMV probe subtitle stream order wrong: {[t.get('language') for t in sub]}")
                    _fail(f"subtitle langs = {[t.get('language') for t in sub]}")

    print()
    if failures:
        print(f"=== FAIL: {len(failures)} assertion(s) failed — do NOT tag v0.6.5 ===", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    print("=== PASS: all assertions OK — safe to git tag v0.6.5 ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
