#!/usr/bin/env python3
"""End-to-end smoke test for the acme-dns app.

Exercises the real DNS listener and the real HTTP API over sockets — no mocks —
covering the paths lego actually uses plus the refusal behaviour that keeps the
server from being an open resolver.

Run:  python3 tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "acme_dns",
    "rootfs",
    "usr",
    "lib",
)
sys.path.insert(0, os.path.abspath(ROOT))

DNS_PORT = 15353
API_PORT = 18080
ZONE = "auth.example.test"

os.environ["ACMEDNS_DNS_PORT"] = str(DNS_PORT)

from dnslib import EDNS0, QTYPE, RCODE, DNSRecord  # noqa: E402
from waitress import serve  # noqa: E402

from acmedns import api, dnsserver  # noqa: E402
from acmedns.config import Config  # noqa: E402
from acmedns.db import Database  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def dns_query(name: str, qtype: str = "TXT") -> DNSRecord:
    return DNSRecord.parse(
        DNSRecord.question(name, qtype).send("127.0.0.1", DNS_PORT, timeout=5)
    )


def http_post(path: str, payload: dict, headers: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{API_PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        body = err.read()
        try:
            return err.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return err.code, {}


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="acmedns-test-")
    db = Database(os.path.join(tmpdir, "test.db"))

    cfg = Config(
        zone=ZONE,
        nsadmin="admin@example.test",
        domains=["example.test"],
        public_ip="198.51.100.1",
        api_port=API_PORT,
        disable_registration=False,
        verify_cname=False,
    )

    servers = dnsserver.start(cfg, db)
    app = api.create_app(cfg, db)
    threading.Thread(
        target=lambda: serve(app, host="127.0.0.1", port=API_PORT, threads=2, _quiet=True),
        daemon=True,
    ).start()
    time.sleep(1.5)

    print("\n== zone apex ==")
    soa = dns_query(ZONE, "SOA")
    check("SOA answered", any(rr.rtype == QTYPE.SOA for rr in soa.rr))
    check("AA flag set", soa.header.aa == 1)
    check(
        "SOA RNAME has '@' replaced by '.'",
        "admin.example.test" in str(soa.rr[0].rdata) if soa.rr else False,
        str(soa.rr[0].rdata) if soa.rr else "no answer",
    )

    ns = dns_query(ZONE, "NS")
    check("NS answered", any(rr.rtype == QTYPE.NS for rr in ns.rr))

    a = dns_query(ZONE, "A")
    check(
        "A answered with public_ip",
        any(str(rr.rdata) == "198.51.100.1" for rr in a.rr),
    )

    print("\n== out-of-zone is refused (no amplification) ==")
    refused = dns_query("example.com", "A")
    check(
        "REFUSED for out-of-zone",
        refused.header.rcode == RCODE.REFUSED,
        f"rcode={RCODE[refused.header.rcode]}",
    )
    check("no answers leaked", len(refused.rr) == 0)

    print("\n== unknown in-zone name ==")
    nx = dns_query(f"nosuchaccount.{ZONE}", "TXT")
    check(
        "NXDOMAIN for unknown subdomain",
        nx.header.rcode == RCODE.NXDOMAIN,
        f"rcode={RCODE[nx.header.rcode]}",
    )
    check("SOA in authority for negative caching", len(nx.auth) == 1)

    print("\n== EDNS0 ==")
    edns_query = DNSRecord.question(ZONE, "SOA")
    edns_query.add_ar(EDNS0(udp_len=4096))
    edns_reply = DNSRecord.parse(
        edns_query.send("127.0.0.1", DNS_PORT, timeout=5)
    )
    check(
        "OPT echoed when the querier offers EDNS0",
        any(rr.rtype == QTYPE.OPT for rr in edns_reply.ar),
        f"ar={len(edns_reply.ar)}",
    )
    plain_reply = dns_query(ZONE, "SOA")
    check(
        "no OPT when the querier did not send one",
        not any(rr.rtype == QTYPE.OPT for rr in plain_reply.ar),
    )

    print("\n== registration ==")
    status, account = http_post("/register", {})
    check("POST /register returns 201", status == 201, f"status={status}")
    required = {"username", "password", "fulldomain", "subdomain", "allowfrom"}
    check(
        "response has the goacmedns field names",
        required.issubset(account.keys()),
        f"missing={required - set(account.keys())}",
    )
    check(
        "fulldomain is subdomain.zone",
        account.get("fulldomain") == f"{account.get('subdomain')}.{ZONE}",
        account.get("fulldomain", ""),
    )

    auth = {"X-Api-User": account["username"], "X-Api-Key": account["password"]}
    sub = account["subdomain"]
    v1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    v2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    print("\n== update + DNS visibility ==")
    status, _ = http_post("/update", {"subdomain": sub, "txt": v1}, auth)
    check("POST /update accepted", status == 200, f"status={status}")

    txt = dns_query(f"{sub}.{ZONE}", "TXT")
    values = [str(rr.rdata).strip('"') for rr in txt.rr]
    check("TXT value visible over DNS", v1 in values, f"got={values}")
    check("TXT TTL is 1", all(rr.ttl == 1 for rr in txt.rr))

    print("\n== two concurrent values (wildcard + apex case) ==")
    http_post("/update", {"subdomain": sub, "txt": v2}, auth)
    txt = dns_query(f"{sub}.{ZONE}", "TXT")
    values = [str(rr.rdata).strip('"') for rr in txt.rr]
    check(
        "both values present simultaneously",
        v1 in values and v2 in values,
        f"got={values}",
    )

    print("\n== third update rotates out the oldest ==")
    v3 = "ccccccccccccccccccccccccccccccccccccccccccc"
    http_post("/update", {"subdomain": sub, "txt": v3}, auth)
    txt = dns_query(f"{sub}.{ZONE}", "TXT")
    values = [str(rr.rdata).strip('"') for rr in txt.rr]
    check("exactly two values retained", len(values) == 2, f"got={values}")
    check("newest retained", v3 in values, f"got={values}")

    print("\n== auth enforcement ==")
    status, _ = http_post("/update", {"subdomain": sub, "txt": v1}, {})
    check("no credentials -> 401", status == 401, f"status={status}")

    status, _ = http_post(
        "/update",
        {"subdomain": sub, "txt": v1},
        {"X-Api-User": account["username"], "X-Api-Key": "wrong"},
    )
    check("wrong password -> 401", status == 401, f"status={status}")

    other = db.register()
    status, _ = http_post(
        "/update",
        {"subdomain": other["subdomain"], "txt": v1},
        auth,
    )
    check(
        "cannot write another account's subdomain -> 401",
        status == 401,
        f"status={status}",
    )

    status, _ = http_post("/update", {"subdomain": sub, "txt": "!!!bad!!!"}, auth)
    check("malformed TXT rejected -> 400", status == 400, f"status={status}")

    print("\n== storage file ==")
    from acmedns import bootstrap

    storage = os.path.join(tmpdir, "accounts.json")
    bootstrap.run(cfg, db, storage)
    with open(storage, encoding="utf-8") as handle:
        stored = json.load(handle)
    check("base domain key present", "example.test" in stored)
    check("wildcard key present", "*.example.test" in stored)
    check(
        "both keys share one account",
        stored.get("example.test") == stored.get("*.example.test"),
    )
    check(
        "entry has goacmedns fields",
        {"fulldomain", "subdomain", "username", "password", "server_url"}.issubset(
            stored.get("example.test", {}).keys()
        ),
    )
    check("file mode is 0600", oct(os.stat(storage).st_mode)[-3:] == "600",
          oct(os.stat(storage).st_mode)[-3:])

    print("\n== idempotent re-bootstrap ==")
    before = dict(stored)
    bootstrap.run(cfg, db, storage)
    with open(storage, encoding="utf-8") as handle:
        after = json.load(handle)
    check("re-running does not rotate credentials", before == after)

    for server in servers:
        server.stop()
    db.close()

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
