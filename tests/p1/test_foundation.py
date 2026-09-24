import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import psycopg

from callops.config import ConfigurationError, Settings
from callops.db import DatabaseError, migrate, readiness


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.values = {
            "CALLOPS_DATABASE_URL": "postgresql://callops_app:test-only@127.0.0.1:55439/callops_test?sslmode=disable",
            "CALLOPS_DATA_ROOT": self.temp.name,
        }

    def test_missing_values_fail_without_loading_dotenv_or_generic_database_url(self):
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"DATABASE_URL": self.values["CALLOPS_DATABASE_URL"]})
        for name in self.values:
            values = self.values.copy()
            del values[name]
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                Settings.from_env(values)

    def test_secret_repr_and_invalid_trust_boundaries(self):
        self.assertNotIn("test-only", repr(Settings.from_env(self.values)))
        for url in ("sqlite:///test", "postgresql://postgres:secret@localhost:5432/db",
                    "postgresql://callops_app:secret@remote.invalid:5432/db?sslmode=disable",
                    "postgresql://callops_app:secret@localhost:5432/db?options=unsafe"):
            with self.subTest(url_scheme=url.split(":")[0]), self.assertRaises(ConfigurationError):
                Settings.from_env({**self.values, "CALLOPS_DATABASE_URL": url})
        for root in ("relative", str(Path(__file__).resolve().parents[2] / "data")):
            with self.subTest(root_type="invalid"), self.assertRaises(ConfigurationError):
                Settings.from_env({**self.values, "CALLOPS_DATA_ROOT": root})


class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Provisioning is explicit and separate: this suite never creates/drops a DB.
        if not os.environ.get("CALLOPS_TEST_DATABASE_URL") or not os.environ.get("CALLOPS_TEST_MIGRATION_DATABASE_URL"):
            raise RuntimeError("P1 requires a dedicated PostgreSQL test DB; run scripts/test_postgres.py")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        values = {"CALLOPS_DATA_ROOT": cls.temp.name,
                  "CALLOPS_DATABASE_URL": os.environ["CALLOPS_TEST_DATABASE_URL"],
                  "CALLOPS_MIGRATION_DATABASE_URL": os.environ["CALLOPS_TEST_MIGRATION_DATABASE_URL"]}
        cls.app = Settings.from_env(values)
        cls.owner = Settings.from_env(values, migration=True)
        if cls.app.database != "callops_test" or cls.owner.database != "callops_test" or cls.app.host != "127.0.0.1" or cls.owner.host != "127.0.0.1":
            raise RuntimeError("Tests only accept 127.0.0.1/callops_test")

    def test_migration_readiness_constraints_and_role_separation(self):
        with self.assertRaises(DatabaseError):
            readiness(self.app)
        source = Path(__file__).resolve().parents[2] / "migrations"
        names = sorted(path.name for path in source.glob('*.sql'))
        self.assertEqual(migrate(self.owner), names)
        self.assertEqual(migrate(self.owner), [])
        readiness(self.app)
        with psycopg.connect(self.app.database_url) as conn:
            row = conn.execute("INSERT INTO callops.recordings (id, sha256, size_bytes, storage_key) VALUES (gen_random_uuid(), %s, 10, gen_random_uuid()) RETURNING classification_status", ("a" * 64,)).fetchone()
            self.assertEqual(row[0], "PENDING")
            conn.rollback()
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO callops.recordings (id, sha256, size_bytes, storage_key) VALUES (gen_random_uuid(), 'invalid', -1, gen_random_uuid())")
            conn.rollback()
            for statement in ("CREATE TABLE callops.forbidden (id int)",
                              "CREATE TABLE public.forbidden (id int)",
                              "DELETE FROM callops.schema_migrations",
                              "SET ROLE callops_migrator"):
                with self.subTest(operation=statement.split()[0]), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
                conn.rollback()
        with self.assertRaises(DatabaseError):
            migrate(replace(self.owner, database_url=self.app.database_url))

        # A later broken migration must roll back its DDL and version marker.
        source = Path(__file__).resolve().parents[2] / "migrations"
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            initial = (source / "001_initial.sql").read_bytes()
            for name in names:
                (directory / name).write_bytes((source / name).read_bytes())
            (directory / "999_broken.sql").write_text("CREATE TABLE callops.partial (id int); SELECT nonexistent_column;", encoding="utf-8")
            with self.assertRaises(DatabaseError):
                migrate(self.owner, directory)
            with psycopg.connect(self.owner.database_url) as conn:
                self.assertIsNone(conn.execute("SELECT to_regclass('callops.partial')").fetchone()[0])
                self.assertEqual(conn.execute("SELECT count(*) FROM callops.schema_migrations").fetchone()[0], len(names))
            (directory / "999_broken.sql").unlink()
            (directory / "001_initial.sql").write_bytes(initial + b"\n-- changed\n")
            with self.assertRaises(DatabaseError):
                migrate(self.owner, directory)
