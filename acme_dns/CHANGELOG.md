# Changelog

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
