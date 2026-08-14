# Changelog

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
