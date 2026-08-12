# ACME-DNS

An acme-dns server plus account manager, so the **Let's Encrypt app can solve DNS-01
challenges without ever getting access to your real DNS zone**.

Use this when your DNS provider has no usable API — for example **strato.de**, which none of
lego's 219 providers support. Instead of handing certbot credentials to your whole zone, you
delegate exactly one name, `_acme-challenge.<yourdomain>`, to this app.

The Let's Encrypt app needs **no modification**: its built-in `dns-lego` / `acme-dns` provider
already speaks this protocol.

## How it works

```
Let's Encrypt asks for  _acme-challenge.example.org  TXT
                                   │
  your real zone (one static CNAME, created once)
                                   └──► <uuid>.auth.example.org
                                                  │
                                        this app answers, authoritatively
                                                  ▲
                                   POST /update   │  (internal Docker network only)
                                        Let's Encrypt app → certbot → lego
```

Your real zone contains a single CNAME that never changes. All churn happens inside the
delegated zone this app serves.

## Prerequisites

These are outside the app and must be in place, or nothing works:

1. **A delegated subdomain.** In your real zone, create an `NS` record delegating a subdomain
   to your own host, plus the glue `A` record:

   ```
   auth.example.org.   IN   NS   auth.example.org.
   auth.example.org.   IN   A    <your public IP>
   ```

2. **Inbound UDP/53 (and TCP/53) reachable from the internet**, forwarded to the Home
   Assistant host. Some ISPs block port 53 — verify before relying on it.

3. **A free host port.** See [Ports and the CoreDNS conflict](#ports-and-the-coredns-conflict)
   below — you do *not* need host port 53.

4. **A dynamic-DNS updater** keeping the glue `A` record current, unless your IP is static.

## Configuration

```yaml
zone: auth.example.org
nsadmin: admin@example.org
public_ip: ""
api_base: ""
domains:
  - example.org
allow_from: []
api_port: 8080
disable_registration: true
verify_cname: true
log_level: info
```

| Option | Purpose |
|---|---|
| `zone` | The delegated subdomain this app is authoritative for. Must match your `NS` record. |
| `nsadmin` | Contact address for the SOA record. `@` is converted to `.` automatically. |
| `public_ip` | Your public IP, served as the zone's own `A` record. Leave blank to detect it at startup. |
| `api_base` | URL the Let's Encrypt app should use to reach the API. Leave blank to derive it from the container hostname. |
| `domains` | Certificate domains to pre-register accounts for. Use the **base** domain for a wildcard: `example.org`, not `*.example.org`. |
| `allow_from` | Optional CIDR allow-list restricting which sources may update TXT records. |
| `api_port` | Internal HTTP port. Deliberately not published to the host. |
| `disable_registration` | Leave `true`. Accounts are created internally from `domains`; the public endpoint is not needed. |
| `verify_cname` | Poll after startup until your CNAME resolves correctly, then report it. |

## First run

1. Set `zone`, `nsadmin` and `domains`, then start the app.
2. Read the log. It prints the CNAME to create:

   ```
   Create this CNAME in the DNS zone for example.org:

      _acme-challenge.example.org.  IN  CNAME  <uuid>.auth.example.org.
   ```

3. Create that record at your DNS provider. The app polls until it resolves.
4. Confirm the server is reachable from outside:

   ```
   dig @<your-public-ip> SOA auth.example.org
   ```

5. Configure the **Let's Encrypt app** — options only, no code changes:

   ```yaml
   email: you@example.org
   domains:
     - example.org
     - "*.example.org"
   certfile: fullchain.pem
   keyfile: privkey.pem
   challenge: dns
   dns:
     provider: dns-lego
     lego_provider: acme-dns
     lego_env:
       - "ACME_DNS_API_BASE=http://local-acme-dns:8080"
       - "ACME_DNS_STORAGE_PATH=/ssl/acme-dns-accounts.json"
     propagation_seconds: 30
   test_cert: true
   ```

   The exact `ACME_DNS_API_BASE` host is printed in this app's log at startup — use that value.
   Keep `test_cert: true` until a staging certificate succeeds, because failed validations count
   against Let's Encrypt's production rate limits.

6. Once staging works, remove `test_cert` and run the Let's Encrypt app again.

## Ports and the CoreDNS conflict

**You do not need host port 53, and the app never competes with Home Assistant's own DNS
plugin for it.**

The app runs in its own network namespace, so the port it listens on *inside* the container is
private to it. Only the **host-side** mapping can collide with anything. That is why the default
is:

```yaml
ports:
  53/udp: 5354      # container 53  ->  host 5354
  53/tcp: 5354
```

On your router, forward **external UDP and TCP 53 to host port 5354**. DNS carries no port
information in its payload, so the translation is completely transparent — Let's Encrypt queries
port 53 as always and never learns about the rewrite.

```
Internet :53  ──router DNAT──►  HA host :5354  ──docker──►  container :53
```

The host port is editable in the app's **Network** panel without touching any files, so pick
whatever your router setup needs. Leave the left-hand side at `53`.

Forward **both UDP and TCP**. UDP carries essentially all real traffic — answers here are a few
hundred bytes, far below any limit — but some resolvers probe TCP, and a silently missing TCP
path is an annoying failure to diagnose later.

## Wildcards

`*.example.org` does **not** cover `example.org`. If you serve anything on the apex, request
both — and note both validate at the *same* record name, `_acme-challenge.example.org`, needing
two different TXT values at once. This app keeps two values per account for exactly that reason,
so it works out of the box. You still only need **one** CNAME.

## Why this app runs continuously

Let's Encrypt queries your DNS from several vantage points at a moment it chooses, and retries.
An authoritative server that is only up on a schedule will fail validation. So this app runs
continuously (`startup: services`) and is tiny; schedule the **Let's Encrypt app** instead, with
an automation calling `hassio.addon_start` once a day.

## Security notes

- The HTTP API is **never published to the host** — it is reachable only by other apps on the
  internal Docker network, so it needs no TLS.
- The DNS listener answers **only** for its own zone and never recurses. Out-of-zone queries get
  `REFUSED`, so it cannot be abused as an open resolver or reflection amplifier.
- `/ssl/acme-dns-accounts.json` holds credentials in cleartext at mode `0600`. It is visible to
  any app mapping `ssl`. The blast radius is limited: those credentials can only write TXT
  records in the delegated zone and cannot touch your real DNS.
- Your public IP is published in your real zone via the glue record. That is inherent to
  self-hosting DNS, not something this app adds.
- `allow_from` restricts TXT updates by source CIDR. `X-Forwarded-For` is deliberately ignored,
  since there is no trusted proxy in front of the API.

## Troubleshooting

| Symptom | Cause |
|---|---|
| App exits with "Could not bind port 53" | The *host* port in the Network panel is taken — change it (5354 is the default) rather than the container port. |
| `dig` works locally but not from outside | Port forward missing, or the ISP blocks 53. |
| Log keeps saying "Waiting for N CNAME record(s)" | The CNAME is missing or points somewhere else. |
| Let's Encrypt app: "no such domain in storage" | `domains` here must match the app's `domains` there — use the base domain for wildcards. |
| Let's Encrypt app cannot reach the API | Use the `ACME_DNS_API_BASE` value printed in this log, or the host's LAN IP. |
