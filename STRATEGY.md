# Strategy: acme-dns support for Home Assistant, reusing the existing Let's Encrypt app

Analysis of `acme-dns/acme-dns-client`, `home-assistant/apps-example`, and
`home-assistant/addons/letsencrypt` as of 2026-08-12.

---

## 1. Headline finding: most of the work is already done upstream

**The certbot ↔ acme-dns integration already exists, is shipped, and is officially
documented in the Let's Encrypt app you already have installed.**

`letsencrypt/DOCS.md` lines 372-386 give acme-dns as *the* worked example of the generic
`dns-lego` provider:

```yaml
challenge: dns
dns:
  provider: dns-lego
  lego_provider: acme-dns
  lego_env:
    - "ACME_DNS_API_BASE=http://10.0.0.8:4443"
    - "ACME_DNS_STORAGE_PATH=/share/acme-dns-accounts.json"
  propagation_seconds: 120
```

Chain of evidence that this genuinely works:

| Link | Evidence |
|---|---|
| App exposes a generic lego escape hatch | `letsencrypt/config.yaml:98` (`lego_provider: str?`), `:96` (`lego_env: [str?]`), `:152` (`dns-lego` in provider list) |
| Arbitrary `KEY=VALUE` reaches lego as env | `letsencrypt/rootfs/.../run:176-192` writes each entry to `/data/dns-multi.ini`; `certbot-dns-multi/dns_multi.py:65-69` turns every ini key except `dns_multi_provider` into a lego env var |
| lego has an acme-dns provider | `lego/providers/dns/zz_gen_dns_providers.go:235` — `case "acmedns", "acme-dns":` |
| Its env vars | `lego/providers/dns/acmedns/acmedns.go` — `ACME_DNS_API_BASE`, `ACME_DNS_STORAGE_PATH`, `ACME_DNS_STORAGE_BASE_URL`, `ACME_DNS_ALLOWLIST` |
| It performs the TXT update | `acmedns.go` `Present()` → `d.client.UpdateTXTRecord(ctx, account, info.Value)` |

**Consequence: do not port `acme-dns-client`'s certbot hook.** `pkg/client/validation.go`
(read `CERTBOT_DOMAIN` / `CERTBOT_VALIDATION`, look up account, push TXT) is exactly what
lego's provider already does inside the existing app. Reimplementing it would be pure
duplication.

---

## 2. What is actually missing

`acme-dns-client`'s feature list (README) mapped against what the installed app covers:

| acme-dns-client feature | Covered by lego in the existing app? |
|---|---|
| TXT update during DNS-01 challenge | **Yes** — fully |
| acme-dns account registration | Partially — see below |
| Guided CNAME record creation | **No** |
| CNAME verification / monitoring | **No** |
| Guided CAA record creation (RFC 8657 `accounturi`) | **No** |
| Reading the ACME account URI from certbot config for CAA | **No** |
| Config checks (`check`, `list`) | **No** |

On registration, lego's behaviour is deliberately awkward as a primary workflow
(`acmedns.go` `Present()`): on a storage miss it registers a new account, persists it, and
then **returns an error telling you to set up the CNAME**. So the first certificate attempt
always fails by design, and the only way to discover your `fulldomain` target is to go read
the JSON file afterwards.

Persisting requires **write** access to `ACME_DNS_STORAGE_PATH`, and the app's mappings are
`map: [ssl:rw, share]` (`letsencrypt/config.yaml:14-16`). A bare `share` is **read-only**:
supervisor sets `read_only = result.group(2) != "rw"` (`supervisor/apps/validate.py:397`)
and defaults `read_only: True` (`:500`). `/ssl` **is** writable.

Two consequences, and the first is a bug in the official documentation:

1. **The `/share/...` path in `DOCS.md` cannot bootstrap.** `register()` does
   `storage.Put` → `storage.Save` (`acmedns.go`), and `Save` on a read-only mount fails, so
   you never get an account. **Use `/ssl/...` instead.**
2. **On a writable path, no external registration step is needed at all** — lego registers
   and persists by itself.

And lego tells you the CNAME directly. `register()` returns `ErrCNAMERequired{Domain, FQDN,
Target}` whose `Error()` renders:

```
acme-dns: new account created for "home.example.org". To complete setup for
"home.example.org" you must provision the following CNAME in your DNS zone and
re-run this provider when it is in place:
_acme-challenge.home.example.org. CNAME a1b2c3d4-….auth.example.com.
```

That reaches the app's log in the HA UI. So "guided CNAME creation" is effectively already
covered too.

> **Revised conclusion: no new app is strictly required.** The whole use case is reachable
> by configuring the installed app alone (§2a). Build the helper app only for the
> ergonomics listed in §2b.

