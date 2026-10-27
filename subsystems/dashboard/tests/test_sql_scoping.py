"""A heuristic scan for SQL that touches a tenant-owned table without naming
tenant_id, the most common way to leak one tenant's rows to another.

Every string literal in the dashboard app and the single-tenant scripts
(adjacent literals, f-string parts and `+` concatenations are joined first)
that reads or writes a tenant-owned table must mention tenant_id. Each JOIN
onto a tenant-owned table must also have tenant_id in its ON clause, since a
join on rustdesk_id or email alone can match another tenant's row.

Statements that are fine without it go in ALLOWED with the reason.
"""
import ast
import re
from pathlib import Path

from app.tenant_migration import TENANT_TABLES

ROOT = Path(__file__).resolve().parents[2]          # subsystems/
SOURCES = sorted([*(ROOT / "dashboard" / "app").rglob("*.py"), *(ROOT / "single-tenant").glob("*.py")])
# The migration copies every row of the pre-tenancy tables by design, and the
# provider console (app/console/) works across tenants on purpose: it lists,
# provisions and purges them. It is only reachable on the console host
# (test_console_separation.py), never through a tenant's request.
SKIP_FILES = {ROOT / "dashboard" / "app" / "tenant_migration.py",
              *(ROOT / "dashboard" / "app" / "console").rglob("*.py")}

TABLES = set(TENANT_TABLES)
_DML = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.I)
_TABLE_REF = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+"?(\w+)"?', re.I)
_JOIN = re.compile(r'\bJOIN\s+"?(\w+)"?(?:\s+(?:AS\s+)?\w+)?\s+ON\b(.*?)(?=\bLEFT\b|\bJOIN\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|\)|$)',
                   re.I | re.S)

# (file name, a fragment of the statement) -> why it needs no tenant_id.
ALLOWED = {
    ("deps.py", "UPDATE client_groups SET policy_ts = auto_update_ts"):
        "pre-tenancy migration step; runs before tenant_id exists",
    ("deps.py", "INSERT INTO users_rebuild"):
        "pre-tenancy migration step (users_rebuild); runs before tenant_id exists",
    ("audit.py", "FROM provisioning_events WHERE ?"):
        "the WHERE is _events_scope(tenant), always a tenant_id filter (test_isolation covers it)",
    ("backup.py", "INSERT INTO provisioning_events (event, detail"):
        "provider event (backups): tenant_id is deliberately NULL",
    ("setup_server.py", "INSERT INTO provisioning_events (event, detail)"):
        "fresh install before the dashboard has migrated the database",
}


def _literal(node) -> str | None:
    """The text of a string expression made of literals, f-strings and +."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else " ? " for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None or right is not None:
            return (left or " ? ") + (right or " ? ")
    return None


def sql_strings(path: Path):
    tree = ast.parse(path.read_text(), str(path))
    parts_of_bigger = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.JoinedStr)):
            for child in ast.walk(node):
                if child is not node:
                    parts_of_bigger.add(id(child))
    for node in ast.walk(tree):
        if id(node) in parts_of_bigger:
            continue
        text = _literal(node)
        if text and _DML.search(text) and _TABLE_REF.search(text):
            yield node.lineno, " ".join(text.split())


def _allowed(path: Path, sql: str) -> bool:
    return any(path.name == f and frag in sql for f, frag in ALLOWED)


def test_scanner_finds_sql():
    found = [(p.name, s) for p in SOURCES for _, s in sql_strings(p)]
    assert len(found) > 150, len(found)
    assert any("FROM device_sessions s" in s for _, s in found)   # a + concatenation


def test_every_statement_on_a_tenant_table_names_tenant_id():
    problems = []
    for path in SOURCES:
        if path in SKIP_FILES:
            continue
        for line, sql in sql_strings(path):
            touched = {t for t in _TABLE_REF.findall(sql) if t in TABLES}
            if touched and "tenant_id" not in sql and not _allowed(path, sql):
                problems.append(f"{path.relative_to(ROOT)}:{line}: {sorted(touched)}: {sql[:160]}")
    assert not problems, "SQL on tenant-owned tables without tenant_id:\n" + "\n".join(problems)


def test_every_join_onto_a_tenant_table_matches_tenant_id():
    problems = []
    for path in SOURCES:
        if path in SKIP_FILES:
            continue
        for line, sql in sql_strings(path):
            for table, on in _JOIN.findall(sql):
                if table in TABLES and "tenant_id" not in on and not _allowed(path, sql):
                    problems.append(f"{path.relative_to(ROOT)}:{line}: JOIN {table} ON {on.strip()[:120]}")
    assert not problems, "JOINs onto tenant-owned tables without tenant_id:\n" + "\n".join(problems)


def test_the_scanner_catches_the_mistakes_it_is_for(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        'a = "SELECT * FROM devices WHERE rustdesk_id = ?"\n'
        'b = ("SELECT d.* FROM devices d "\n     "JOIN client_groups cg ON cg.id = d.group_id WHERE d.tenant_id = ?")\n'
    )
    found = list(sql_strings(bad))
    assert len(found) == 2
    assert "tenant_id" not in found[0][1]
    assert [t for t, on in _JOIN.findall(found[1][1]) if "tenant_id" not in on] == ["client_groups"]
