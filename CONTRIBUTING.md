# Contributing to Shrinkerr

Thanks for helping. Bug reports, ideas, translations and code are all
welcome.

## Reporting a bug

[Open an issue](https://github.com/I-IAL9000/shrinkerr/issues/new/choose)
with the bug report form. The most useful thing to attach is the
**diagnostics zip** from the **Doctor** page: the version, the Doctor's
checks, your settings, recent log lines and failed jobs, with API keys,
tokens and passwords removed. File paths are left in, so look it over before
attaching it. Say which image you run (`:latest`, `:nvenc`, `:develop`…) and
what you expected to happen.

Security problems go to the private form instead: see [SECURITY.md](SECURITY.md).

## Suggesting a feature

Use the feature request form. Describe the problem you have before the
solution you'd like: what you're trying to do with your library, and what
gets in the way.

## Translating

Shrinkerr's interface is in English and Spanish. To add a language:

1. Copy `frontend/src/i18n/locales/en/` to `frontend/src/i18n/locales/<code>/`
   (e.g. `de`) and translate the values, not the keys. Keep `{{placeholders}}`
   and tags such as `<b>…</b>` or `<link>…</link>` as they are.
2. Add the language to `LANGUAGES` in `frontend/src/i18n/index.ts`.
3. For notifications and Home Assistant entity names, copy
   `backend/locales/en/` to `backend/locales/<code>/` too.
4. Plurals use i18next's suffixes: `_one` and `_other`, plus `_many` for
   languages whose plural rules have it (Spanish does). If yours does, add it
   to `_MANY_LANGS` in `backend/i18n.py`.

A missing key shows the English text, so a partial translation still works.
Improvements to the Spanish are welcome too.

## Changing code

Shrinkerr is a FastAPI backend (Python 3.11+) and a React + TypeScript
frontend. The README's [Development](README.md#development) section has the
local setup.

- **Branch from `develop` and open pull requests against `develop`.**
  `main` only holds released code.
- **Backend:** run `python -m pytest backend/tests -q`. Add a test for what
  you change, and check it fails without your change. Code that moves,
  renames or deletes media files needs a test for the failure case too
  (what happens to the original when something goes wrong).
- **Frontend:** run `npm run build` in `frontend/` (it type-checks the
  whole app; `tsc --noEmit` alone misses errors). Every string shown to
  users goes in the English catalog, with a Spanish entry next to it (the
  tests check this for messages the server sends).
- **CHANGELOG:** add one line under `## [Unreleased]` saying what changed.
  If users must do something after updating, add it under
  `### Upgrade notes` too.
- Keep pull requests to one change. Formatting-only changes to code you
  didn't otherwise touch make review harder.

## License

Contributions are released under the [Apache License 2.0](LICENSE), like
the rest of Shrinkerr.
