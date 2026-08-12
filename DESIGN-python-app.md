# Design: single Python HA app — acme-dns server + client, driving the existing Let's Encrypt app

Supersedes §3 of `STRATEGY.md` (the "helper app" sketch). §1, §2 and §4 of that document — the
lego integration findings and the storage format — remain valid and are load-bearing here.

Context: DNS for the target domain is at **strato.de**, which has no usable DNS API
(verified: 0 of lego's 219 providers; `go-acme/lego#2197` open since 2024-05-29; the only
certbot options are web-UI scrapers needing your full Strato account password).

---

## 1. Core design decision: implement the acme-dns *protocol*, not just "an API"

The app must expose the **exact acme-dns HTTP contract**. That single decision means the
Let's Encrypt app needs **zero new code and zero modification** — lego's built-in `acme-dns`
provider already speaks it.

Protocol contract, from `joohoi/acme-dns` v2.0.2:

| Route | Auth | Request | Purpose |
|---|---|---|---|
| `POST /register` | none (disableable) | `{"allowfrom": ["1.2.3.4/32"]}` (optional) | create account |
| `POST /update` | `X-Api-User`, `X-Api-Key` headers | `{"subdomain": "...", "txt": "..."}` | set TXT value |
| `GET /health` | none | — | liveness |

Source: `pkg/api/api.go:83-86` (routes), `pkg/api/auth.go:66-67` (header names),
`pkg/acmedns/types.go:73-76` (`ACMETxtPost{subdomain, txt}`).

`/register` must respond with the account object whose field names match `goacmedns.Account`
(`STRATEGY.md` §4) plus `allowfrom`:

```json
{
  "username":   "<uuid>",
  "password":   "<40+ char secret>",
  "fulldomain": "<uuid>.auth.example.de",
  "subdomain":  "<uuid>",
  "allowfrom":  []
}
```

> Deviate from these field names and lego silently fails to parse the account. This is the
> one part of the implementation with no room for improvisation.

### Why Python rather than bundling the Go binary

`acme-dns` v2.0.2 publishes **only** `acme-dns_2.0.2_linux_amd64.tar.gz` — no `aarch64`
asset. Bundling it would restrict the app to `amd64` or force a Go toolchain into the image.
A Python implementation is architecture-neutral and satisfies the `arch: [aarch64, amd64]`
requirement directly.

---

## 2. Architecture

One app, one container, two listeners in one Python process:

```
┌───────────────────────────────────────────────────────────────┐
│ NEW APP: acme-dns (Python)                                    │
│                                                               │
│  ┌──────────────────┐        ┌───────────────────────────┐    │
│  │ DNS server       │        │ HTTP API (acme-dns proto) │    │
│  │ udp/tcp :53      │        │ :8080  — LAN/docker only  │    │
│  │ PUBLICLY exposed │        │ NOT exposed to internet   │    │
│  └────────┬─────────┘        └─────────────┬─────────────┘    │
│           │                                │                  │
│           └────────► SQLite /data/acme-dns.db ◄───────────┘   │
│                                                               │
│  bootstrap: writes /ssl/acme-dns-accounts.json (lego storage)  │
└───────────────────────────────────────────────────────────────┘
          ▲                                    ▲
          │ TXT query from Let's Encrypt       │ POST /update
          │ (public internet, udp/53)          │ (internal docker network)
          │                                    │
   Let's Encrypt validators          EXISTING Let's Encrypt app
                                     (certbot → dns-multi → lego), UNMODIFIED
```

**Key property: only UDP/53 needs public exposure.** The HTTP API stays on the internal
Docker network, reachable by the Let's Encrypt app at the app's container hostname. That is a
large reduction in attack surface versus a standard acme-dns deployment, which exposes the
API on 443.

### Configuration of the existing app (unchanged code, options only)

```yaml
dns:
  provider: dns-lego
  lego_provider: acme-dns
  lego_env:
    - "ACME_DNS_API_BASE=http://<app-hostname>:8080"
    - "ACME_DNS_STORAGE_PATH=/ssl/acme-dns-accounts.json"
  propagation_seconds: 30
```

