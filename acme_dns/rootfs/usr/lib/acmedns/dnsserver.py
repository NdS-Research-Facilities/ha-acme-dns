"""Authoritative DNS listener for the acme-dns zone.

Answers only for its own zone and never recurses. Everything outside the zone
gets REFUSED rather than an attempt to resolve it, so this cannot be used as an
open resolver or a reflection amplifier.
"""

from __future__ import annotations

import logging
import os

from dnslib import EDNS0, NS, QTYPE, RCODE, RR, SOA, A, TXT
from dnslib.server import BaseResolver, DNSLogger, DNSServer

_LOG = logging.getLogger(__name__)

# Advertised EDNS0 UDP payload size. 1232 is the DNS Flag Day 2020 figure,
# chosen to stay under the smallest common path MTU and avoid IP fragmentation.
EDNS_UDP_SIZE = 1232

# Challenge values change between runs, so they must not be cached.
TTL_TXT = 1
# Zone metadata is stable.
TTL_ZONE = 300
# Negative caching window (SOA MINIMUM).
TTL_NEGATIVE = 60

SOA_REFRESH = 86400
SOA_RETRY = 7200
SOA_EXPIRE = 604800


class QuietLogger(DNSLogger):
    """dnslib logs every packet by default; route errors to our logger instead.

    Subclassed rather than configured with a log spec string so a future dnslib
    change to spec parsing cannot turn the listener into a firehose.
    """

    def log_recv(self, handler, data): pass
    def log_send(self, handler, data): pass
    def log_request(self, handler, request): pass
    def log_reply(self, handler, reply): pass
    def log_truncated(self, handler, reply): pass
    def log_data(self, dnsobj): pass

    def log_error(self, handler, err):
        # Malformed packets from the internet are routine, not exceptional.
        _LOG.debug("DNS protocol error: %s", err)


class AcmeDnsResolver(BaseResolver):
    """Serves SOA/NS/A for the zone apex and TXT for account subdomains."""

    def __init__(self, cfg, db) -> None:
        self.cfg = cfg
        self.db = db
        self.zone = cfg.zone.lower()
        self._serial = 1

    # ------------------------------------------------------------- record data

    def _soa_rr(self, rname: str) -> RR:
        return RR(
            rname=rname,
            rtype=QTYPE.SOA,
            ttl=TTL_ZONE,
            rdata=SOA(
                mname=self.zone + ".",
                rname=self.cfg.soa_rname + ".",
                times=(
                    self._serial,
                    SOA_REFRESH,
                    SOA_RETRY,
                    SOA_EXPIRE,
                    TTL_NEGATIVE,
                ),
            ),
        )

    def _apex_answers(self, qtype: int) -> list[RR]:
        answers: list[RR] = []
        want_any = qtype == QTYPE.ANY

        if want_any or qtype == QTYPE.SOA:
            answers.append(self._soa_rr(self.zone + "."))
        if want_any or qtype == QTYPE.NS:
            answers.append(
                RR(self.zone + ".", QTYPE.NS, ttl=TTL_ZONE, rdata=NS(self.zone + "."))
            )
        if (want_any or qtype == QTYPE.A) and self.cfg.public_ip:
            answers.append(
                RR(self.zone + ".", QTYPE.A, ttl=TTL_ZONE, rdata=A(self.cfg.public_ip))
            )
        return answers

    # ------------------------------------------------------------------ resolve

    @staticmethod
    def _echo_edns(request, reply) -> None:
        """Mirror EDNS0 back when the querier offered it.

        dnslib's reply() drops the additional section, so without this a client
        that sent an OPT record concludes we do not support EDNS and falls back
        to a 512-byte UDP limit. Our answers are far smaller than that, so this
        is about being a correct authoritative server rather than fixing a
        truncation bug -- Let's Encrypt's validators all use EDNS.
        """
        if any(rr.rtype == QTYPE.OPT for rr in request.ar):
            reply.add_ar(EDNS0(udp_len=EDNS_UDP_SIZE))

    def resolve(self, request, handler):  # noqa: ARG002 - dnslib interface
        reply = request.reply()
        try:
            reply = self._resolve(request, reply)
        except Exception:  # noqa: BLE001 - a bad packet must never kill the listener
            _LOG.exception("Failed to build a reply; returning SERVFAIL")
            reply = request.reply()
            reply.header.rcode = RCODE.SERVFAIL

        self._echo_edns(request, reply)
        return reply

    def _resolve(self, request, reply):
        qname = str(request.q.qname).rstrip(".").lower()
        qtype = request.q.qtype

        # Out of zone: refuse rather than resolve. This is what keeps the server
        # from being usable as an amplifier.
        if qname != self.zone and not qname.endswith("." + self.zone):
            reply.header.rcode = RCODE.REFUSED
            return reply

        reply.header.aa = 1

        if qname == self.zone:
            answers = self._apex_answers(qtype)
            if answers:
                for rr in answers:
                    reply.add_answer(rr)
            else:
                # Name exists, no data of this type: NOERROR with SOA.
                reply.add_auth(self._soa_rr(self.zone + "."))
            return reply

        label = qname[: -(len(self.zone) + 1)]

        # Only a single label below the apex can be an account subdomain.
        if "." in label:
            reply.header.rcode = RCODE.NXDOMAIN
            reply.add_auth(self._soa_rr(self.zone + "."))
            return reply

        account = self.db.get_by_subdomain(label)
        if account is None:
            reply.header.rcode = RCODE.NXDOMAIN
            reply.add_auth(self._soa_rr(self.zone + "."))
            return reply

        if qtype in (QTYPE.TXT, QTYPE.ANY):
            values = self.db.get_txt_values(label)
            for value in values:
                reply.add_answer(
                    RR(qname + ".", QTYPE.TXT, ttl=TTL_TXT, rdata=TXT(value))
                )
            if not values:
                # Registered but no challenge in flight.
                reply.add_auth(self._soa_rr(self.zone + "."))
            return reply

        # Subdomain exists but holds no record of the requested type.
        reply.add_auth(self._soa_rr(self.zone + "."))
        return reply


def start(cfg, db) -> list[DNSServer]:
    """Start the UDP and TCP listeners, returning them for later shutdown."""
    resolver = AcmeDnsResolver(cfg, db)
    logger = QuietLogger()
    servers = []
    # Always 53 in the app; overridable so the suite can run unprivileged.
    port = int(os.environ.get("ACMEDNS_DNS_PORT", "53"))

    for use_tcp in (False, True):
        server = DNSServer(
            resolver,
            port=port,
            address="0.0.0.0",
            tcp=use_tcp,
            logger=logger,
        )
        server.start_thread()
        servers.append(server)
        _LOG.info("DNS listening on 0.0.0.0:53/%s", "tcp" if use_tcp else "udp")

    return servers
