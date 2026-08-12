"""Entry point: start the DNS listeners, bootstrap accounts, serve the API."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from waitress import serve

from . import __version__, api, bootstrap, config, dnsserver

_LOG = logging.getLogger("acmedns")


def main() -> int:
    try:
        cfg = config.load()
    except config.ConfigError as err:
        # Logging is not configured yet, so write plainly and exit non-zero;
        # the s6 finish script turns that into a visible app failure.
        print(f"[ERROR] Configuration invalid: {err}", file=sys.stderr)
        return 1

    config.configure_logging(cfg.log_level)
    _LOG.info("acme-dns %s starting for zone %s", __version__, cfg.zone)

    db = db_open(config.DB_PATH)
    if db is None:
        return 1

    # DNS comes up before anything else: the zone is already delegated in public
    # DNS, so every second without a listener is a lame delegation.
    try:
        servers = dnsserver.start(cfg, db)
    except OSError as err:
        _LOG.error(
            "Could not bind port 53 (%s). Another service on the host may hold "
            "it, or the app lacks permission.",
            err,
        )
        return 1

    # Registration and CNAME polling can take minutes; keep them off the path
    # that makes the API available.
    threading.Thread(
        target=_bootstrap_guarded,
        args=(cfg, db),
        name="bootstrap",
        daemon=True,
    ).start()

    stopping = threading.Event()

    def handle_signal(signum, _frame):
        _LOG.info("Received signal %s, shutting down", signum)
        stopping.set()
        for server in servers:
            try:
                server.stop()
            except Exception:  # noqa: BLE001 - best effort during shutdown
                _LOG.debug("Error stopping a DNS listener", exc_info=True)
        db.close()
        # waitress has no clean in-process stop; exiting is what s6 expects.
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    app = api.create_app(cfg, db)
    _LOG.info(
        "HTTP API on 0.0.0.0:%d (internal network only) - reachable as %s",
        cfg.api_port,
        cfg.resolved_api_base(),
    )
    serve(app, host="0.0.0.0", port=cfg.api_port, threads=4, ident="acme-dns")
    return 0


def db_open(path: str):
    from .db import Database

    try:
        return Database(path)
    except Exception as err:  # noqa: BLE001 - surface the reason, then fail
        _LOG.error("Could not open the database at %s: %s", path, err)
        return None


def _bootstrap_guarded(cfg, db) -> None:
    try:
        bootstrap.run(cfg, db, config.STORAGE_PATH)
    except Exception:  # noqa: BLE001 - never take the DNS server down with it
        _LOG.exception("Bootstrap failed; the DNS server keeps running")


if __name__ == "__main__":
    sys.exit(main())
