# ha-acme-dns

A Home Assistant app repository containing the **DNS-01 Let's Encrypt challenge server**: a self-hosted
[acme-dns](https://github.com/joohoi/acme-dns) server *and* account manager in one app, so the
official **Let's Encrypt app can solve DNS-01 challenges without ever getting credentials for
your real DNS zone**.

Built for DNS providers with no usable API. **strato.de** is the motivating case: none of lego's
219 DNS providers support it, and the only certbot options are community hooks that scrape
Strato's web control panel using your full account password.

The Let's Encrypt app is **not modified, forked, or patched**. It is configured through its own
documented `dns-lego` / `acme-dns` options, because lego already speaks this protocol.

## Installation

Home Assistant → **Settings → Add-ons → Add-on store → ⋮ → Repositories**, add:

```
https://github.com/NdS-Research-Facilities/ha-acme-dns
```

Then install **DNS-01 Let's Encrypt challenge server** and follow
[`acme_dns/DOCS.md`](acme_dns/DOCS.md).

## How it works

```
Let's Encrypt asks for  _acme-challenge.example.org  TXT
                                   │
  your real zone (one static CNAME, created once)
                                   └──► <uuid>.auth.example.org
                                                  │
                                        this app answers, authoritatively
                                                  ▲
                                   POST /update   │  internal Docker network only
                                        Let's Encrypt app → certbot → lego
```

Your real zone holds a single CNAME that never changes again. Only UDP/53 is exposed publicly;
the HTTP API never leaves the internal Docker network, so it needs no TLS.

## Design notes

The documents in this repository record *why* the app is shaped the way it is, including the
options that turned out to need no code at all:

| Document | Contents |
|---|---|
| [`STRATEGY.md`](STRATEGY.md) | Analysis of `acme-dns-client`, `apps-example` and the Let's Encrypt app. Shows the challenge-solving half already ships in the Let's Encrypt app, and documents a **zero-code** path plus a bug in the official docs (`/share` is read-only, so the documented storage path cannot bootstrap). |
| [`DESIGN-python-app.md`](DESIGN-python-app.md) | Design of this app: protocol contract, architecture, prerequisites, run model, and an honest security assessment of exposing self-written code on port 53. |
| [`DECISION-desec-vs-selfhost.md`](DECISION-desec-vs-selfhost.md) | The alternative worth reading **before** you self-host: delegating the challenge label to a free API-capable provider needs no server, no code and no open ports. Verified against lego's CNAME-following behaviour. |

If you only want a certificate and do not specifically want to run DNS yourself, read
`DECISION-desec-vs-selfhost.md` first. It may save you this entire app.

## Features

- Authoritative DNS listener (UDP + TCP) serving SOA, NS, A and per-account TXT.
- Out-of-zone queries are **refused** and nothing is ever recursed, so the server cannot be used
  as an open resolver or a reflection amplifier.
- **Two concurrent TXT values per account**, so one certificate can cover both `example.org` and
  `*.example.org` — they validate at the same record name with different values.
- acme-dns-compatible HTTP API (`POST /register`, `POST /update`, `GET /health`).
- Account pre-registration, printed CNAME instructions, and **bounded** CNAME verification.
- Writes the `goacmedns` storage file lego reads, under both the base and wildcard keys.
- Pure Python, no compiled dependencies — works on `aarch64` and `amd64`. (Upstream acme-dns
  v2.0.2 publishes only a `linux_amd64` binary, which is why this is a reimplementation rather
  than a wrapper.)

## Tests

```
python3 -m venv .venv
.venv/bin/pip install dnslib==0.9.26 flask==3.1.3 waitress==3.0.2
.venv/bin/python tests/smoke_test.py
```

30 checks against a live DNS listener and a live HTTP server — no mocks. Covers zone answers,
out-of-zone refusal, negative caching, EDNS0 echoing, the two-value rotation, credential and
cross-account authorisation enforcement, storage-file shape and permissions, and bootstrap
idempotency.

## Ports

The app maps container port 53 to **host port 5354** by default, so it never competes with Home
Assistant's own DNS plugin — the container has its own network namespace, and only the host-side
number can collide. Forward external UDP **and** TCP 53 to host 5354 on your router; DNS carries
no port information in its payload, so the translation is transparent.

```
Internet :53  ──router DNAT──►  HA host :5354  ──docker──►  container :53
```

The host port is editable in the app's **Network** panel.

## Status

**Running in production.** As of 2026-08-15 the app has issued a real Let's Encrypt certificate
on Home Assistant hardware, covering both a domain and its wildcard, validated over DNS-01
against this server — with the DNS provider never holding an API credential. Marked
`stage: stable` as of 1.0.0.

The one environment-specific prerequisite remains inbound UDP/53 reachability to your router,
which no amount of testing here can establish for your network — see `DESIGN-python-app.md` §3.
Renewal has not yet been exercised; the first falls due 90 days after issuance.

## License

MIT — see [LICENSE](LICENSE).
