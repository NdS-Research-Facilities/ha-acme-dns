# Changelog

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
