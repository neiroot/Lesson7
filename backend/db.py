import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from backend.config import ROOT, prepare_data


class AppError(Exception):
    def __init__(self, code, message, status=422, *, stage=None, details=None):
        super().__init__(message)
        self.status = status
        self.payload = {"code": code, "message": message}
        if stage:
            self.payload["stage"] = stage
        if details is not None:
            self.payload["details"] = details


def uid():
    return uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def unpack(value, default=None):
    return json.loads(value) if value is not None else default


@contextmanager
def connect(config, *, write=False):
    db = sqlite3.connect(config.db, timeout=8, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def migrate(config):
    prepare_data(config)
    with connect(config, write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        for path in sorted((ROOT / "backend/migrations").glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
                continue
            # These migrations contain simple SQL statements only. Keep DDL inside this transaction.
            for statement in path.read_text().split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    config.db.chmod(0o600)


def require_row(db, sql, args, message="Запись не найдена"):
    row = db.execute(sql, args).fetchone()
    if row is None:
        raise AppError("not_found", message, 404)
    return row


def add_job(db, kind, *, meeting=None, speaker=None, draft=None, profile=None, version=None, params=None):
    job_id, stamp = uid(), now()
    db.execute(
        """INSERT INTO jobs(id,kind,state,meeting_id,speaker_id,draft_id,target_profile_id,expected_version,
           created_at,updated_at,params) VALUES (?,?,'queued',?,?,?,?,?,?,?,?)""",
        (job_id, kind, meeting, speaker, draft, profile, version, stamp, stamp, dump(params or {})),
    )
    return job_id


def enqueue_summary(db, meeting, version):
    job = db.execute(
        """SELECT id FROM jobs WHERE kind='summarize' AND meeting_id=? AND expected_version=?
           AND state IN ('queued','running')""", (meeting, version),
    ).fetchone()
    if job:
        return job["id"]
    db.execute(
        "UPDATE meetings SET summary_status='queued', summary_error=NULL, updated_at=? WHERE id=?",
        (now(), meeting),
    )
    return add_job(db, "summarize", meeting=meeting, version=version)


def defer_unlink(db, path):
    if path:
        db.execute("INSERT OR IGNORE INTO pending_deletions VALUES (?)", (path,))