### 2a. The zero-code path — configuration only

1. Set the **existing** app's options, with the storage path on writable `/ssl`:

   ```yaml
   email: your.email@example.com
   domains:
     - home.example.org
   certfile: fullchain.pem
   keyfile: privkey.pem
   challenge: dns
   dns:
     provider: dns-lego
     lego_provider: acme-dns
     lego_env:
       - "ACME_DNS_API_BASE=https://auth.example.com"
       - "ACME_DNS_STORAGE_PATH=/ssl/acme-dns-accounts.json"
     propagation_seconds: 60
   ```

   Set `test_cert: true` for the bootstrap runs to stay clear of rate limits.
2. **Start** the app. It registers an acme-dns account, writes
   `/ssl/acme-dns-accounts.json`, and **fails on purpose**, printing the CNAME to create.
3. Create that CNAME in your real DNS zone.
4. **Start** it again → challenge passes, certificate issued.
5. Turn off `test_cert`, start once more for a real certificate. Renewals need nothing
   further.

Total new code: **zero**. Total new apps: **zero**.

### 2b. What the zero-code path costs you

| Friction | Detail |
|---|---|
| One deliberate failed run **per new domain** | `dns_multi.perform()` loops over challenges and the first `ErrCNAMERequired` aborts the whole run. Each run registers one more domain, so N new domains ≈ N failed runs before you can even create the CNAMEs. |
| Failed validations count against rate limits | Mitigate with `test_cert: true` / `dry_run: true` during bootstrap. |
| No CNAME pre-verification | You discover a typo'd CNAME as another failed issuance rather than a clear check. |
| No `check` / `list` audit across domains | Nothing reports which domains have accounts and whether their CNAMEs still resolve. |
| No CAA / `accounturi` guidance | Optional; not enforced in production by Let's Encrypt today. |
| Credentials created implicitly | No control over `allow_from` CIDR restriction at registration time (settable via `ACME_DNS_ALLOWLIST` in `lego_env`, but only before first registration). |

If you are setting up **one or two domains once**, take the zero-code path. The helper app
earns its keep when you have several domains, expect to add more, or want the CNAME state
auditable.

---

## 3. Optional architecture, if you want the ergonomics of §2b

Everything below is **opt-in convenience**. It does not unlock any capability that §2a lacks;
it removes the deliberate-failure bootstrap loop and adds verification and auditing.

Three cooperating parts, only the middle one being new:

```
┌─────────────────────────────┐
│ NEW: acme-dns-register app  │  run once, on demand (startup: once, boot: manual)
│  · POST /register           │
│  · write goacmedns JSON     │───┐
│  · print CNAME to create    │   │  /ssl/acme-dns/accounts.json
│  · verify CNAME via dig     │   │  (goacmedns file storage format)
└─────────────────────────────┘   │
                                  ▼
┌──────────────────────────────────────────────┐
│ EXISTING Let's Encrypt app — UNMODIFIED      │
│  dns.provider: dns-lego                      │
│  dns.lego_provider: acme-dns                 │
│  dns.lego_env: [ACME_DNS_API_BASE=...,       │
│                 ACME_DNS_STORAGE_PATH=...]   │
│  → certbot → certbot-dns-multi → lego        │
│  → reads accounts.json, updates TXT          │
└──────────────────────────────────────────────┘
```

The new app **never** runs certbot, never touches the Let's Encrypt app's `/data`, and never
writes certificate files. It only creates one JSON file and prints instructions.

### Why this satisfies "without touching the already installed app"

- Configuration happens entirely through `dns.lego_provider` / `dns.lego_env` — documented,
  supported, first-class options of the installed app.
- No fork, no image rebuild, no patched `run` script, no changed `config.yaml` schema.
- Installing the new app changes nothing until you edit the Let's Encrypt app's *own*
  options.
- Fully reversible: delete the `lego_env` lines and set `provider` back.

---

## 4. Storage format compatibility (verified)

`acme-dns-client` uses `cpu/goacmedns`; lego uses `nrdcg/goacmedns`. Their `Account` structs
are **byte-identical**:

```go
type Account struct {
	FullDomain string `json:"fulldomain"`
	SubDomain  string `json:"subdomain"`
	Username   string `json:"username"`
	Password   string `json:"password"`
	ServerURL  string `json:"server_url"`
}
```

The file container is a flat JSON map, domain → Account, written at mode `0600`
(`nrdcg/goacmedns/storage/file.go:25,53,58`). So a file written by `acme-dns-client`, by
lego, or by our own `jq` one-liner is interchangeable. Target shape:

