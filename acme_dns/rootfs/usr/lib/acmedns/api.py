"""acme-dns HTTP API.

Implements the upstream contract exactly, because that is what lets lego's
built-in `acme-dns` provider drive this server with no client-side code:

    POST /register   optional {"allowfrom": [...]}      -> account object
    POST /update     X-Api-User / X-Api-Key headers,
                     {"subdomain": ..., "txt": ...}     -> echo
    GET  /health                                        -> 200

Field names in the /register response must match goacmedns.Account or lego
silently fails to parse the stored account.
"""

from __future__ import annotations

import ipaddress
import logging
import re

from flask import Flask, jsonify, request

_LOG = logging.getLogger(__name__)

# A dns-01 value is base64url-encoded SHA-256: 43 characters, no padding.
# Kept as a charset+length check rather than a hard equality so a future ACME
# change does not lock the server out, while still refusing junk.
TXT_RE = re.compile(r"^[A-Za-z0-9_-]{20,255}$")


def _client_ip() -> str:
    """Direct peer address.

    X-Forwarded-For is deliberately ignored: this API is only reachable on the
    internal Docker network, so there is no trusted proxy in front of it and
    honouring the header would let a caller forge its own source address.
    """
    return request.remote_addr or ""


def _allowed_from(account: dict) -> bool:
    networks = account.get("allowfrom") or []
    if not networks:
        return True

    peer_raw = _client_ip()
    try:
        peer = ipaddress.ip_address(peer_raw)
    except ValueError:
        _LOG.warning("Rejecting update: unparseable peer address %r", peer_raw)
        return False

    for entry in networks:
        try:
            if peer in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            _LOG.warning("Ignoring malformed allow_from entry %r", entry)

    _LOG.warning("Rejecting update from %s: not in allowfrom %s", peer, networks)
    return False


def create_app(cfg, db) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return "", 200

    @app.post("/update")
    def update():
        username = request.headers.get("X-Api-User", "")
        password = request.headers.get("X-Api-Key", "")

        account = db.get_by_username(username) if username else None
        # Compare unconditionally so a missing account and a wrong password take
        # the same path out.
        if account is None or account["password"] != password:
            _LOG.warning("Rejected update: bad credentials from %s", _client_ip())
            return jsonify({"error": "forbidden"}), 401

        if not _allowed_from(account):
            return jsonify({"error": "forbidden"}), 401

        payload = request.get_json(silent=True) or {}
        subdomain = str(payload.get("subdomain", "")).strip().lower()
        value = str(payload.get("txt", "")).strip()

        # An account may only write its own subdomain.
        if subdomain != account["subdomain"]:
            _LOG.warning(
                "Rejected update: account %s tried to write subdomain %r",
                username,
                subdomain,
            )
            return jsonify({"error": "forbidden"}), 401

        if not TXT_RE.match(value):
            return jsonify({"error": "bad_txt"}), 400

        if not db.update_txt(subdomain, value):
            return jsonify({"error": "bad_txt"}), 400

        _LOG.info("Updated TXT for %s", cfg.fulldomain(subdomain))
        return jsonify({"txt": value}), 200

    if cfg.disable_registration:
        _LOG.info("Registration endpoint disabled by configuration")
    else:

        @app.post("/register")
        def register():
            payload = request.get_json(silent=True) or {}
            allow_from = payload.get("allowfrom") or cfg.allow_from
            account = db.register(allow_from=list(allow_from))
            return (
                jsonify(
                    {
                        "username": account["username"],
                        "password": account["password"],
                        "fulldomain": cfg.fulldomain(account["subdomain"]),
                        "subdomain": account["subdomain"],
                        "allowfrom": account["allowfrom"],
                    }
                ),
                201,
            )

    return app
