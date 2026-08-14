# Changelog

## 1.0.0

- **Promoted to stable**, which the app linter requires be expressed by *removing* `stage`
  rather than setting it to `stable` — that is the default value, and default-valued options are
  rejected, the same rule that keeps `boot: auto` out of the file. The store drops the
  experimental warning label either way. The app has issued a real production certificate on Home Assistant
  hardware: a Let's Encrypt leaf covering both a domain and its wildcard, validated over DNS-01
  against this server, chaining to an ISRG root and verifying clean — with the DNS provider never
  holding an API credential, which is the whole point of the app.
- That exercised the case the design turns on: apex and wildcard produce two ACME authorizations
  whose challenge record is the *same* FQDN needing two different TXT values at once. Both SANs
  are present on the issued certificate, so the two-value-per-account behaviour works as intended
  against a real CA rather than only in the smoke test.
- Also verified in place: SOA answered over both UDP and TCP on the mapped host port, the zone's
  own A record tracking a dynamic public IP detected at startup, the delegation resolving through
  three independent public resolvers, and account bootstrap minting a fresh subdomain with the
  CNAME verification loop confirming it.
- 1.0.0 rather than 0.3.0 because the version now means something specific: the documented flow
  has been executed against the production Let's Encrypt API, not just tested.

  Renewal has not yet run — the first is due before the certificate expires 90 days out.

## 0.2.1

- **Fix the app failing to start with `No module named acmedns`.** The AppArmor profile granted
  `/usr/lib/acmedns/** r` but nothing for `/usr/lib/` itself. Python's import machinery calls
  `listdir()` on every `sys.path` entry — `/usr/lib` arrives via `PYTHONPATH` — and AppArmor
  mediates `readdir` through the directory's own path, which a `dir/**` rule does not cover. The
  listing was denied, `FileFinder` swallowed the `EACCES` and treated the directory as empty, so
  the denial surfaced as a plain import error naming no cause. Directory rules added for
  `/usr/lib/`, `/usr/lib/acmedns/` and `/usr/lib/python3*/`.

  Worth noting for future profile edits: the `apparmor_parser` check in CI proves a profile
  compiles, not that it permits what the app does at runtime. This one compiled cleanly.

## 0.2.0

- **Renamed to "DNS-01 Let's Encrypt challenge server".** The old name was character-for-character
  upstream `joohoi/acme-dns`, and this app is a reimplementation of that protocol rather than a
  packaging of it. Naming it after what it does follows how official apps are named
  ("NGINX Home Assistant SSL proxy", "Mosquitto broker"). The slug stays `acme_dns`, and the
  storage file, module and service names keep the acme-dns spelling because those are accurate:
  it really does speak that protocol and write the `goacmedns` storage format.
- **Pre-built images.** Installing is now a pull instead of a build on your own hardware, which
  took minutes and failed on any transient pip or apk error. Published as a multi-architecture
  manifest at `ghcr.io/nds-research-facilities/app-acme-dns`; the `app-` prefix follows
  `home-assistant/apps-example`, and avoids reading as a build of upstream `joohoi/acme-dns`,
  which this is not.
- **AppArmor profile.** The DNS listener is self-written code reachable from the internet on port
  53, so it now runs in a nested profile modelled on the official dnsmasq app: its own code is
  read-only, writes are confined to `/data` and `/ssl`, and it holds only
  `capability net_bind_service`. Network access is deliberately unrestricted — the app is a
  network daemon, so enumerating address families would buy no security while risking an opaque
  startup failure.
- **Store icon and logo**, so the app no longer renders as a blank tile.
- **Marked `stage: experimental`.** The code is covered by 30 live checks, but has still not run
  on Home Assistant hardware, and inbound UDP/53 reachability can only be verified on the target
  network. The store shows a warning label; the app remains installable.
- Continuous integration on every push and pull request: the Home Assistant app linter, an
  `apparmor_parser` syntax check (a profile that fails to compile would otherwise only break on
  real hardware), and the existing smoke test against the same pinned dependency versions the
  container installs.

## 0.1.1

- Echo EDNS0 (OPT) back when the querier offers it. dnslib's `reply()` drops the additional
  section, so previously a resolver that sent an OPT record concluded the server was
  EDNS-incapable. Answers here are far below the 512-byte fallback limit so nothing was broken
  in practice, but Let's Encrypt's validators all use EDNS and an authoritative server should
  respond correctly.
- Map the DNS listener to **host port 5354** by default instead of 53. The container has its own
  network namespace, so only the host-side port can collide with Home Assistant's CoreDNS.
  Forward external UDP+TCP 53 to 5354 on your router; the translation is transparent to DNS.

## 0.1.0

- Initial release.
- Authoritative DNS listener (UDP + TCP) serving SOA, NS, A and per-account TXT records;
  out-of-zone queries are refused so the server cannot be used as an open resolver.
- acme-dns-compatible HTTP API (`POST /register`, `POST /update`, `GET /health`), kept on the
  internal Docker network only.
- Two concurrent TXT values per account, so a certificate covering both a domain and its
  wildcard can validate.
- Account pre-registration with CNAME instructions and bounded CNAME verification.
- Writes the `goacmedns` storage file consumed by lego, under both the base and wildcard keys.