```json
{
  "home.example.org": {
    "fulldomain": "a1b2c3d4-....auth.example.com",
    "subdomain": "a1b2c3d4-....",
    "username": "...",
    "password": "...",
    "server_url": "https://auth.example.com"
  }
}
```

### Storage key must match certbot's domain exactly

`Present()` does `d.storage.Fetch(ctx, domain)` where `domain` is what certbot passes
(`achall.domain`). Therefore:

- Keys must exactly match the entries in the Let's Encrypt app's `domains:` list.
- **Wildcards:** for `*.example.org` certbot passes `example.org`, so the key is
  `example.org` — not the wildcard string.
- A multi-domain (SAN) certificate needs **one account and one CNAME per domain key**.

---

## 5. Implementation choice for the registration step

### Option A — plain bash + `curl` + `jq` (recommended)

acme-dns registration is a single unauthenticated `POST /register` returning precisely the
fields we need. The whole app becomes ~60 lines of shell in the `apps-example` skeleton:

```bash
resp=$(curl -fsS -X POST "${SERVER}/register" \
         -H 'Content-Type: application/json' \
         -d "$(jq -nc --argjson a "${ALLOWFROM}" '{allowfrom:$a}')")

jq --arg d "${domain}" --arg s "${SERVER}" --argjson a "${resp}" \
   '.[$d] = ($a + {server_url:$s})' "${STORE}" > "${STORE}.tmp"
mv "${STORE}.tmp" "${STORE}" && chmod 600 "${STORE}"
```

Then print the CNAME and verify with `dig`:

```
_acme-challenge.${domain}.  IN  CNAME  ${fulldomain}.
```

**Why this over bundling the upstream binary:** it is less total code, has no Go toolchain
in the image, no unmaintained dependency, and no interactivity problem.

### Option B — bundle the upstream `acme-dns-client` binary

Prebuilt static binaries exist for the right architectures
(`acme-dns-client_0.3_linux_amd64.tar.gz`, `..._linux_arm64.tar.gz`), matching the app
`arch: [aarch64, amd64]`. You would gain CAA guidance and `check` / `list` for free.

But there are four real obstacles:

1. **Unmaintained.** Latest release is **v0.3, 2022-01-28** — over four years old.
   `go.mod` targets go 1.15 with `cpu/goacmedns v0.1.1`.
2. **Interactive by design.** `Register()` calls `YesNoPrompt` three times
   (`pkg/client/register.go:119,145,151`). The CNAME prompt **defaults to yes**, which
   enters `CNAMESetupWizard` and polls DNS — a hang risk in a non-TTY app container.
3. **Hardcoded storage path.** `main.go:14` — `storagepath = "/etc/acmedns/clientstorage.json"`,
   not configurable. Requires symlinking `/etc/acmedns` onto the shared volume.
4. **Refuses public instances silently.** Against `https://auth.acme-dns.io` without
   `--dangerous` it prints a warning and calls `os.Exit(0)` **without registering**
   (`register.go:57-59`).

**Recommendation: Option A.** Revisit B only if CAA/`accounturi` guidance is a hard
requirement. (If so, verify first whether `check` is genuinely non-interactive — I did not
confirm that.)

---

## 6. File plan — reuse `apps-example` verbatim

Copy `apps-example/example/` and change as little as possible. Files marked *as-is* need no
edits at all.

```
acme-dns-register/
├── config.yaml        rewrite (small)
├── Dockerfile         as-is + one apk line (curl, jq, bind-tools)
├── DOCS.md            write
├── README.md          write
├── CHANGELOG.md       as-is pattern
├── translations/en.yaml   adapt option labels
├── apparmor.txt       adapt (or drop initially)
├── icon.png / logo.png    replace
└── rootfs/etc/services.d/acme-dns-register/
    ├── run            THE ONLY REAL CODE (~60 lines)
    └── finish         as-is — copy unchanged
repository.yaml        as-is pattern
```

`rootfs/.../finish` from the example is generic s6 teardown — copy it byte-for-byte.

### `config.yaml`

```yaml
name: ACME-DNS Register
version: "1.0.0"
slug: acme_dns_register
description: Register acme-dns accounts and verify CNAME records for the Let's Encrypt app
arch:
  - aarch64
  - amd64
init: false
startup: once      # mirrors the letsencrypt app
boot: manual       # run on demand, not every boot
map:
  - ssl:rw
options:
  server: "https://auth.example.com"
  storage: "/ssl/acme-dns/accounts.json"
  domains: []
  allow_from: []
  verify_cname: true
schema:
  server: url
  storage: str
  domains:
    - str
  allow_from:
    - str?
  verify_cname: bool?
  dns_server: str?
```

### `run` logic

