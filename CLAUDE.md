# Project preferences for Claude Code

These override skill defaults for the Shrinkerr repo. Read on session start.

## File locations

- **Superpowers specs and plans** — write to `.superpowers/specs/` and
  `.superpowers/plans/`, not the skill default `docs/superpowers/`. The
  `docs/` directory is user-facing documentation only.

## Writing conventions

- **CHANGELOG entries are one-liners.** State what changed and (briefly)
  why. No implementation details, no "root cause" essays, no
  multi-bullet sub-sections. If a change has a genuine compat break or
  migration note, bold it and add one extra short sentence — not a
  paragraph. See existing v0.5.x entries for the target shape.

- **The "What's new" dialog is built from the CHANGELOG** (v0.10.0): it
  shows every Added / Changed entry, and only the Fixed / Security /
  Removed entries that start with a **bold lead**, as one line each — the
  bold text, or an Added entry's first sentence. So start notable fixes
  with a short bold lead (a few words, not the whole entry); minor fixes
  stay plain and are just counted.

- **Commit messages** can be a bit longer than CHANGELOG entries (they
  document context for future archaeology) but still tight. Subject
  line under 70 chars; body, if any, explains why not what.

## Release workflow

Two channels (since v0.9.156, 2026-10-08). Users get an "update available"
notice only for real releases, every few weeks — not for every fix.

- **`develop`** — all day-to-day work. Every push runs the tests and builds
  `:develop` / `:develop-nvenc` images (VERSION shown as
  `X.Y.Z-dev.<run>+<sha>`). The maintainer's NUC runs `:develop-nvenc`.
- **`main`** — released code only. The stable `:latest` / `:nvenc` images move
  only when a `vX.Y.Z` tag is pushed (pushing `main` alone builds nothing).
  A release tag also moves the minor line (`:X.Y[-nvenc]`; patches only).

The in-app update notice follows the channel: a release hears about newer
releases (GitHub `releases/latest`), a `-dev` build about newer successful
`develop` image builds. Users can turn the check off.

`VERSION` on `develop` holds the **next** release number (e.g. `0.10.0`).

### Day-to-day (any fix or feature)

```bash
git checkout develop
# change + tests (backend pytest; `npm run build` for frontend changes)
# CHANGELOG.md: add a one-liner under `## [Unreleased]` — and under
#   `### Upgrade notes` if users must do something after updating (a
#   changed default, a removed or renamed setting, a new requirement)
git add <touched-files>
git commit -m "..."
git push origin develop
```

Do **not** bump VERSION, tag, or push `main` for normal work.

### Release day (only when the user asks for a release)

```bash
git checkout develop
# CHANGELOG.md: rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`
#   (VERSION already says X.Y.Z); group lines under
#   ### Upgrade notes / Added / Changed / Fixed / Security
#   (Upgrade notes first, in full sentences: the What's new dialog shows them
#   first, as written; the other sections by their bold leads)
git commit -am "release: vX.Y.Z"
git checkout main && git merge --ff-only develop
git tag vX.Y.Z
git push origin main && git push origin vX.Y.Z
git checkout develop
# bump VERSION to the next planned version, add a fresh `## [Unreleased]`
git commit -am "chore: start X.Y+1.0" && git push origin develop
```

GitHub Actions then builds the release images and creates the GitHub Release
(which triggers the in-app update notice).

### Hotfix (security or data-loss only, with the user's OK)

```bash
git checkout main
# fix + regression test
echo "X.Y.Z" > VERSION          # patch bump of the last release
# CHANGELOG.md: new `## [X.Y.Z] — date` section at the top
git add ... && git commit -m "fix: ..."
git tag vX.Y.Z && git push origin main && git push origin vX.Y.Z
git checkout develop && git merge main   # keep develop's VERSION; keep both changelog sections
git push origin develop
```

Optional pre-release for testers: tag `vX.Y.Z-rc.N` on `develop` → GitHub
pre-release + `:vX.Y.Z-rc.N[-nvenc]` images; stable users are not notified.