`propagation_seconds` is a plain `sleep` (`letsencrypt/run:547` → `--dns-multi-propagation-seconds`;
`dns_multi.perform()` sleeps it). Since our own server answers the TXT query directly and
authoritatively, propagation is effectively instant — 30s is generous. **Verify the exact
container hostname** the Supervisor assigns to a local-repository app before relying on it;
fall back to the host LAN IP if resolution is awkward.

---

## 3. Hard external prerequisites — read before writing any code

These are not implementation details; they decide whether the design can work at all.

| # | Requirement | Risk |
|---|---|---|
| 1 | **NS delegation for a subdomain at Strato.** | ✅ **SATISFIED — verified in live DNS 2026-08-12.** `auth.example.org. NS auth.example.org.` plus glue `auth.example.org. A 198.51.100.1` (TTL 150) already exist in the `example.org` zone. Strato permits subdomain delegation. |
| 2 | **Public inbound UDP/53** (and ideally TCP/53) to the HA host. | ⚠ **Unconfirmed.** Nothing answers at `198.51.100.1:53` yet — UDP times out, TCP is *refused*. The RST suggests the ISP is not dropping port 53, but a port-forward to the HA host still has to be proven with a live listener. |
| 3 | **Stable public IP**, or dynamic DNS keeping the glue record current. | ⚠ The address is a residential FTTH line and the glue TTL is 150 s, which suggests dyndns management. **Confirm the A record for `auth.example.org` updates automatically**, or a WAN-IP change silently kills all validation. |
| 4 | **Host port 53 actually free.** HA OS runs its own DNS plugin (CoreDNS). | Needs verification on your system; a bind conflict is a hard failure at startup. |
| 5 | **One CNAME per certificate domain at Strato**: `_acme-challenge.example.de. CNAME <uuid>.auth.example.de.` | Standard; created once and never changes again. |

Requirement 1 is the one to settle before anything else.

### ⚠ VERIFIED ALTERNATIVE that removes prerequisite 1 entirely

**Status: confirmed working by source inspection (2026-08-12). See `DECISION-desec-vs-selfhost.md`.**

Delegate only the challenge label to **deSEC.io** with a single `CNAME` at Strato, and use the
Let's Encrypt app's **native** `dns-desec` provider. No server, no code, no open ports, no
subdomain `NS` record.

- lego follows CNAMEs **by default**: `dns01.GetChallengeInfo` sets
  `EffectiveFQDN: getChallengeFQDN(ctx, fqdn, !ok)` where `ok` comes from
  `LEGO_DISABLE_CNAME_SUPPORT` (`challenge/dns01/dns_challenge.go:211-219`). Unset ⇒ following
  is on.
- The deSEC provider writes at the CNAME target: `Present()` calls
  `d.client.Domains.GetResponsible(ctx, dns01.UnFqdn(info.EffectiveFQDN))`
  (`providers/dns/desec/desec.go`).
- The app supports it as a first-class named provider — `run.sh:224-226`, `config.yaml:49,130`,
  `DOCS.md:542-559`.

**This also satisfies your once-a-day run model, which the self-hosted design cannot** (§4).
Read the decision document before building anything here.

---

## 4. Run model: why continuous beats once-a-day

You asked for once-a-day or in-tandem. There is a correctness problem with both for the
**DNS half**:

Let's Encrypt queries `_acme-challenge...` from **multiple network vantage points**, at a
moment it chooses, and retries. If the authoritative server for that zone is down when a
query arrives, validation fails. A DNS server is only useful while it is running.

Recommended split:

| Component | Startup | Rationale |
|---|---|---|
| **This app** | `startup: services`, `boot: auto` — **runs continuously** | It is tiny (a few tens of MB). Always-available DNS is the entire point. |
| **Let's Encrypt app** | `startup: once`, `boot: manual`, triggered daily by an HA automation calling `hassio.addon_start` | Unchanged from how you run it today. |

This gives you the once-a-day behaviour you want *where it belongs* — on the certbot side —
without making DNS availability depend on scheduling.

