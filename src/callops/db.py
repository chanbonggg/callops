"""Small transactional SQL migration runner; no auto-migration at startup."""

import hashlib
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


class DatabaseError(RuntimeError):
    pass


def verify_role(conn, expected):
    row = conn.execute("SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    if not row or row[0] != expected or any(row[1:]):
        raise DatabaseError("Unexpected or overprivileged database role")
    if expected == "callops_app":
        if conn.execute("SELECT pg_has_role(current_user, 'callops_migrator', 'MEMBER') OR has_schema_privilege(current_user, 'callops', 'CREATE')").fetchone()[0]:
            raise DatabaseError("Application role must not have migration privileges")


def migration_files(directory):
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files or len({p.name[:3] for p in files}) != len(files):
        raise DatabaseError("Missing or duplicate migration versions")
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.read_text(encoding="utf-8")) for p in files}


def migrate(settings, directory=MIGRATIONS):
    files = migration_files(directory)
    try:
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            verify_role(conn, "callops_migrator")
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute("SET LOCAL statement_timeout = '30s'")
            conn.execute("SELECT pg_advisory_xact_lock(65001001)")
            conn.execute("CREATE TABLE IF NOT EXISTS callops.schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
            conn.execute("REVOKE ALL ON callops.schema_migrations FROM callops_app")
            conn.execute("GRANT SELECT ON callops.schema_migrations TO callops_app")
            applied = dict(conn.execute("SELECT name, sha256 FROM callops.schema_migrations").fetchall())
            if any(name not in files or files[name][0] != digest for name, digest in applied.items()):
                raise DatabaseError("Migration history differs from this checkout")
            completed = []
            for name, (digest, script) in files.items():
                if name in applied:
                    continue
                conn.execute(script)
                conn.execute("INSERT INTO callops.schema_migrations (name, sha256) VALUES (%s, %s)", (name, digest))
                completed.append(name)
            return completed
    except psycopg.Error:
        raise DatabaseError("Migration failed; transaction rolled back (details withheld)") from None


def readiness(settings):
    try:
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            verify_role(conn, "callops_app")
            actual = dict(conn.execute("SELECT name, sha256 FROM callops.schema_migrations").fetchall())
            expected = {name: digest for name, (digest, _) in migration_files(MIGRATIONS).items()}
            if actual != expected:
                raise DatabaseError("Database migration required before startup")
            conn.execute("SELECT id FROM callops.recordings LIMIT 0")
    except psycopg.Error:
        raise DatabaseError("Database not ready (details withheld)") from None
