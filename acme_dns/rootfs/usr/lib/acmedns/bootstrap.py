"""Account pre-registration and CNAME guidance.

This is the acme-dns-client half of the app. It pre-registers an account per
configured domain and writes the goacmedns storage file that lego reads, so the
Let's Encrypt app never has to register anything itself — which matters because
its `/share` mount is read-only and a first run would otherwise fail by design
just to tell you the CNAME target.
"""

from __future__ import annotations

import json
import logging
import os
import time

from dnslib import QTYPE, DNSRecord

_LOG = logging.getLogger(__name__)

# Public resolvers used only to check the user's CNAME, never to answer queries.
CHECK_RESOLVERS = ("1.1.1.1", "8.8.8.8")

CNAME_ATTEMPTS = 30
CNAME_DELAY_SECONDS = 10


def _lookup_cname(name: str) -> str | None:
    """Return the CNAME target for `name`, or None."""
    for resolver in CHECK_RESOLVERS:
        try:
            response = DNSRecord.parse(
                DNSRecord.question(name, "CNAME").send(resolver, 53, timeout=5)
            )
        except Exception as err:  # noqa: BLE001 - resolver problems are not fatal
            _LOG.debug("CNAME lookup for %s via %s failed: %s", name, resolver, err)
            continue

        for rr in response.rr:
            if rr.rtype == QTYPE.CNAME:
                return str(rr.rdata).rstrip(".").lower()
    return None


def write_storage(cfg, entries: dict[str, dict], path: str) -> None:
    """Merge `entries` into the goacmedns storage file, atomically, mode 0600.

    Format is a flat map of domain -> account, identical between cpu/goacmedns
    (acme-dns-client) and nrdcg/goacmedns (lego), so the file is interchangeable.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    existing: dict[str, dict] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                existing = json.load(handle)
            if not isinstance(existing, dict):
                _LOG.warning("%s did not contain a JSON object; rewriting", path)
                existing = {}
        except (OSError, json.JSONDecodeError) as err:
            _LOG.warning("Could not read %s (%s); rewriting", path, err)
            existing = {}

    existing.update(entries)

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(existing, handle, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _LOG.info("Wrote %d account(s) to %s", len(existing), path)


def _account_entry(cfg, account: dict) -> dict:
    return {
        "fulldomain": cfg.fulldomain(account["subdomain"]),
        "subdomain": account["subdomain"],
        "username": account["username"],
        "password": account["password"],
        "server_url": cfg.resolved_api_base(),
    }


def run(cfg, db, storage_path: str) -> None:
    """Ensure an account exists per configured domain and report the CNAMEs."""
    if not cfg.domains:
        _LOG.warning(
            "No 'domains' configured. The server is running but no accounts were "
            "pre-registered; add your certificate domains to the app options."
        )
        return

    entries: dict[str, dict] = {}
    pending: list[tuple[str, str]] = []

    for domain in cfg.domains:
        key = cfg.account_key(domain)
        account = db.get_by_domain(key)
        if account is None:
            account = db.register(domain=key, allow_from=cfg.allow_from)
        else:
            _LOG.info("Reusing existing account for %s", key)

        entry = _account_entry(cfg, account)

        # Write the bare key and the wildcard form. lego looks the account up by
        # the ACME authorization identifier, which for a wildcard is the base
        # domain (RFC 8555 carries the wildcard as a separate flag). Storing both
        # costs nothing and removes any doubt about which string is passed.
        entries[key] = entry
        entries[f"*.{key}"] = entry

        challenge = f"_acme-challenge.{key}"
        pending.append((challenge, entry["fulldomain"]))

        _LOG.info(
            "\n"
            "──────────────────────────────────────────────────────────────\n"
            " Create this CNAME in the DNS zone for %s:\n"
            "\n"
            "   %s.  IN  CNAME  %s.\n"
            "\n"
            " Create it once; it never changes again.\n"
            "──────────────────────────────────────────────────────────────",
            key,
            challenge,
            entry["fulldomain"],
        )

    write_storage(cfg, entries, storage_path)

    _LOG.info(
        "\n"
        "Configure the Let's Encrypt app with:\n"
        "  dns:\n"
        "    provider: dns-lego\n"
        "    lego_provider: acme-dns\n"
        "    lego_env:\n"
        "      - \"ACME_DNS_API_BASE=%s\"\n"
        "      - \"ACME_DNS_STORAGE_PATH=%s\"\n"
        "    propagation_seconds: 30",
        cfg.resolved_api_base(),
        storage_path,
    )

    if cfg.verify_cname:
        verify(pending)


def verify(pending: list[tuple[str, str]]) -> None:
    """Poll until every CNAME resolves correctly, or give up.

    Bounded on purpose. The upstream client's wizard can wait indefinitely,
    which would leave this app looking healthy while stuck forever.
    """
    outstanding = list(pending)

    for attempt in range(1, CNAME_ATTEMPTS + 1):
        still_missing = []
        for challenge, expected in outstanding:
            actual = _lookup_cname(challenge)
            if actual == expected.rstrip(".").lower():
                _LOG.info("CNAME for %s is correct", challenge)
            elif actual is None:
                still_missing.append((challenge, expected))
            else:
                _LOG.error(
                    "CNAME for %s points at %s but should point at %s",
                    challenge,
                    actual,
                    expected,
                )
                still_missing.append((challenge, expected))

        outstanding = still_missing
        if not outstanding:
            _LOG.info("All CNAME records verified. Ready for certificate issuance.")
            return

        if attempt < CNAME_ATTEMPTS:
            _LOG.info(
                "Waiting for %d CNAME record(s); retry %d/%d in %ds",
                len(outstanding),
                attempt,
                CNAME_ATTEMPTS,
                CNAME_DELAY_SECONDS,
            )
            time.sleep(CNAME_DELAY_SECONDS)

    for challenge, expected in outstanding:
        _LOG.warning(
            "Giving up waiting for %s -> %s. The DNS server keeps running; "
            "create the record and restart this app to re-check.",
            challenge,
            expected,
        )
