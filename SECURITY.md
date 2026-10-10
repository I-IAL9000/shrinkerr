# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue:
**[Report a vulnerability](https://github.com/I-IAL9000/shrinkerr/security/advisories/new)**
(GitHub's private reporting). Include the version (Settings → System →
Updates shows it), how to reproduce it, and what an attacker could do with it.

You'll get a reply within a week. A confirmed problem is fixed in a hotfix
release when it's serious, otherwise in the next release, and credited in
the [CHANGELOG](CHANGELOG.md) under **Security** unless you'd rather not be
named.

## Supported versions

Only the latest release gets security fixes. `:develop` images get them
first.

## What Shrinkerr protects, and how

[docs/security.md](docs/security.md) describes the threat model, sign-in and
API keys, how secrets are stored, the path and network checks, and a
hardening checklist for exposing Shrinkerr outside your home network.