**If you still want strict tandem operation**, it is achievable with an HA automation:
start this app → wait ~20 s for the DNS listener → start the Let's Encrypt app → wait for it
to finish → stop this app. Be aware this adds a race for no resource saving, and any retry
arriving after teardown fails. Not recommended, but not forbidden.

---

## 5. Python implementation plan

### Dependencies

| Need | Library | Note |
|---|---|---|
| DNS server | **`dnslib`** | Has a batteries-included `DNSServer` with UDP+TCP; far less code than `dnspython` for *serving*. |
| HTTP API | **`fastapi` + `uvicorn`**, or `flask` | Only 3 routes; either is fine. FastAPI gives request-model validation free. |
| Storage | **`sqlite3`** (stdlib) | Matches upstream's default engine. |
| Secrets | **`secrets`**, **`uuid`** (stdlib) | Username = UUIDv4, password = `secrets.token_urlsafe(32)`. |

No compiled dependencies → clean wheels on both `aarch64` and `amd64`.

### File layout — reusing `apps-example` structure verbatim

```
acme-dns/
├── config.yaml            new (see below)
├── Dockerfile             from apps-example + apk add python3 py3-pip + pip install
├── DOCS.md / README.md / CHANGELOG.md / translations/en.yaml
├── rootfs/
│   ├── etc/services.d/acme-dns/
│   │   ├── run            ~15 lines: bashio config → env → exec python
│   │   └── finish         COPY UNCHANGED from apps-example
│   └── usr/lib/acmedns/
│       ├── __main__.py    ~40   startup, thread launch, signal handling
│       ├── config.py      ~40   read app options from env/bashio
│       ├── db.py          ~60   SQLite schema + account/TXT CRUD
│       ├── dnsserver.py   ~120  SOA/NS/A/TXT + CNAME-target answers
│       ├── api.py         ~80   /register, /update, /health
│       └── bootstrap.py   ~60   pre-register domains, write lego storage JSON,
│                                print CNAME instructions
└── repository.yaml
```

Roughly **400 lines of Python** plus boilerplate. `finish` is copied byte-for-byte from the
example; `run` is a thin bashio→env shim.

### DNS records the server must answer

For zone `auth.example.de` with public IP `P`:

- `SOA auth.example.de.` → `nsname=auth.example.de.`, `nsadmin` with `@`→`.`
- `NS  auth.example.de.` → `auth.example.de.`
- `A   auth.example.de.` → `P`
- `TXT <uuid>.auth.example.de.` → current value(s) for that account
- `NXDOMAIN`/empty NOERROR for everything else in-zone; **REFUSED out-of-zone**

**REQUIRED: serve two TXT values per account concurrently.** Upstream does this deliberately —
`db.go:225-232` `NewTXTValuesInTransaction` inserts **two** rows per subdomain and updates
rotate between them:

```go
// NewTXTValuesInTransaction creates two rows for subdomain to the txt table
instr := fmt.Sprintf("INSERT INTO txt (Subdomain, LastUpdate) values('%s', 0)", subdomain)
_, _ = tx.Exec(instr)
_, _ = tx.Exec(instr)
```

This is not a nicety. A certificate covering both `example.org` and `*.example.org` yields
**two ACME authorizations whose challenge FQDN is the identical name**
`_acme-challenge.example.org`, each needing a *different* TXT value present at the same
moment. Keep only one value and the second `Present()` overwrites the first, so one
authorization always fails. Return both values in a single TXT response.

### Bootstrap (`bootstrap.py`) — the acme-dns-client half

On startup, for each configured domain:

1. If the domain already has an account in SQLite, reuse it.
2. Otherwise create one (uuid + secret), insert into SQLite.
3. Upsert `/ssl/acme-dns-accounts.json` in `goacmedns` format — flat map, domain → account,
   mode `0600` (`STRATEGY.md` §4). **Key must exactly match the domain certbot passes**: for
   `*.example.de`, the key is `example.de`.
4. Log the CNAME to create, then verify it resolves (bounded retry, never an infinite wait).

