"""Configuration for the acme-dns app.

The Supervisor writes the user's options to /data/options.json, so we read that
file directly instead of shelling out to bashio once per option.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.request
from dataclasses import dataclass, field

OPTIONS_PATH = os.environ.get("ACMEDNS_OPTIONS", "/data/options.json")
DB_PATH = os.environ.get("ACMEDNS_DB", "/data/acme-dns.db")
STORAGE_PATH = os.environ.get("ACMEDNS_STORAGE", "/ssl/acme-dns-accounts.json")

# Used only when public_ip is left blank. Failure is not fatal: the zone's own
# A record is a convenience, since resolvers normally use the glue record in the
# parent zone to reach us in the first place.
IP_LOOKUP_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)

LOG_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_LOG = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when the user's options cannot produce a working server."""


@dataclass
class Config:
    """Validated app options."""

    zone: str
    nsadmin: str
    domains: list[str] = field(default_factory=list)
    allow_from: list[str] = field(default_factory=list)
    public_ip: str = ""
    api_base: str = ""
    api_port: int = 8080
    disable_registration: bool = True
    verify_cname: bool = True
    log_level: str = "info"

    @property
    def soa_rname(self) -> str:
        """SOA RNAME. acme-dns writes the admin address with '@' as '.'."""
        return self.nsadmin.replace("@", ".")

    def account_key(self, domain: str) -> str:
        """Storage key for a domain.

        lego looks up the account by the ACME authorization identifier. For a
        wildcard certificate RFC 8555 uses the base domain with a separate
        `wildcard` flag, so `*.example.org` must be stored as `example.org`.
        """
        return domain[2:] if domain.startswith("*.") else domain

    def fulldomain(self, subdomain: str) -> str:
        return f"{subdomain}.{self.zone}"

    def resolved_api_base(self) -> str:
        """Base URL other apps use to reach our HTTP API.

        Informational only for lego, which takes ACME_DNS_API_BASE from its own
        environment, but it is written into the storage file for compatibility
        with acme-dns-client and shown in the setup instructions.
        """
        if self.api_base:
            return self.api_base.rstrip("/")
        return f"http://{socket.gethostname()}:{self.api_port}"


def _detect_public_ip() -> str:
    for url in IP_LOOKUP_URLS:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                ip = response.read().decode("utf-8").strip()
            if ip:
                _LOG.info("Detected public IP %s via %s", ip, url)
                return ip
        except Exception as err:  # noqa: BLE001 - any failure just means try next
            _LOG.debug("Public IP lookup via %s failed: %s", url, err)

    _LOG.warning(
        "Could not determine the public IP. The zone's own A record will not be "
        "served. Set the 'public_ip' option to silence this."
    )
    return ""


def load() -> Config:
    """Read, validate and normalise the app options."""
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as err:
        raise ConfigError(f"Options file {OPTIONS_PATH} not found") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"Options file {OPTIONS_PATH} is not valid JSON: {err}") from err

    zone = str(raw.get("zone", "")).strip().rstrip(".").lower()
    if not zone or "." not in zone:
        raise ConfigError(f"'zone' must be a fully qualified domain, got {zone!r}")

    nsadmin = str(raw.get("nsadmin", "")).strip()
    if not nsadmin:
        raise ConfigError("'nsadmin' is required")

    domains = [str(d).strip().rstrip(".").lower() for d in raw.get("domains") or []]
    domains = [d for d in domains if d]

    cfg = Config(
        zone=zone,
        nsadmin=nsadmin,
        domains=domains,
        allow_from=[str(c).strip() for c in raw.get("allow_from") or [] if str(c).strip()],
        public_ip=str(raw.get("public_ip", "")).strip(),
        api_base=str(raw.get("api_base", "")).strip(),
        api_port=int(raw.get("api_port", 8080)),
        disable_registration=bool(raw.get("disable_registration", True)),
        verify_cname=bool(raw.get("verify_cname", True)),
        log_level=str(raw.get("log_level", "info")).lower(),
    )

    if not cfg.public_ip:
        cfg.public_ip = _detect_public_ip()

    return cfg


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=LOG_LEVELS.get(level, logging.INFO),
        format="[%(levelname)s] %(name)s: %(message)s",
    )
    # Waitress logs every request at INFO, which is noise for a service that
    # handles a couple of requests per day.
    logging.getLogger("waitress").setLevel(logging.WARNING)
