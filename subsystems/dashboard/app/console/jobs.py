"""Background jobs for the provider console: tenant creation, relay start /
stop / upgrade, purges. They can take minutes (docker pulls, certificates),
so they never run inside a request: a request queues a job and returns, and
one worker thread runs jobs one at a time, recording each step's progress in
provider_jobs, which the console polls.

A job interrupted by a restart is never left half-done: on startup its
handler's `recover` runs (tenant creation undoes what it had done) and the
job is marked failed. Retrying is always a new job.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from contextlib import contextmanager
from typing import Callable

from app import paths
from app.deps import get_db

JOBS_SQL = """
    CREATE TABLE IF NOT EXISTS provider_jobs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL,
        tenant_id    INTEGER,
        params       TEXT NOT NULL DEFAULT '{}',
        status       TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued','running','succeeded','failed')),
        steps        TEXT NOT NULL DEFAULT '[]',
        error        TEXT,
        created_by   TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        started_at   TEXT,
        finished_at  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_provider_jobs_status ON provider_jobs(status, id);
"""


class JobFailed(Exception):
    """A step failed in an expected way; the message is shown as is."""


class Job:
    """What a handler gets: its row's fields, and step() to report progress."""

    def __init__(self, row):
        self.id = row["id"]
        self.kind = row["kind"]
        self.tenant_id = row["tenant_id"]
        self.params = json.loads(row["params"] or "{}")
        self.created_by = row["created_by"] or ""
        self.steps: list[dict] = json.loads(row["steps"] or "[]")

    def _save(self) -> None:
        conn = get_db()
        conn.execute("UPDATE provider_jobs SET steps = ?, tenant_id = ? WHERE id = ?",
                     (json.dumps(self.steps), self.tenant_id, self.id))
        conn.commit()
        conn.close()

    @contextmanager
    def step(self, name: str):
        """Run one named step: shown as running, then done or failed."""
        entry = {"name": name, "status": "running", "detail": "", "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.steps.append(entry)
        self._save()
        try:
            yield entry
        except BaseException as e:
            entry["status"] = "failed"
            entry["detail"] = str(e)[:500]
            self._save()
            raise
        entry["status"] = "done"
        self._save()

    def note(self, name: str, status: str = "done", detail: str = "") -> None:
        self.steps.append({"name": name, "status": status, "detail": detail[:500],
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        self._save()


# kind -> (run(job), recover(job) or None)
HANDLERS: dict[str, tuple[Callable[[Job], None], Callable[[Job], None] | None]] = {}


def handler(kind: str, recover: Callable[[Job], None] | None = None):
    def deco(fn):
        HANDLERS[kind] = (fn, recover)
        return fn
    return deco


def ensure_table(conn) -> None:
    conn.executescript(JOBS_SQL)


def enqueue(kind: str, tenant_id: int | None, params: dict, created_by: str) -> int:
    conn = get_db()
    cur = conn.execute("INSERT INTO provider_jobs (kind, tenant_id, params, created_by) VALUES (?, ?, ?, ?)",
                       (kind, tenant_id, json.dumps(params), created_by))
    conn.commit()
    conn.close()
    _wake.set()
    return cur.lastrowid


def get(job_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM provider_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return row


def active_for(tenant_id: int) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM provider_jobs WHERE tenant_id = ? AND status IN ('queued','running')",
                        (tenant_id,)).fetchall()
    conn.close()
    return rows


def _finish(job_id: int, status: str, error: str = "") -> None:
    conn = get_db()
    conn.execute("UPDATE provider_jobs SET status = ?, error = ?, finished_at = datetime('now') WHERE id = ?",
                 (status, error[:2000] or None, job_id))
    conn.commit()
    conn.close()


def run_one(row) -> None:
    conn = get_db()
    conn.execute("UPDATE provider_jobs SET status = 'running', started_at = datetime('now') WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    job = Job(get(row["id"]))
    run, _recover = HANDLERS.get(job.kind, (None, None))
    if run is None:
        _finish(job.id, "failed", f"Unknown job kind {job.kind!r}.")
        return
    try:
        run(job)
    except JobFailed as e:
        _finish(job.id, "failed", str(e))
    except Exception as e:
        _finish(job.id, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
    else:
        _finish(job.id, "succeeded")


def run_pending() -> int:
    """Run every queued job now, in order (the worker does this; tests call it)."""
    n = 0
    while True:
        conn = get_db()
        row = conn.execute("SELECT * FROM provider_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        conn.close()
        if row is None:
            return n
        run_one(row)
        n += 1


def recover_interrupted() -> None:
    """Jobs left 'running' by a restart: undo what they can, mark them failed."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM provider_jobs WHERE status = 'running'").fetchall()
    conn.close()
    for row in rows:
        job = Job(row)
        _run, recover = HANDLERS.get(job.kind, (None, None))
        error = "Interrupted by a dashboard restart."
        if recover:
            try:
                job.note("Undo after restart", "running")
                recover(job)
                job.steps[-1]["status"] = "done"
                job._save()
                error += " What it had done was undone; it is safe to try again."
            except Exception as e:
                job.steps[-1].update(status="failed", detail=str(e)[:500])
                job._save()
                error += f" Undoing it failed too: {e}"
        _finish(job.id, "failed", error)


_wake = threading.Event()
_started = False


def _worker() -> None:
    try:
        recover_interrupted()
    except Exception:
        pass
    while True:
        try:
            run_pending()
        except Exception:
            pass
        _wake.wait(30)
        _wake.clear()


def start_worker() -> None:
    global _started
    if _started or not paths.WATCHERS_ENABLED:
        return
    _started = True
    threading.Thread(target=_worker, daemon=True, name="provider-jobs").start()
