# Decision: deSEC CNAME delegation vs. self-hosted acme-dns app

Investigated 2026-08-12. Verdict: **deSEC delegation is preferable** for this setup
(domain at strato.de, Home Assistant on a home connection). Evidence below; the
counter-arguments for self-hosting are in §5.

---

## 1. It works — verified in source, not assumed

Two independent facts had to hold. Both do.

**(a) lego follows CNAMEs by default.** `challenge/dns01/dns_challenge.go:205-221`:

```go
ok, _ := strconv.ParseBool(os.Getenv("LEGO_DISABLE_CNAME_SUPPORT"))
fqdn := getAuthorizationDomainName(domain)
return ChallengeInfo{
	Value:         value,
	FQDN:          getChallengeFQDN(ctx, fqdn, false),
	EffectiveFQDN: getChallengeFQDN(ctx, fqdn, !ok),   // ← follows CNAME unless disabled
	Prefix:        challengeLabel,
}
```

`LEGO_DISABLE_CNAME_SUPPORT` is unset ⇒ `ok == false` ⇒ `!ok == true` ⇒ CNAME resolution is
**on**. `EffectiveFQDN` is documented as "the resulting FQDN after the CNAMEs resolutions"
(`:179`).

**(b) The deSEC provider writes at the CNAME target, not the original name.**
`providers/dns/desec/desec.go`, `Present()`:

```go
info := dns01.GetChallengeInfo(ctx, domain, keyAuth)
responsibleDomain, err := d.client.Domains.GetResponsible(ctx, dns01.UnFqdn(info.EffectiveFQDN))
recordName, err := dns01.ExtractSubDomain(info.EffectiveFQDN, responsibleDomain.Name)
```

It resolves which **deSEC** zone is responsible for the *effective* FQDN and creates the TXT
there. Exactly the behaviour required.

**(c) The Let's Encrypt app supports deSEC natively** — not even via the `dns-lego` escape
hatch: `config.yaml:49` (`desec_token: str?`), `:130` (`dns-desec` in the provider list),
`run.sh:224-226` (maps to `DESEC_TOKEN`), `DOCS.md:542-559` (documented).

### Resulting flow

```
Let's Encrypt asks for: _acme-challenge.example.de   TXT
                                 │
Strato zone (static, set once):  └── CNAME → _acme-challenge.example.dedyn.io
                                                        │
lego: EffectiveFQDN = _acme-challenge.example.dedyn.io  │
      GetResponsible → deSEC zone example.dedyn.io      │
      writes TXT via deSEC API ─────────────────────────┘
                                                        │
Let's Encrypt follows the CNAME and reads the TXT at deSEC ✓
```

Your Strato zone holds **one static CNAME that never changes again**. All dynamic record
churn happens in a deSEC zone that contains nothing else.

---

## 2. Configuration — the entire implementation

At Strato, once (per certificate domain):

```
_acme-challenge.example.de.   IN   CNAME   _acme-challenge.example.dedyn.io.
```

In the **existing** Let's Encrypt app:

```yaml
email: you@example.de
domains:
  - example.de
certfile: fullchain.pem
keyfile: privkey.pem
challenge: dns
dns:
  provider: dns-desec
  desec_token: <deSEC API token>
  propagation_seconds: 60
```

That is the whole thing. No new app, no Python, no ports, no `NS` records.

You do not even need to move a domain to deSEC — a free `*.dedyn.io` account provides a zone
you fully control via API, which is all the CNAME target needs to be.

---

## 3. Why this is preferable — point by point

| Dimension | Self-hosted Python acme-dns app | deSEC delegation |
|---|---|---|
| **Strato subdomain `NS` record** | **Required — unverified, may be impossible** | Not needed (CNAME only, universally supported) |
| New code to write & maintain | ~400 lines, security-relevant | **0** |
| New HA app | 1 | **0** |
| Internet-exposed ports | **UDP/53 from your home** | **none** |
| Home IP published in public DNS | Yes, permanently, via `NS`/`A` glue | No |
| Dynamic-IP handling | Needed (dyndns for glue record) | Not applicable |
| Must run 24/7 | **Yes** — DNS must answer whenever LE queries | No server at all |
| **Your "once a day" run model** | **Incompatible** (see §4) | **Fully satisfied** |
| ISP blocking port 53 | Common failure mode | Irrelevant |
| Host port 53 vs HA's CoreDNS | Possible bind conflict | Irrelevant |
| DDoS-reflection risk you own | Yes — must refuse out-of-zone, no recursion | None |
| DNSSEC on challenge zone | You'd implement it (or skip it) | On by default at deSEC |
| Ongoing maintenance | Yours | None |
| Third-party trust | **None** | deSEC (see §5) |

### ⚠ UPDATE 2026-08-12 — argument 1 below is void; argument 3 is weakened

