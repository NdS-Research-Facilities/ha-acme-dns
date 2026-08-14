# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant **app repository** (`repository.yaml` at the root) containing one app, in
`acme_dns/`. The app is a from-scratch Python reimplementation of
[acme-dns](https://github.com/joohoi/acme-dns) — upstream ships a `linux_amd64` binary only,
which is why this is a reimplementation rather than a wrapper.

Home Assistant renamed "add-ons" to **apps**; upstream docs now live at
`developers.home-assistant.io/docs/apps/…` and the reference repository is
`home-assistant/apps-example`. Older material and the linter action still say "addon".

## Commands

```bash
python3 -m venv .venv
.venv/bin/pip install dnslib==0.9.26 flask==3.1.3 waitress==3.0.2
.venv/bin/python tests/smoke_test.py
```

`tests/smoke_test.py` is a single script, not a test framework — 30 checks against a live DNS
listener and a live HTTP server on `127.0.0.1:15353/18080`, no mocks. There is no way to select
one check; run the script. Keep the pinned versions identical to the Dockerfile's, so the test
exercises what ships.

Two CI checks cannot run on macOS: the app linter is a Docker action, and `apparmor_parser` is
Linux-only. Push to a branch and let CI run them rather than trying to reproduce locally.

## Architecture

The app is two halves in one container, which is the point of its design: the DNS-01 *challenge
solver* already exists inside the official Let's Encrypt app (lego speaks the acme-dns protocol),
so what was missing was a **server** plus an **account manager** to bootstrap it.

- `__main__.py` — starts the DNS listeners, spawns bootstrap on a thread, installs signal
  handlers, then blocks serving the HTTP API through waitress.
- `config.py` — reads `/data/options.json` (Supervisor-written). `DB_PATH`, `STORAGE_PATH` and
  the options path are env-overridable, which is how the smoke test redirects them.
  `account_key()` strips a leading `*.`; `resolved_api_base()` derives the URL other apps use.
- `db.py` — SQLite. Holds **two TXT values per account** and rotates them.
- `dnsserver.py` — `dnslib` resolver. Answers SOA/NS/A/TXT for its own zone only; out-of-zone
  queries get `REFUSED` and nothing is recursed, so it cannot be an open resolver or amplifier.
  Echoes EDNS0 back when offered.
- `api.py` — Flask app: `POST /update`, `GET /health`, and `POST /register` only when
  registration is enabled. Authorises by username/password and by subdomain ownership.
  `X-Forwarded-For` is deliberately ignored — there is no trusted proxy in front.
- `bootstrap.py` — pre-registers an account per configured domain, prints the CNAME the user must
  create, verifies it (bounded: 30 attempts, 10s apart), and writes the `goacmedns` storage file
  lego reads.

**Two invariants worth not breaking.** A certificate covering both `example.org` and
`*.example.org` produces two ACME authorizations whose challenge record is the *same* FQDN with
two different values needed at once — hence two TXT values per account. And the storage file is
written under **both** the base and `*.`-prefixed keys, because lego looks accounts up by the
authorization identifier, which RFC 8555 gives as the base domain plus a wildcard flag.

Runtime path: s6 `rootfs/etc/services.d/acme-dns/run` → `/usr/sbin/acme-dns` (a wrapper that
exports `PYTHONPATH=/usr/lib`) → `python3 -m acmedns`.

## Packaging constraints

- **Do not rename `acme_dns/` or its slug.** Supervisor derives the app's identity from it;
  renaming orphans existing installations.
- **The linter rejects options set to their default value.** This is why `boot: auto` is absent,
  and why "stable" is expressed by *omitting* `stage` rather than setting `stage: stable`.
- **`version` in `config.yaml` drives the published image tag.** Bump it in any PR that touches a
  monitored file (`config.yaml`, `Dockerfile`, `rootfs/`), or the merge re-pushes an existing tag
  with different content. `DOCS.md` is *not* monitored and Supervisor renders it from the
  repository rather than the image, so documentation changes need no bump and trigger no rebuild.
- **`aarch64` and `amd64` only.** 32-bit is not supportable with maintained tooling: upstream
  stopped publishing `armv7`/`i386` base images at Alpine 3.23, `prepare-multi-arch-matrix`
  accepts only these two architectures, and the legacy builder that did support 32-bit is
  deprecated and slated for removal. Evidence is in closed PR #2 — don't re-attempt it.
- Merging to `main` publishes `ghcr.io/nds-research-facilities/app-acme-dns` (public).

## Before touching `acme_dns/apparmor.txt`

Two failure modes already cost a release each:

1. **The profile attaches to `/usr/sbin/acme-dns`, an exact path outside `/usr/bin`.** Attaching
   to the interpreter needs a glob (`/usr/bin/python3.*`, since `python3` is a symlink to a
   versioned binary), and a glob cannot be merged with the outer profile's `/usr/bin/** ix` —
   `apparmor_parser` rejects it with "conflicting x modifiers". Pinning the versioned path
   compiles but silently stops confining the service when Alpine bumps Python.
2. **Directory rules need the trailing slash.** `dir/** r` does not permit `readdir` on `dir/`
   itself. Python calls `listdir()` on every `sys.path` entry, `FileFinder` swallows the `EACCES`
   and reports the directory as empty — so a missing `/usr/lib/ r` surfaced as
   `No module named acmedns`, with nothing pointing at AppArmor.

CI proves the profile *compiles*, never that it permits what the app does at runtime. AppArmor is
also not user-togglable (Supervisor reports it but exposes no setter), so a bad profile requires
shipping a new version. To diagnose one on hardware: `journalctl -k | grep -i apparmor`, or add
`complain` to the inner profile's flags temporarily.

## Repository hygiene

This repository is **public**. `MY-DEPLOYMENT.md` and `CHAT-HISTORY.txt` are gitignored because
they contain a real domain, e-mail address, public IP and LAN topology — never `git add -f` them.
`.claude/` is ignored too (machine-specific paths). Keep tracked docs on `example.org` and RFC
5737 addresses.

## Workflow notes

- Releases are tagged `1.0.0` — matching `config.yaml` and the image tag, no `v` prefix.
- PRs are **rebase-merged**, so branch SHAs are rewritten. `git branch --merged` will wrongly
  report merged branches as unmerged; use `git cherry -v main <branch>` and look for `-`.
- Pushing anything under `.github/workflows/` requires the `workflow` scope on the `gh` token
  (`gh auth refresh -h github.com -s workflow`), otherwise the push is rejected outright.
- macOS has no `timeout` binary. Wrapping a verification command in it fails as "command not
  found", and any `|| echo "…"` fallback then reports a convincing false negative — this produced
  a wrong "no TLS" diagnosis once. Use `gtimeout`, or no bound.
