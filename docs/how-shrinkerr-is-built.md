# How Shrinkerr is built

Shrinkerr changes the files in your library, so it's fair to ask who builds
it and how it's checked.

## Who does what

- **The maintainer designs the product.** They decide what Shrinkerr does,
  how it behaves and looks, and what goes into a release, and they run it on
  their own server and library.
- **The code is written by Claude**, Anthropic's AI model, in Claude Code:
  the backend, the interface, the tests and these docs, working from the
  maintainer's direction.

## How changes are checked

- **Tests for every change.** More than 1,300 backend tests run on every
  push. A new test is checked by breaking the code it covers and confirming
  the test then fails, so it would catch the bug coming back.
- **Data safety first.** Code that moves, replaces or deletes files is
  written so the original is never at risk before the new file is safely in
  place ([What happens to your files](what-happens-to-your-files.md)), and
  those changes get extra review passes that look for ways to lose a file.
- **The interface is checked in a browser**, against a test library of
  generated video clips, not just compiled.
- **Dependencies are audited** for known vulnerabilities (pip-audit), every
  week and whenever they change.
- **An audit** of security, data safety, performance and the interface was
  done in October 2026, and its findings were worked through for 0.10.
- **Development builds first.** Every change goes to the `:develop` images,
  which the maintainer runs on their own library, before it reaches a
  release. Releases come every few weeks, with [release notes](../CHANGELOG.md).

## What that means for you

Most of Shrinkerr's code was written by an AI. That's the reason for the
checks above, and for defaults that keep your originals while you get to know
it. Judge it by what it does and how it's tested: report bugs on
[GitHub](https://github.com/I-IAL9000/shrinkerr/issues), and security
problems privately ([SECURITY.md](../SECURITY.md)).
