"""Custom-domain DNS rules (rdf_helper.check_domain, used by the dashboard
through app/domain_dns.py), with DNS faked per resolver."""
import pytest

from app import domain_dns

PROVIDER = "rds.example.com"
TARGET = "abc.rds.example.com"           # the tenant's own subdomain
DOMAIN = "rds.abc.example"
BOX = "198.51.100.10"
BOX6 = "2001:db8::10"
TOKEN = "rdf-verify-" + "t" * 32
CLOUDFLARE = "104.16.1.1"


class Zone:
    """records[name] = [(type, data)] answered by every resolver unless
    per_resolver overrides it; status per name (default NOERROR)."""

    def __init__(self, records=None, per_resolver=None, status=None):
        self.records = records or {}
        self.per_resolver = per_resolver or {}
        self.status = status or {}

    def __call__(self, resolver, name, rtype):
        recs = self.per_resolver.get(resolver, {}).get(name, self.records.get(name, []))
        status = self.status.get((resolver, name), self.status.get(name, "NOERROR"))
        out, seen = [], name
        # Follow CNAMEs the way a recursive resolver's answer section does.
        for _ in range(10):
            here = self.per_resolver.get(resolver, {}).get(seen, self.records.get(seen, []))
            cname = [d for t, d in here if t == "CNAME"]
            if cname and rtype != "CNAME":
                out.append((seen, "CNAME", cname[0] + "."))
                seen = cname[0]
                continue
            out += [(seen, t, d) for t, d in here if t == rtype]
            break
        del recs
        return status, out


def check(zone, ipv4=BOX, ipv6="", token=TOKEN, resolvers=("1.1.1.1", "8.8.8.8")):
    h = domain_dns.helper()
    return h.check_domain(zone, DOMAIN, TARGET, PROVIDER, token, ipv4, ipv6, resolvers)


def ours():
    return {TARGET: [("A", BOX)]}


def test_cname_to_own_subdomain_passes():
    r = check(Zone({DOMAIN: [("CNAME", TARGET)], **ours()}))
    assert r.ok and r.method == "cname"


def test_cname_chain_through_another_name_reaching_own_subdomain_passes():
    r = check(Zone({DOMAIN: [("CNAME", "alias.abc.example")], "alias.abc.example": [("CNAME", TARGET)], **ours()}))
    assert r.ok and r.method == "cname"


@pytest.mark.parametrize("other", ["globex.rds.example.com", "admin.rds.example.com", "rds.example.com"])
def test_cname_to_another_tenant_or_the_console_is_a_different_account(other):
    r = check(Zone({DOMAIN: [("CNAME", other)], other: [("A", BOX)]}))
    assert not r.ok and r.code == "other_account"
    assert "points at a different account" in r.reason
    assert "globex" not in r.reason            # never names the other tenant


def test_cname_somewhere_else():
    r = check(Zone({DOMAIN: [("CNAME", "web.hosting.example")], "web.hosting.example": [("A", "203.0.113.5")]}))
    assert not r.ok and r.code == "cname_elsewhere"
    assert "web.hosting.example" in r.reason and TARGET in r.reason


def test_a_plus_txt_passes():
    r = check(Zone({DOMAIN: [("A", BOX)], f"_rdf-verify.{DOMAIN}": [("TXT", f'"{TOKEN}"')]}))
    assert r.ok and r.method == "txt"


def test_a_plus_aaaa_plus_txt_passes_with_ipv6_configured():
    zone = Zone({DOMAIN: [("A", BOX), ("AAAA", BOX6)], f"_rdf-verify.{DOMAIN}": [("TXT", f'"{TOKEN}"')]})
    assert check(zone, ipv6=BOX6).ok
    r = check(zone)                               # the box has no IPv6: that AAAA isn't ours
    assert not r.ok and r.code == "aaaa_mismatch"


def test_txt_split_into_strings_is_joined():
    half = len(TOKEN) // 2
    r = check(Zone({DOMAIN: [("A", BOX)], f"_rdf-verify.{DOMAIN}": [("TXT", f'"{TOKEN[:half]}" "{TOKEN[half:]}"')]}))
    assert r.ok


def test_a_without_txt_fails():
    r = check(Zone({DOMAIN: [("A", BOX)]}))
    assert not r.ok and r.code == "txt_missing"


def test_wrong_txt_fails():
    r = check(Zone({DOMAIN: [("A", BOX)], f"_rdf-verify.{DOMAIN}": [("TXT", '"rdf-verify-someone-elses-token"')]}))
    assert not r.ok and r.code == "txt_wrong"