Measured against live DNS, Strato **does** permit subdomain `NS` delegation with glue, and the
delegation for `auth.example.org` already exists:

```
auth.example.org.  150  IN  NS  auth.example.org.
auth.example.org.  150  IN  A   198.51.100.1   (glue in parent zone example.org)
```

`198.51.100.1` reverse-resolves to `<reverse-dns-of-a-residential-line>` — a residential
FTTH line in NL. Consequences for this decision:

- **Argument 1 (unverified blocker) no longer applies.** The prerequisite is satisfied.
- **Argument 3 (home network exposure) is weakened.** That residential IP is *already*
  published in public DNS, so the self-hosted design no longer *adds* that disclosure. Opening
  inbound 53 and running a self-written DNS parser is still new exposure.
- **Argument 2 (the once-a-day run model) stands unchanged** and is now the main technical
  argument for deSEC.

Current state of that delegation: **lame — nothing answers there yet.** `UDP/53` times out and
`TCP/53` returns *connection refused*. The refusal is mildly encouraging: an RST means packets
reach your premises rather than being silently dropped by the ISP, so inbound 53 is probably not
filtered. It does **not** prove a port-forward to the HA host exists — that still needs testing
with a live listener. Until something answers, any DNS-01 validation through this path fails.

**1. ~~It removes the unverified blocker.~~ VOID — see update above.** Retained for history:
the self-hosted design hinges on Strato permitting `NS` records for a subdomain, which budget
registrars often disallow. Measurement showed Strato permits it.

**2. It is the only option that matches the run model you asked for.** You wanted an app that
starts once a day, or in tandem with the Let's Encrypt app. A self-hosted authoritative DNS
server *cannot* work that way — Let's Encrypt queries from multiple vantage points at a time it
chooses and retries; if your server is down when a query lands, validation fails. I had to talk
you out of that in `DESIGN-python-app.md` §4. With deSEC there is no server to schedule, so the
Let's Encrypt app alone runs on your daily automation and the architecture you originally
described works exactly as you described it.

**3. It removes your home network from the attack surface.** No inbound port, no published
residential IP, no self-written packet parser facing the internet, no reflection-amplification
exposure. Compare the honest risk list in `DESIGN-python-app.md` §6 — all of it disappears.

---

## 4. Security comparison, stated fairly

The two options are **equivalent** on the axis that matters most, and differ on one other.

**Equivalent — blast radius.** In both designs your real Strato zone is never writable by the
ACME tooling. A compromised credential can only alter TXT records in a throwaway delegated
zone. The deSEC token can rewrite `example.dedyn.io`, which contains nothing but challenge
records — the same scope an acme-dns account has. deSEC additionally supports **scoping tokens
to specific domains and subnames**, which can make it tighter than an acme-dns account.

**Different — third-party trust.** Whoever controls the zone your `_acme-challenge` delegates
into can, in principle, satisfy a DNS-01 challenge for your domain and obtain a certificate for
it. With deSEC that is deSEC (a registered German non-profit operating a DNSSEC-signed public
resolver). With **self-hosted** acme-dns it is nobody but you. This is the single genuine
advantage of self-hosting, and it is the same trust question as using public
`auth.acme-dns.io` — which was the alternative you'd otherwise land on if the Strato `NS`
record turns out to be unavailable.

If that trust delta matters to you, the mitigation is a CAA record with `accounturi`
(RFC 8657) rather than 400 lines of Python — and note Let's Encrypt does not enforce
`accounturi` in production today.

**Availability trust.** If deSEC is down, a renewal attempt fails. Certificates are valid 90
days and the app retries daily, so the tolerance is weeks. Set against the obligation to keep
your own DNS server reachable 24/7 from a residential connection, deSEC is almost certainly the
more available option.

---

## 5. When self-hosting would still be right

Choose `DESIGN-python-app.md` instead if:

- You have a hard requirement that **no third party** can ever satisfy a challenge for your
  domain, and CAA/`accounturi` is not an acceptable mitigation.
- You already run a VPS with a public IP and a delegable zone, making prerequisites 1-4 free.
- Building the acme-dns server is itself a goal (learning, or you want it for other hosts too),
  independent of it being the cheapest way to get this certificate.

In those cases the design is sound and stands as written — but note that even then, running
acme-dns on a **VPS** rather than the HA box removes most of the risks in §6 of that document.

---

## 6. Recommendation

1. Create a free deSEC account and a `dedyn.io` zone; generate an API token (scope it to that
   zone).
2. Add the single `CNAME` at Strato.
3. Set the four `dns:` options in the existing Let's Encrypt app, with `test_cert: true`.
4. Start it, confirm a staging certificate, then remove `test_cert` and issue for real.
5. Keep your daily automation on the Let's Encrypt app. Nothing else runs.

Cost: one CNAME, one token, four config lines. **Zero code.** Delete
`DESIGN-python-app.md` from the plan unless one of the §5 conditions applies to you.