Because we pre-register, lego never needs to write to the storage file — but `/ssl` is
`ssl:rw` in the Let's Encrypt app anyway, so it can self-heal for a domain you add later.

### `config.yaml`

```yaml
name: ACME-DNS
version: "0.1.0"
slug: acme_dns
description: acme-dns server and account manager for the Let's Encrypt app
arch: [aarch64, amd64]
init: false
startup: services
boot: auto
map:
  - ssl:rw
ports:
  53/udp: 53
  53/tcp: 53
ports_description:
  53/udp: DNS — must be reachable from the internet
  53/tcp: DNS over TCP (large responses)
options:
  zone: "auth.example.de"
  nsadmin: "admin.example.de"
  public_ip: ""
  domains: []
  api_port: 8080
  disable_registration_after_bootstrap: true
schema:
  zone: str
  nsadmin: email
  public_ip: str?
  domains: ["str"]
  api_port: port
  disable_registration_after_bootstrap: bool?
```

Note the API port is deliberately **not** in `ports:` — it stays internal.

---

## 6. Security assessment — the honest version

This design puts **new, self-written code on the public internet on port 53**, replacing a
project with 2810 stars and six years of hardening. That is the real cost of this approach,
and it is worth stating plainly rather than discovering later:

- **Parser exposure.** A DNS server parses untrusted packets from anyone. `dnslib` is doing
  the parsing, so most of the risk is inherited from it rather than written by you — but
  malformed-packet handling, and never letting an exception kill the listener thread, is on
  you.
- **Amplification / reflection.** Refuse out-of-zone queries, never recurse, answer `ANY`
  minimally, and consider response-rate limiting. An open or chatty resolver becomes a DDoS
  reflector and gets your IP nullrouted.
- **`disable_registration` after bootstrap.** Upstream supports this
  (`pkg/api/api.go:83` guards route registration). Do the same — an open `/register` on your
  LAN is needless.
- **`allowfrom` CIDR.** Restrict each account to the Let's Encrypt app's source address.
- **Credential file.** `/ssl/acme-dns-accounts.json` is cleartext and visible to any app
  mapping `ssl`. Blast radius is limited to TXT updates on one delegated subdomain — it
  cannot touch your real Strato zone. Keep mode `0600`.
- **Exposing your home IP.** The `NS`/`A` glue records publish your residential IP in public
  DNS, permanently and searchable.

Mitigation worth considering: run the DNS listener on a VPS and keep only the API local — but
that reintroduces the infrastructure you were avoiding.

---

## 7. What this abandons from the original brief

Your first message asked for **minimal own code**, reusing the examples. That goal is met for
the *certificate* path (still zero lines — lego and the unmodified app do it all) but not for
the DNS path: ~400 lines of security-relevant Python, versus 0 lines if you use the public
`auth.acme-dns.io` or a delegated provider, or ~60 lines for the `STRATEGY.md` §3 helper on
top of an externally-run acme-dns.

That is a reasonable trade if self-hosting and single-app packaging matter more to you than
code volume — which is what you've described. Recording it so the trade-off stays visible.

---

## 8. Build order

1. **Verify prerequisite 1** — can Strato create `NS` records for a subdomain? Everything
   below is wasted if not.
2. Verify prerequisites 2-4: port-forward UDP/53, host port 53 free, public IP situation.
3. Scaffold the app from `apps-example` (skeleton, `finish`, Dockerfile, `repository.yaml`).
4. `db.py` + `api.py`: get `POST /register` and `POST /update` correct against the contract in
   §1. Test with plain `curl`.
5. `dnsserver.py`: answer SOA/NS/A/TXT. Test with `dig @<host> TXT <uuid>.auth.example.de`
   locally, then externally once forwarded.
6. `bootstrap.py`: write the lego storage JSON; create the CNAME at Strato.
7. Point the Let's Encrypt app at it with `test_cert: true` / `dry_run: true` and iterate
   safely against staging.
8. Switch to production issuance; add the daily automation for the Let's Encrypt app.
