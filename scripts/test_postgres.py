"""Run P1 against a fresh local PostgreSQL, never an existing application DB."""

import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def provision(admin_url, phase):
    url = urlsplit(admin_url)
    if (url.scheme != "postgresql" or url.hostname != "127.0.0.1"
            or url.username != "callops_test_admin" or url.path != "/callops_test"
            or not url.port or not url.password or url.query != "sslmode=disable"):
        raise RuntimeError("Only explicit local callops_test admin credentials are accepted")
    password = secrets.token_hex(24)
    with psycopg.connect(admin_url, connect_timeout=5) as conn:
        if conn.execute("SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = 'callops') OR EXISTS (SELECT FROM pg_roles WHERE rolname IN ('callops_app', 'callops_migrator'))").fetchone()[0]:
            raise RuntimeError("Test database/roles already provisioned; refusing to reuse or reset")
        if conn.execute("SELECT EXISTS (SELECT FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema'))").fetchone()[0]:
            raise RuntimeError("Test database must be empty")
        conn.execute((ROOT / "deploy/server/bootstrap.sql").read_text(encoding="utf-8"))
        for role in ("callops_app", "callops_migrator"):
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    env = os.environ.copy()
    for key, role in (("CALLOPS_TEST_DATABASE_URL", "callops_app"), ("CALLOPS_TEST_MIGRATION_DATABASE_URL", "callops_migrator")):
        env[key] = f"postgresql://{role}:{password}@127.0.0.1:{url.port}/callops_test?sslmode=disable"
    return subprocess.run([sys.executable, str(ROOT / "scripts/check.py"), "--phase", phase], cwd=ROOT, env=env).returncode


def checked(command):
    # Windows postgres descendants can inherit pipes and keep communicate() open.
    # A file lets us wait for pg_ctl itself without waiting for the server to exit.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=60)
        if result.returncode:
            if command[-1] == "start":
                output.seek(0)
                print(output.read().decode(errors="replace")[-1500:], file=sys.stderr)
            raise RuntimeError(f"PostgreSQL tool failed: {Path(command[0]).stem}; output withheld")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['P1', 'P2', 'P3'], default='P1')
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--pg-bin", type=Path, help="initdb/pg_ctl directory for a disposable native cluster")
    options.add_argument("--service", action="store_true", help="fresh CI service via CALLOPS_TEST_ADMIN_URL")
    args = parser.parse_args()
    if args.service:
        return provision(os.environ.get("CALLOPS_TEST_ADMIN_URL", ""), args.phase)
    suffix = ".exe" if os.name == "nt" else ""
    initdb = args.pg_bin / f"initdb{suffix}"
    pg_ctl = args.pg_bin / f"pg_ctl{suffix}"
    if not initdb.is_file() or not pg_ctl.is_file():
        raise RuntimeError("PostgreSQL initdb and pg_ctl are required")
    base = (ROOT / "tmp").resolve()
    base.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="callops-p1-", dir=base)).resolve()
    # Every directory used for recursive cleanup is generated and contained here.
    if work.parent != base or not work.name.startswith("callops-p1-"):
        raise RuntimeError("Unexpected temporary path")
    attempted_start = False
    stopped = True
    cluster = work / "cluster"
    try:
        password = secrets.token_hex(24)
        pwfile = work / "password"
        pwfile.write_text(password, encoding="utf-8")
        checked([str(initdb), "-D", str(cluster), "-U", "callops_test_admin", "-A", "scram-sha-256", "--pwfile", str(pwfile), "--encoding=UTF8", "--no-locale"])
        pwfile.unlink()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        attempted_start = True
        stopped = False
        try:
            options = f"-h 127.0.0.1 -p {port}"
            if os.name != "nt":
                options += " -c unix_socket_directories=''"
            checked([str(pg_ctl), "-D", str(cluster), "-l", str(work / "server.log"), "-o", options, "-w", "start"])
        except RuntimeError:
            # This cluster has no data or SQL yet; only startup diagnostics exist.
            log = work / "server.log"
            if log.exists():
                print(log.read_text(encoding="utf-8", errors="replace")[-3000:], file=sys.stderr)
            raise
        admin_base = f"postgresql://callops_test_admin:{password}@127.0.0.1:{port}"
        with psycopg.connect(admin_base + "/postgres?sslmode=disable", autocommit=True, connect_timeout=5) as conn:
            actual = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
            if actual != cluster.resolve():
                raise RuntimeError("PostgreSQL cluster identity mismatch")
            conn.execute("CREATE DATABASE callops_test")
        return provision(admin_base + "/callops_test?sslmode=disable", args.phase)
    finally:
        if attempted_start:
            status = subprocess.run([str(pg_ctl), "-D", str(cluster), "status"], capture_output=True, timeout=15)
            if status.returncode == 0:
                checked([str(pg_ctl), "-D", str(cluster), "-m", "fast", "-w", "stop"])
                stopped = True
            elif status.returncode == 3:
                stopped = True
            else:
                raise RuntimeError("Cluster status unknown; temporary files retained")
        if stopped:
            shutil.rmtree(work)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, psycopg.Error, subprocess.TimeoutExpired) as error:
        # Connection strings and server errors can contain passwords or SQL values.
        print(f"FAIL: test PostgreSQL setup ({type(error).__name__}); details withheld", file=sys.stderr)
        raise SystemExit(1)