1. Read options via `bashio::config`; `bashio::config.require 'server'` and `'domains'`.
2. `mkdir -p "$(dirname "$STORE")"`; seed `{}` if absent; `chmod 600`.
3. Per domain: if `jq -e --arg d "$d" 'has($d)'` → log existing `fulldomain`, skip.
   Else `POST /register`, merge with `jq`, log success.
4. Print the exact CNAME line to create.
5. If `verify_cname`: `dig +short CNAME "_acme-challenge.$d"` in a **bounded** retry loop
   (e.g. 30 × 10s) — never unbounded, unlike the upstream wizard.
6. Print the ready-to-paste Let's Encrypt app YAML block.
7. Exit 0 on success so `finish` does not halt the supervision tree.

### `Dockerfile`

Keep the example's `ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23` and `COPY rootfs /`.
The `tempio` download block is unnecessary here (no templated config) — drop it and add:

```dockerfile
RUN apk add --no-cache curl jq bind-tools
```

---

## 7. Storage path decision

| Path | Let's Encrypt app access | Notes |
|---|---|---|
| `/ssl/acme-dns/accounts.json` | **read-write** (`ssl:rw`) | **Recommended, and required for §2a.** lego can register and self-heal for newly added domains. Included in HA backups. |
| `/share/acme-dns-accounts.json` | **read-only** (bare `share`) | What `DOCS.md` shows. Viable **only** with external pre-registration; lego can never write here, so the zero-code path of §2a fails on this path. |

Use `/ssl/acme-dns/accounts.json` unless you have a reason not to — and keep both apps
pointing at the identical string.

**Security note:** the file holds acme-dns credentials in cleartext and is visible to any
app mapping the same folder. Blast radius is limited (each credential can only update TXT
for its own acme-dns subdomain, and cannot touch your real zone), but keep mode `0600` and
prefer `allow_from` CIDR restrictions to your HA host's egress IP.

---

## 8. Resulting user workflow

1. Install the new app from your repository; set `server` and `domains`; **Start**. Read the
   log.
2. Create the printed CNAME records in your real DNS zone (one per domain).
3. Restart the new app to confirm CNAMEs resolve correctly (bounded verification).
4. Edit the **existing** Let's Encrypt app's options to the `dns-lego` block in §1, pointing
   `ACME_DNS_STORAGE_PATH` at the same file. Start it.
5. Renewals thereafter need no further action.

---

## 9. Risks and caveats

- **`propagation_seconds` is a plain sleep.** `run:547` passes
  `--dns-multi-propagation-seconds`, and `dns_multi.perform()` simply `sleep`s it. acme-dns
  serves the TXT immediately, so the documented `120` is very conservative — a lower value
  shortens every issuance. (DOCS.md also claims `propagation_seconds` generates a
  `<PROVIDER>_PROPAGATION_TIMEOUT` env var; that does not happen in this version's `run`
  script, so the hyphen in `ACME-DNS` is a non-issue.)
- **Test against Let's Encrypt staging first** using the existing app's `test_cert: true` /
  `dry_run: true` options — no new code needed to get a safe rehearsal.
- **Registration may be disabled** on the acme-dns instance (`/register` is commonly
  IP-restricted or turned off). Fail loudly with the HTTP status.
- **CAA/`accounturi` guidance is out of scope** in Option A. If you use a public acme-dns
  instance, note the upstream warning: the instance operator can obtain certificates for
  your domain. Self-hosting acme-dns is the better mitigation.
- **Version drift.** The `dns-lego` escape hatch is the contract we depend on; it is
  documented, but pin the Let's Encrypt app version you validated against
  (currently `6.4.0`) and re-test after major upgrades.
- **`init: false`** is required in both apps — the example and the letsencrypt app both set
  it, since s6-overlay is the entrypoint.

---

## 10. Effort summary

| Component | §2a zero-code | §3 helper app |
|---|---|---|
| DNS-01 challenge / TXT update | 0 — lego, already installed | 0 |
| Certificate issuance & renewal | 0 — existing app, unmodified | 0 |
| Account registration | 0 — lego self-registers | ~25 lines bash |
| CNAME instructions | 0 — lego's error message | ~5 lines bash |
| CNAME verification, audit, `allow_from` | **not available** | ~30 lines bash |
| App skeleton, s6 service, teardown | n/a | 0 — copied from `apps-example` |
| Docs / metadata / icons | n/a | boilerplate |

**The requested use case needs no new code and no new app.** `acme-dns-client`'s two
essential functions — solving the DNS-01 challenge through acme-dns, and telling you which
CNAME to create — are both already present in the Let's Encrypt app you have installed, via
lego. The helper app is a convenience layer over a one-time bootstrap, worth ~60 lines of
bash only if the §2b frictions bother you.
