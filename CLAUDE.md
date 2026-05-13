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

- **Commit messages** can be a bit longer than CHANGELOG entries (they
  document context for future archaeology) but still tight. Subject
  line under 70 chars; body, if any, explains why not what.

## Release workflow

Standard release sequence at the end of any feature/fix work:

```bash
echo "X.Y.Z" > VERSION
# edit CHANGELOG.md (one-liner under the top)
git add VERSION CHANGELOG.md <touched-files>
git commit -m "..."
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

GitHub Actions builds multi-arch images on tag push.
