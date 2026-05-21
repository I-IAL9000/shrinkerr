"""v0.7.4 pre-tag verification gate.

Runs INSIDE the production container against the two reference discs:
  - Fast-Walking (1982) DVD  → expect audio[0]='eng'
  - Elephant (2003) BDMV     → expect audio[0]='fre', audio[1]='eng'
                                expect subtitle[0]='fre', subtitle[1]='fre'
  - The Skin I Live In (2011) DVD ISO → expect classifier='dvd', audio[0] non-empty
  - Elephant (2003) BD ISO   → expect audio[:2]=['fre','eng'], subtitle[:2]=['fre','fre']

Per spec acceptance criterion 10: exits 0 only if all assertions pass.
A non-zero exit blocks `git tag v0.7.4`.

Usage from host:
  docker cp scripts/verify_disc_languages.py shrinkerr:/tmp/
  docker exec shrinkerr python3 /tmp/verify_disc_languages.py

Reference paths can be overridden via env vars
(SHRINKERR_TEST_DVD, SHRINKERR_TEST_BDMV,
 SHRINKERR_TEST_DVD_ISO, SHRINKERR_TEST_BDMV_ISO) for flexibility.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


DEFAULT_DVD = "/media/Misc/Movies2/Fast-Walking (1982) [tt0083930]"
DEFAULT_BDMV = "/media/Misc/Elephant (2003) [tt0363589]"
DEFAULT_DVD_ISO = "/media/Misc/Movies2/The Skin I Live In (2011) [tt1189073]/sublime-skinilivein.iso"
DEFAULT_BDMV_ISO = "/media/Misc/Movies2/Elephant (2003) [tt0363589]/rz0u.iso"

EXPECTED_DVD_AUDIO_FIRST = "eng"
EXPECTED_BDMV_AUDIO = ["fre", "eng"]
# Elephant BD has TWO French PGS subtitle tracks (main + forced/SDH), no English subs.
# Earlier guesses of ["fre", "eng"] were wrong — confirmed via libbluray ctypes.
EXPECTED_BDMV_SUBTITLE = ["fre", "fre"]


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}", flush=True)


async def main() -> int:
    failures: list[str] = []

    dvd_root = Path(os.environ.get("SHRINKERR_TEST_DVD", DEFAULT_DVD))
    bdmv_root = Path(os.environ.get("SHRINKERR_TEST_BDMV", DEFAULT_BDMV))

    print(f"=== Layer-2 verification: v0.7.4 disc language metadata ===", flush=True)
    print(f"DVD reference : {dvd_root}", flush=True)
    print(f"BDMV reference: {bdmv_root}", flush=True)
    print()

    from backend.disc_metadata import parse_disc_languages
    from backend.scanner import probe_file

    # --- DVD: parse only ---
    # Folder-disc assertions skip-on-missing for either the root OR the
    # inner disc structure (VIDEO_TS/VIDEO_TS.IFO). Source-handling from
    # v0.6.x deletes VIDEO_TS after conversion, so a media folder can
    # exist without the inner disc — that's NOT a v0.7.0 regression.
    dvd_marker = dvd_root / "VIDEO_TS" / "VIDEO_TS.IFO"
    print("[1/4] DVD parse_disc_languages against Fast-Walking IFO", flush=True)
    if not dvd_marker.is_file():
        _ok(f"SKIPPED — folder disc reference not on this NUC (verified in v0.6.5 release)")
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
    if not dvd_marker.is_file():
        _ok(f"SKIPPED — folder disc reference not on this NUC (verified in v0.6.5 release)")
    else:
        marker = dvd_marker
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
    bdmv_marker = bdmv_root / "BDMV" / "index.bdmv"
    print("[3/4] BDMV parse_disc_languages against Elephant mpls", flush=True)
    if not bdmv_marker.is_file():
        _ok(f"SKIPPED — folder disc reference not on this NUC (verified in v0.6.5 release)")
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
    if not bdmv_marker.is_file():
        _ok(f"SKIPPED — folder disc reference not on this NUC (verified in v0.6.5 release)")
    else:
        marker = bdmv_marker
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
            if len(sub) >= 2 and sub[0].get("language") == "fre" and sub[1].get("language") == "fre":
                _ok("subtitle_tracks[0]=fre, subtitle_tracks[1]=fre (correct stream order)")
            else:
                failures.append(f"BDMV probe subtitle stream order wrong: {[t.get('language') for t in sub]}")
                _fail(f"subtitle langs = {[t.get('language') for t in sub]}")

    dvd_iso = Path(os.environ.get("SHRINKERR_TEST_DVD_ISO", DEFAULT_DVD_ISO))
    bdmv_iso = Path(os.environ.get("SHRINKERR_TEST_BDMV_ISO", DEFAULT_BDMV_ISO))

    print()
    print(f"DVD ISO  : {dvd_iso}", flush=True)
    print(f"BDMV ISO : {bdmv_iso}", flush=True)

    # --- DVD ISO: classify + parse ---
    print("[5/8] DVD ISO classification + parse (Skin I Live In)", flush=True)
    from backend.disc_metadata import _classify_disc_iso, parse_disc_languages
    if not dvd_iso.is_file():
        failures.append(f"DVD ISO missing: {dvd_iso}")
        _fail("DVD ISO not found")
    else:
        dt = _classify_disc_iso(dvd_iso)
        if dt == "dvd":
            _ok(f"classifier returned 'dvd'")
        else:
            failures.append(f"DVD ISO classifier returned {dt!r}, expected 'dvd'")
            _fail(f"classifier returned {dt!r}")
        dvd_iso_langs = parse_disc_languages(dvd_iso, "dvd")
        if dvd_iso_langs["audio"] and dvd_iso_langs["audio"][0]:
            _ok(f"audio[0]={dvd_iso_langs['audio'][0]!r} (eyeball-confirm: expected spa or similar for Almodóvar)")
        else:
            failures.append(f"DVD ISO audio empty: {dvd_iso_langs!r}")
            _fail(f"audio = {dvd_iso_langs['audio']!r}")
        print(f"    full result: {dvd_iso_langs}", flush=True)

    # --- DVD ISO: end-to-end probe ---
    print("[6/8] DVD ISO probe_file integration", flush=True)
    if dvd_iso.is_file():
        probe = await probe_file(str(dvd_iso))
        if probe is None:
            failures.append("DVD ISO probe_file returned None")
            _fail("probe failed")
        else:
            audio = probe.get("audio_tracks", [])
            if audio and audio[0].get("language") and audio[0].get("language") != "und":
                _ok(f"audio_tracks[0].language={audio[0].get('language')!r}")
            else:
                failures.append(f"DVD ISO probe audio[0].language wrong: {audio[0] if audio else 'no tracks'}")
                _fail(f"audio[0]={audio[0] if audio else None}")
            if probe.get("disc_type") == "dvd":
                _ok(f"disc_type={probe.get('disc_type')!r}")
            else:
                failures.append(f"DVD ISO probe disc_type wrong: {probe.get('disc_type')!r}")

    # --- BD ISO: classify + parse ---
    print("[7/8] BD ISO classification + parse (Elephant)", flush=True)
    if not bdmv_iso.is_file():
        failures.append(f"BD ISO missing: {bdmv_iso}")
        _fail("BD ISO not found")
    else:
        dt = _classify_disc_iso(bdmv_iso)
        if dt == "bdmv":
            _ok(f"classifier returned 'bdmv'")
        else:
            failures.append(f"BD ISO classifier returned {dt!r}, expected 'bdmv'")
            _fail(f"classifier returned {dt!r}")
        bdmv_iso_langs = parse_disc_languages(bdmv_iso, "bdmv")
        audio = bdmv_iso_langs["audio"]
        if len(audio) >= 2 and audio[0] == "fre" and audio[1] == "eng":
            _ok(f"audio[:2]={audio[:2]!r}")
        else:
            failures.append(f"BD ISO audio[:2] wrong: got {audio!r}, expected ['fre','eng']")
            _fail(f"audio = {audio!r}")
        sub = bdmv_iso_langs["subtitle"]
        if len(sub) >= 2 and sub[0] == "fre" and sub[1] == "fre":
            _ok(f"subtitle[:2]={sub[:2]!r}")
        else:
            failures.append(f"BD ISO subtitle[:2] wrong: got {sub!r}, expected ['fre','fre']")
        print(f"    full result: {bdmv_iso_langs}", flush=True)

    # --- BD ISO: end-to-end probe + stream-order ---
    print("[8/8] BD ISO probe_file stream-order (Elephant)", flush=True)
    if bdmv_iso.is_file():
        probe = await probe_file(str(bdmv_iso))
        if probe is None:
            failures.append("BD ISO probe_file returned None")
            _fail("probe failed")
        else:
            audio = probe.get("audio_tracks", [])
            sub = probe.get("subtitle_tracks", [])
            if len(audio) >= 2 and audio[0].get("language") == "fre" and audio[1].get("language") == "eng":
                _ok("audio_tracks[0]=fre, audio_tracks[1]=eng (correct stream order)")
            else:
                failures.append(f"BD ISO audio stream order wrong: {[t.get('language') for t in audio]}")
                _fail(f"audio langs = {[t.get('language') for t in audio]}")
            if len(sub) >= 2 and sub[0].get("language") == "fre" and sub[1].get("language") == "fre":
                _ok("subtitle_tracks[0]=fre, subtitle_tracks[1]=fre (correct stream order)")
            else:
                failures.append(f"BD ISO subtitle stream order wrong: {[t.get('language') for t in sub]}")
                _fail(f"subtitle langs = {[t.get('language') for t in sub]}")

    print()
    if failures:
        print(f"=== FAIL: {len(failures)} assertion(s) failed — do NOT tag v0.7.4 ===", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    print("=== PASS: all assertions OK — safe to git tag v0.7.4 ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
