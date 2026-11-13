import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

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

    def seed_legacy_schedules(self, conn, other_recording):
        recording, artifact, other_artifact, stt, analysis, attempt = [uuid4() for _ in range(6)]
        candidate, schedule, manual, orphan = [uuid4() for _ in range(4)]
        conn.execute("""INSERT INTO callops.recordings (id,sha256,size_bytes,storage_key,classification_status)
            VALUES (%s,%s,10,%s,'BUSINESS_APPROVED')""", (recording, 'c' * 64, uuid4()))
        for artifact_id, recording_id in ((artifact, recording), (other_artifact, other_recording)):
            conn.execute('''INSERT INTO callops.artifacts (id,recording_id,version,sha256,size_bytes,expires_at)
                VALUES (%s,%s,1,%s,10,now()+interval '90 days')''', (artifact_id, recording_id, 'd' * 64))
        conn.execute("INSERT INTO callops.jobs (id,recording_id,stage,retry_at) VALUES (%s,%s,'STT',now())", (stt, recording))
        conn.execute("""INSERT INTO callops.jobs (id,recording_id,stage,input_artifact_id,retry_at)
            VALUES (%s,%s,'ANALYSIS',%s,now())""", (analysis, recording, artifact))
        conn.execute('''INSERT INTO callops.attempts (id,job_id,worker_id,lease_token,lease_until,preparation_deadline,result_artifact_id)
            VALUES (%s,%s,'synthetic',%s,now(),now(),%s)''', (attempt, stt, uuid4(), artifact))
        start, end = '2030-01-03T05:00:00+00:00', '2030-01-03T06:00:00+00:00'
        conn.execute('''INSERT INTO callops.schedule_candidates
            (id,recording_id,analysis_job_id,position,segment_ids,title,time_hint,start_at,end_at,status,revision,logical_item_id,expires_at)
            VALUES (%s,%s,%s,1,ARRAY[1],'draft','',%s,%s,'AWAITING_APPROVAL',2,%s,now()+interval '90 days')''',
            (candidate, recording, analysis, start, end, schedule))
        snapshot = Jsonb({'title': 'approved', 'start_at': start, 'end_at': end,
                          'transcript_id': str(artifact), 'segment_ids': [1]})
        conn.execute('''INSERT INTO callops.schedule_approvals (candidate_id,revision,actor,approved_at,snapshot,snapshot_sha256)
            VALUES (%s,1,'owner',now(),%s,%s)''', (candidate, snapshot, 'e' * 64))
        conn.execute('INSERT INTO callops.manual_schedules (id,request_id,title,start_at,end_at) VALUES (%s,%s,%s,%s,%s)',
                     (manual, uuid4(), 'manual', start, end))
        for event_id in (schedule, orphan):
            conn.execute("INSERT INTO callops.calendar_deliveries VALUES (%s,1,%s,'SUCCEEDED')", (event_id, 'f' * 64))
            conn.execute('INSERT INTO callops.mock_calendar_events VALUES (%s,%s)', (event_id, 'f' * 64))
        return recording, artifact, other_artifact, analysis, attempt, candidate, schedule, manual, orphan

    def check_migrated_schedules(self, conn, fixture, other_recording):
        recording, artifact, other_artifact, analysis, attempt, candidate, schedule, manual, orphan = fixture
        self.assertEqual(conn.execute('SELECT source,title FROM callops.schedules WHERE id=%s', (manual,)).fetchone(),
                         ('manual', 'manual'))
        self.assertEqual(conn.execute('SELECT title,approved_candidate_id,approved_revision FROM callops.schedules WHERE id=%s',
                                      (schedule,)).fetchone(), ('approved', candidate, 1))
        self.assertEqual(conn.execute('SELECT source,title,deleted_at IS NOT NULL FROM callops.schedules WHERE id=%s',
                                      (orphan,)).fetchone(), ('call', None, True))
        self.assertEqual(conn.execute('SELECT candidate_id FROM callops.calendar_deliveries WHERE logical_item_id=%s',
                                      (schedule,)).fetchone(), (candidate,))
        self.assertEqual(conn.execute('SELECT recording_id FROM callops.attempts WHERE id=%s', (attempt,)).fetchone(),
                         (recording,))
        for query, values in (
            ('UPDATE callops.jobs SET input_artifact_id=%s WHERE id=%s', (other_artifact, analysis)),
            ('UPDATE callops.jobs SET active_attempt=%s WHERE id=%s', (attempt, analysis)),
            ('UPDATE callops.attempts SET result_artifact_id=%s WHERE id=%s', (other_artifact, attempt)),
            ('''INSERT INTO callops.schedule_candidates
                (id,recording_id,analysis_job_id,position,segment_ids,title,time_hint,status,expires_at)
                VALUES (%s,%s,%s,2,ARRAY[1],'wrong recording','','NEEDS_CONFIRMATION',now())''',
             (uuid4(), other_recording, analysis)),
        ):
            with self.subTest(query=query), self.assertRaises(psycopg.errors.ForeignKeyViolation), conn.transaction():
                conn.execute(query, values)
        conn.execute('''INSERT INTO callops.schedule_candidates
            (id,recording_id,analysis_job_id,position,segment_ids,title,time_hint,status,logical_item_id,expires_at)
            VALUES (%s,%s,%s,2,ARRAY[1],'new analysis','', 'NEEDS_CONFIRMATION',%s,now())''',
            (uuid4(), recording, analysis, schedule))
        self.assertEqual(conn.execute('SELECT count(*) FROM callops.schedule_candidates WHERE logical_item_id=%s',
                                      (schedule,)).fetchone(), (2,))
        conn.execute('DELETE FROM callops.schedule_candidates WHERE recording_id=%s', (recording,))
        self.assertEqual(conn.execute('SELECT title,approved_candidate_id,approved_revision FROM callops.schedules WHERE id=%s',
                                      (schedule,)).fetchone(), ('approved', None, None))
        conn.execute('DELETE FROM callops.calendar_deliveries WHERE logical_item_id IN (%s,%s)', (schedule, orphan))
        conn.execute('DELETE FROM callops.mock_calendar_events WHERE event_id IN (%s,%s)', (schedule, orphan))
        conn.execute('DELETE FROM callops.schedules WHERE id IN (%s,%s,%s)', (schedule, manual, orphan))
        conn.execute('DELETE FROM callops.attempts WHERE id=%s', (attempt,))
        conn.execute('DELETE FROM callops.jobs WHERE recording_id=%s', (recording,))
        conn.execute('DELETE FROM callops.artifacts WHERE id IN (%s,%s)', (artifact, other_artifact))
        conn.execute('DELETE FROM callops.recordings WHERE id=%s', (recording,))

    def test_migration_readiness_constraints_and_role_separation(self):
        with self.assertRaises(DatabaseError):
            readiness(self.app)
        source = Path(__file__).resolve().parents[2] / "migrations"
        names = sorted(path.name for path in source.glob('*.sql'))
        with tempfile.TemporaryDirectory() as temp:
            old_migrations = Path(temp)
            for name in names[:2]:
                (old_migrations / name).write_bytes((source / name).read_bytes())
            self.assertEqual(migrate(self.owner, old_migrations), names[:2])
        legacy_id = uuid4()
        with psycopg.connect(self.app.database_url) as conn:
            conn.execute("INSERT INTO callops.recordings (id,sha256,size_bytes,storage_key) VALUES (%s,%s,10,%s)",
                         (legacy_id, "b" * 64, uuid4()))
        with tempfile.TemporaryDirectory() as temp:
            legacy_migrations = Path(temp)
            for name in names[:6]:
                (legacy_migrations / name).write_bytes((source / name).read_bytes())
            self.assertEqual(migrate(self.owner, legacy_migrations), names[2:6])
        with psycopg.connect(self.app.database_url) as conn:
            fixture = self.seed_legacy_schedules(conn, legacy_id)
        self.assertEqual(migrate(self.owner), names[6:])
        self.assertEqual(migrate(self.owner), [])
        readiness(self.app)
        with psycopg.connect(self.app.database_url) as conn:
            self.check_migrated_schedules(conn, fixture, legacy_id)
            legacy = conn.execute("SELECT counterparty,call_started_at,call_time_trusted FROM callops.recordings WHERE id=%s",
                                  (legacy_id,)).fetchone()
            self.assertEqual(legacy, (None, None, False))
            conn.execute("DELETE FROM callops.recordings WHERE id=%s", (legacy_id,))
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