def test_a_records_not_ours_fail():
    r = check(Zone({DOMAIN: [("A", "203.0.113.9")], f"_rdf-verify.{DOMAIN}": [("TXT", f'"{TOKEN}"')]}))
    assert not r.ok and r.code == "a_mismatch" and BOX in r.reason
    # Ours plus one more is not "exactly ours".
    r = check(Zone({DOMAIN: [("A", BOX), ("A", "203.0.113.9")], f"_rdf-verify.{DOMAIN}": [("TXT", f'"{TOKEN}"')]}))
    assert not r.ok and r.code == "a_mismatch"


def test_a_method_needs_public_ipv4_configured():
    r = check(Zone({DOMAIN: [("A", BOX)], f"_rdf-verify.{DOMAIN}": [("TXT", f'"{TOKEN}"')]}), ipv4="")
    assert not r.ok and r.code == "a_mismatch" and TARGET in r.reason


def test_cdn_proxied_a_records_get_the_cdn_message():
    r = check(Zone({DOMAIN: [("A", CLOUDFLARE)]}))
    assert not r.ok and r.code == "cdn" and "DNS only" in r.reason
    # A CNAME flattened or proxied to a CDN edge, too.
    r = check(Zone({DOMAIN: [("CNAME", "abc.cdn.example")], "abc.cdn.example": [("A", CLOUDFLARE)]}))
    assert not r.ok and r.code == "cdn"


def test_no_record():
    r = check(Zone(status={DOMAIN: "NXDOMAIN"}))
    assert not r.ok and r.code == "no_record"


def test_resolvers_disagreeing_fails():
    zone = Zone({DOMAIN: [("CNAME", TARGET)], **ours()},
                per_resolver={"8.8.8.8": {DOMAIN: [("A", "203.0.113.9")]}})
    r = check(zone)
    assert not r.ok and r.code == "disagree" and "8.8.8.8" in r.reason


def test_lookup_failure_is_reported():
    r = check(Zone({DOMAIN: [("CNAME", TARGET)], **ours()}, status={DOMAIN: "SERVFAIL"}))
    assert not r.ok and r.code == "lookup_failed"


def test_caa_on_the_parent_blocking_lets_encrypt_fails():
    zone = Zone({DOMAIN: [("CNAME", TARGET)], **ours(), "abc.example": [("CAA", '0 issue "digicert.com"')]})
    r = check(zone)
    assert not r.ok and r.code == "caa" and "letsencrypt.org" in r.reason


def test_caa_on_the_domain_itself_blocking_lets_encrypt_fails():
    # A name with a CNAME can't have CAA records of its own; with A + TXT it can.
    zone = Zone({DOMAIN: [("A", BOX), ("CAA", '0 issue ";"')], f"_rdf-verify.{DOMAIN}": [("TXT", f'"{TOKEN}"')]})
    r = check(zone)
    assert not r.ok and r.code == "caa"


def test_caa_allowing_lets_encrypt_passes():
    zone = Zone({DOMAIN: [("CNAME", TARGET)], **ours(),
                 "abc.example": [("CAA", '0 issue "digicert.com"'), ("CAA", '0 issue "letsencrypt.org"')]})
    assert check(zone).ok
    zone = Zone({DOMAIN: [("CNAME", TARGET)], **ours(), "abc.example": [("CAA", '0 iodef "mailto:x@abc.example"')]})
    assert check(zone).ok


def test_dashboard_uses_the_helpers_rules(monkeypatch):
    monkeypatch.setenv("PUBLIC_IPV4", BOX)
    monkeypatch.setattr(domain_dns, "QUERY", Zone({DOMAIN: [("CNAME", TARGET)], **ours()}))
    assert domain_dns.check(DOMAIN, "abc", TOKEN).ok
    assert domain_dns.helper().check_domain.__module__ == "rdf_helper_rules"


def test_resolvers_setting(monkeypatch):
    from app import settings
    assert settings.dns_check_resolvers() == ("1.1.1.1", "8.8.8.8")
    monkeypatch.setenv("DNS_CHECK_RESOLVERS", "9.9.9.9, 2620:fe::fe")
    assert settings.dns_check_resolvers() == ("9.9.9.9", "2620:fe::fe")
    monkeypatch.setenv("DNS_CHECK_RESOLVERS", "9.9.9.9;rm -rf /")
    with pytest.raises(RuntimeError):
        settings.dns_check_resolvers()


def test_dig_command_line_is_fixed():
    calls = []
    h = domain_dns.helper()

    def run(args, timeout):
        calls.append(args)
        return 0, ";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\nrds.abc.example. 300 IN A 198.51.100.10\n"
    assert h.dig_query(run, "1.1.1.1", DOMAIN, "A") == ("NOERROR", [(DOMAIN, "A", BOX)])
    assert calls[0] == ["dig", "@1.1.1.1", "+noall", "+comments", "+answer", "+time=3", "+tries=2", DOMAIN, "A"]
    for bad in ("-f/etc/passwd", "rds.abc.example;id", "_other.rds.abc.example"):
        with pytest.raises(h.HelperError):
            h.dig_query(run, "1.1.1.1", bad, "A")
