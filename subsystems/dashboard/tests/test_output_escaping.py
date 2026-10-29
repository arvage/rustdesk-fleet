"""Every place that puts markup into a page without autoescaping is listed,
with why it's safe, in README.md's "Output escaping" section."""
import re
from collections import Counter
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
APP = DASH / "app"
SINK = re.compile(r"([A-Za-z_$][\w.\[\]()'\-]*\.(?:innerHTML|outerHTML))\s*\+?=(?!=)"
                  r"|([\w.]+\.insertAdjacentHTML\()|(\|\s*safe\b)|(Markup\()")


def _allowlist() -> Counter:
    text = (DASH / "README.md").read_text()
    block = text.split("<!-- escaping-allowlist:start -->")[1].split("<!-- escaping-allowlist:end -->")[0]
    out = Counter()
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) == 4 and cells[0].startswith("`"):
            target = cells[1].strip("`").replace("\\|", "|")
            out[(cells[0].strip("`"), target)] += int(cells[2])
    return out


def _found() -> Counter:
    out = Counter()
    files = list(APP.rglob("*.html")) + list(APP.rglob("*.js")) + list(APP.rglob("*.py"))
    for f in files:
        rel = str(f.relative_to(APP))
        for m in SINK.finditer(f.read_text()):
            target = next(g for g in m.groups() if g)
            if target.startswith("|"):
                target = "| safe"
            out[(rel, target)] += 1
    return out


def test_every_unescaped_sink_is_listed():
    found, allowed = _found(), _allowlist()
    new = {k: n for k, n in found.items() if n > allowed.get(k, 0)}
    assert not new, ("New unescaped output: escape it, or add it with a reason to README.md's "
                     "Output escaping table: " + repr(new))


def test_the_list_has_no_stale_entries():
    found, allowed = _found(), _allowlist()
    stale = {k: n for k, n in allowed.items() if found.get(k, 0) < n}
    assert not stale, "Remove or correct these Output escaping entries: " + repr(stale)


def test_users_page_json_cant_end_its_script_element(two_tenants):
    from conftest import HOST, signed_in_at
    from app.deps import get_db
    conn = get_db()
    conn.execute("UPDATE client_groups SET display_name = '</script><script>alert(1)</script>' WHERE tenant_id = 1")
    conn.commit()
    conn.close()
    html = signed_in_at(two_tenants["a"]["admin"], HOST).get("/users").text
    assert "</script><script>alert(1)" not in html
    assert "\\u003c/script\\u003e" in html
