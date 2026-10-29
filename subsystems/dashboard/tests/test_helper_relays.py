"""rdf-helper's relay commands (subsystems/provisioning/rdf_helper.py): the
compose files it renders, the values it refuses, and that the dashboard has
no Docker access of its own."""
import ast
import json
import os
import sqlite3
from pathlib import Path

import pytest

from conftest import HELPER_MODULE as H

FIXTURE = Path(__file__).parent / "fixtures" / "tenant1-docker-compose.yml"
REPO = Path(__file__).resolve().parents[3]


class Run:
    def __init__(self, out=""):
        self.calls, self.out = [], out

    def __call__(self, args, timeout=300, stdin=None):
        self.calls.append((args, stdin))
        return 0, self.out


def _setup(tmp_path, relays=(), tenants=()):
    data = tmp_path / "fleet"
    data.mkdir()
    conn = sqlite3.connect(data / "fleet.sqlite3")
    conn.executescript("""CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT, status TEXT);
        CREATE TABLE relays (tenant_id INTEGER PRIMARY KEY, host TEXT, hbbs_port INTEGER, hbbr_port INTEGER,
                             data_dir TEXT, compose_path TEXT);""")
    conn.execute("INSERT INTO tenants VALUES (1, 'pacific', 'active')")
    conn.execute("INSERT INTO relays VALUES (1, 'rds.example.com', 21116, 21117, ?, ?)",
                 (str(data / "data"), str(data / "docker-compose.yml")))
    conn.execute("INSERT INTO tenants VALUES (2, 'acme', 'active')")
    d2 = data / "tenants" / "2" / "relay"
    conn.execute("INSERT INTO relays VALUES (2, 'acme.relay.rds.example.com', 21201, 21202, ?, ?)",
                 (str(d2 / "data"), str(d2 / "docker-compose.yml")))
    for row in tenants:
        conn.execute("INSERT OR REPLACE INTO tenants VALUES (?, ?, ?)", row)
    for row in relays:
        conn.execute("INSERT OR REPLACE INTO relays VALUES (?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()
    (data / "data").mkdir()
    d2.mkdir(parents=True)
    (d2 / "data").mkdir()
    run = Run()
    return H.Config("rds.example.com", data, run=run, sleep=lambda s: None), run


def test_tenant_one_renders_exactly_todays_file(tmp_path):
    cfg, _ = _setup(tmp_path)
    assert H.load_relay(cfg, "1").render("1.1.16") == FIXTURE.read_text()
    # ...and the repo's template, at the version it pins, is the same file.
    src = (REPO / "subsystems" / "single-tenant" / "docker-compose.yml").read_text()
    version = H.IMAGE_TAG_RE.search(src).group(1)
    assert H.load_relay(cfg, "1").render(version) == src


def test_writing_tenant_ones_file_again_changes_nothing_and_runs_it_as_is(tmp_path):
    cfg, run = _setup(tmp_path)
    H.relay_write_compose(cfg, "1", "1.1.16")
    assert (cfg.data_dir / "docker-compose.yml").read_text() == FIXTURE.read_text()
    H.relay_restart(cfg, "1")
    args, stdin = run.calls[-1]
    assert args == ["docker", "compose", "-p", "rustdesk-fleet", "--project-directory", str(cfg.data_dir),
                    "-f", "-", "restart"]
    assert stdin == FIXTURE.read_text()


def test_a_new_tenants_compose_mounts_only_its_own_data_directory(tmp_path):
    cfg, run = _setup(tmp_path)
    H.relay_write_compose(cfg, "2", "1.1.16")
    path = cfg.data_dir / "tenants" / "2" / "relay" / "docker-compose.yml"
    text = path.read_text()
    assert "name: rdf-tenant-2" in text and "command: hbbs -p 21201 -r acme.relay.rds.example.com:21202" in text
    assert "command: hbbr -p 21202" in text and text.count("image: rustdesk/rustdesk-server:1.1.16") == 2
    assert [l.strip() for l in text.splitlines() if l.strip().startswith("- ")] == ["- ./data:/root", "- hbbr",
                                                                                   "- ./data:/root"]
    assert "privileged" not in text and "/:" not in text.replace("./data:/root", "")
    H.relay_up(cfg, "2")
    args, stdin = run.calls[-1]
    assert args[:6] == ["docker", "compose", "-p", "rdf-tenant-2", "--project-directory", str(path.parent)]
    assert args[6:] == ["-f", "-", "up", "-d"] and stdin == text


def test_a_changed_compose_file_is_refused(tmp_path):
    cfg, run = _setup(tmp_path)
    H.relay_write_compose(cfg, "2", "1.1.16")
    path = cfg.data_dir / "tenants" / "2" / "relay" / "docker-compose.yml"
    path.write_text(path.read_text().replace("./data:/root", "/:/root"))
    with pytest.raises(H.HelperError, match="isn't the helper's rendering"):
        H.relay_up(cfg, "2")
    path.unlink()
    path.symlink_to(cfg.data_dir / "docker-compose.yml")
    with pytest.raises(H.HelperError):
        H.relay_up(cfg, "2")
    assert run.calls == []


def test_a_symlinked_data_directory_is_refused(tmp_path):
    cfg, run = _setup(tmp_path)
    H.relay_write_compose(cfg, "2", "1.1.16")
    data = cfg.data_dir / "tenants" / "2" / "relay" / "data"
    data.rmdir()
    data.symlink_to("/")
    with pytest.raises(H.HelperError, match="plain directory"):
        H.relay_up(cfg, "2")
    assert run.calls == []


@pytest.mark.parametrize("tid", ["0", "-1", "abc", "2; rm -rf /", "1e3", "", "99"])
def test_bad_tenant_ids(tmp_path, tid):
    cfg, run = _setup(tmp_path)
    with pytest.raises(H.HelperError):
        H.relay_write_compose(cfg, tid, "1.1.16")
    assert H.main(["relay-up", tid], cfg) == 1


@pytest.mark.parametrize("version", ["latest", "1.1", "1.1.16; reboot", "1.1.16-beta", "../1.1.16", " 1.1.16", ""])
def test_bad_versions(tmp_path, version):
    cfg, _ = _setup(tmp_path)
    with pytest.raises(H.HelperError, match="invalid version"):
        H.relay_write_compose(cfg, "2", version)


@pytest.mark.parametrize("row,why", [
    ((2, "evil.example.net", 21201, 21202, "{d2}/data", ""), "relay host"),
    ((2, "other.relay.rds.example.com", 21201, 21202, "{d2}/data", ""), "relay host"),
    ((2, "acme.relay.rds.example.com", 21116, 21117, "{d2}/data", ""), "outside"),
    ((2, "acme.relay.rds.example.com", 22001, 22002, "{d2}/data", ""), "outside"),
    ((2, "acme.relay.rds.example.com", 21201, 21299, "{d2}/data", ""), "outside"),
    ((2, "acme.relay.rds.example.com", 21201, 21202, "/", ""), "data directory"),
    ((2, "acme.relay.rds.example.com", 21201, 21202, "{d1}", ""), "data directory"),
    ((1, "rds.example.com", 21201, 21202, "{d1}", ""), "21116/21117"),
    ((1, "rds.example.com", 21116, 21117, "/", ""), "data directory"),
    ((1, "bad host!", 21116, 21117, "{d1}", ""), "invalid"),
])
def test_out_of_range_relay_rows_are_refused(tmp_path, row, why):
    data = tmp_path / "fleet"
    d1, d2 = data / "data", data / "tenants" / "2" / "relay"
    row = tuple(v.format(d1=d1, d2=d2) if isinstance(v, str) else v for v in row)
    cfg, run = _setup(tmp_path, relays=[row])
    with pytest.raises(H.HelperError, match=why):
        H.relay_write_compose(cfg, str(row[0]), "1.1.16")
    assert run.calls == []


def test_tenant_one_is_never_taken_down(tmp_path):
    cfg, run = _setup(tmp_path)
    H.relay_write_compose(cfg, "1", "1.1.16")
    with pytest.raises(H.HelperError, match="never taken down"):
        H.relay_down(cfg, "1")
    assert run.calls == []


def test_tenant_one_works_before_the_first_migration(tmp_path):
    data = tmp_path / "fleet"
    data.mkdir()
    (data / "data").mkdir()
    conn = sqlite3.connect(data / "fleet.sqlite3")
    conn.execute("CREATE TABLE server_config (id INTEGER, host TEXT, hbbs_port INTEGER, hbbr_port INTEGER, data_dir TEXT)")
    conn.execute("INSERT INTO server_config VALUES (1, 'rds.example.com', 21116, 21117, ?)", (str(data / "data"),))
    conn.commit()
    conn.close()
    cfg = H.Config("", data, run=Run())
    H.relay_write_compose(cfg, "1", "1.1.15")
    assert (data / "docker-compose.yml").read_text() == H.LEGACY_COMPOSE.format(version="1.1.15")
    with pytest.raises(H.HelperError):
        H.relay_write_compose(cfg, "2", "1.1.15")


def test_status_is_json_of_valid_container_names(tmp_path):
    cfg, _ = _setup(tmp_path)
    cfg.run = Run("hbbs\tUp 3 weeks\nrdf-t2-hbbr\tUp 1 minute\nbad name;\tUp\n")
    assert json.loads(H.relay_status(cfg)) == {"hbbs": "Up 3 weeks", "rdf-t2-hbbr": "Up 1 minute"}


# ── The dashboard has no Docker access of its own ────────────────────────────

def _subprocess_args_with_docker(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and node.elts[0].value == "docker":
            yield node.lineno


def test_no_docker_commands_in_the_dashboard():
    files = list((REPO / "subsystems" / "dashboard" / "app").rglob("*.py"))
    files += [REPO / "subsystems" / "single-tenant" / n for n in ("setup_server.py", "generate_installer.py",
                                                                    "sign_installer.py")]
    found = [f"{f.relative_to(REPO)}:{line}" for f in files for line in _subprocess_args_with_docker(f)]
    assert not found, "Docker commands must go through rdf-helper: " + repr(found)
