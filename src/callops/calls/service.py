"""Single-server ingestion, classification and copy accounting. No AI adapters."""

import hashlib
import os
import re
import stat
import wave
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from ..common import Conflict, digest, identifier, utcnow, valid_hash

CALL_TIMEZONE = timezone(timedelta(hours=9))


def counterparty_name(value):
    if not isinstance(value, str) or len(value) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Conflict('INVALID_COUNTERPARTY')
    return value.strip() or None


def recording_filters(counterparty, from_date, to_date):
    name = counterparty_name(counterparty)
    try:
        start = (datetime.combine(date.fromisoformat(from_date), time.min, CALL_TIMEZONE).astimezone(timezone.utc)
                 if from_date else None)
        end = (datetime.combine(date.fromisoformat(to_date) + timedelta(days=1), time.min, CALL_TIMEZONE)
               .astimezone(timezone.utc) if to_date else None)
        if start and end and start >= end:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise Conflict('INVALID_CALL_FILTER') from None
    clauses, values = [], []
    if name:
        clauses.append('strpos(lower(r.counterparty),lower(%s)) > 0')
        values.append(name)
    if start:
        clauses.append('r.call_started_at >= %s')
        values.append(start)
    if end:
        clauses.append('r.call_started_at < %s')
        values.append(end)
    return clauses, values


class Service:
    MAX_AUDIO = 500 * 1024 * 1024

    def __init__(self, settings, clock=utcnow):
        self.settings, self.clock = settings, clock
        self.root = settings.data_root
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('sync-inbox', 'quarantine', 'artifacts'):
            path = self.root / name
            path.mkdir(exist_ok=True)
            self.safe_path(name)

    def safe_path(self, area, name=None):
        if area not in {'sync-inbox', 'quarantine', 'artifacts'}:
            raise Conflict('INVALID_PATH')
        if name is not None and (not isinstance(name, str) or not name or len(name) > 240
                or name in {'.', '..'} or any(c in name for c in '/\\:\x00')
                or name.endswith((' ', '.'))):
            raise Conflict('INVALID_PATH')
        base = self.root / area
        path = base if name is None else base / name
        for item in (self.root, base, path):
            if item.exists() or item.is_symlink():
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise Conflict('INVALID_PATH')
        if not path.resolve().is_relative_to(self.root):
            raise Conflict('INVALID_PATH')
        return path

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.settings.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute("SET LOCAL TIME ZONE 'UTC'")
            conn.execute("SET LOCAL lock_timeout = '10s'")
            # ponytail: serialize file/DB mutations for one server; use per-record
            # locks with a documented lock order if multi-server throughput is needed.
            conn.execute('SELECT pg_advisory_xact_lock(65001002)')
            yield conn

    def recording(self, conn, recording_id):
        row = conn.execute('SELECT * FROM callops.recordings WHERE id=%s FOR UPDATE',
                           (identifier(recording_id),)).fetchone()
        if row is None:
            raise Conflict('NOT_FOUND')
        return row

    def audio_path(self, row):
        return self.safe_path('quarantine', str(row['storage_key']))

    def update_metadata(self, recording_id, counterparty, call_started_at):
        name = counterparty_name(counterparty)
        started = None
        if call_started_at:
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?', call_started_at):
                    raise ValueError
                local = datetime.fromisoformat(call_started_at)
                started = local.replace(tzinfo=CALL_TIMEZONE).astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                raise Conflict('INVALID_CALL_TIME') from None
        with self.transaction() as conn:
            row = self.recording(conn, recording_id)
            if (row['classification_status'] not in ('PENDING', 'DEFERRED')
                    or row['deletion_state'] != 'NONE' or row['expires_at'] <= self.clock()):
                raise Conflict('METADATA_LOCKED')
            conn.execute('''UPDATE callops.recordings
                SET counterparty=%s,call_started_at=%s,call_time_source=%s,call_time_trusted=%s
                WHERE id=%s''', (name, started, 'USER_CONFIRMED' if started else None,
                                  started is not None, row['id']))

    def list_recordings(self, counterparty='', from_date='', to_date=''):
        clauses, values = recording_filters(counterparty, from_date, to_date)
        query = 'SELECT r.* FROM callops.recordings r'
        if clauses:
            clauses.insert(0, """deletion_state='NONE' AND (classification_status='BUSINESS_APPROVED'
                OR (classification_status IN ('PENDING','DEFERRED') AND expires_at>%s))""")
            values.insert(0, self.clock())
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY received_at DESC, id DESC LIMIT 200'
        with self.transaction() as conn:
            return conn.execute(query, values).fetchall()

    def ingest(self, name, completed_sha256):
        """Producer supplies a checksum only after closing its completed transfer.

        No directory watcher guesses completion from file size/age. Initially PCM
        WAV only; other phone codecs need the later isolated decoder integration.
        """
        valid_hash(completed_sha256)
        source = self.safe_path('sync-inbox', name)
        key, now = uuid4(), self.clock()
        part = self.safe_path('quarantine', str(key) + '.part')
        target = self.safe_path('quarantine', str(key))
        try:
            with self.transaction() as conn:
                before = source.stat()
                if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= self.MAX_AUDIO:
                    raise Conflict('INVALID_AUDIO')
                sha = hashlib.sha256()
                size = 0
                with source.open('rb') as incoming, part.open('xb') as outgoing:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                        size += len(chunk)
                        if size > self.MAX_AUDIO:
                            raise Conflict('INVALID_AUDIO')
                        sha.update(chunk)
                        outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                after = self.safe_path('sync-inbox', name).stat()
                with source.open('rb') as incoming:
                    final_hash = hashlib.file_digest(incoming, 'sha256').hexdigest()
                if ((before.st_ino, before.st_size, before.st_mtime_ns) !=
                        (after.st_ino, after.st_size, after.st_mtime_ns)
                        or size != before.st_size or sha.hexdigest() != completed_sha256
                        or final_hash != completed_sha256):
                    raise Conflict('TRANSFER_INCOMPLETE')
                try:
                    with wave.open(str(part), 'rb') as audio:
                        frames = audio.getnframes()
                        duration = frames / audio.getframerate()
                        if not 0 < duration <= 7200 or audio.getnchannels() not in (1, 2):
                            raise Conflict('INVALID_AUDIO')
                        remaining = frames * audio.getnchannels() * audio.getsampwidth()
                        while chunk := audio.readframes(65536):
                            remaining -= len(chunk)
                        if remaining != 0:
                            raise Conflict('INVALID_AUDIO')
                except (wave.Error, EOFError, ZeroDivisionError):
                    raise Conflict('INVALID_AUDIO') from None
                row = conn.execute('SELECT * FROM callops.recordings WHERE sha256=%s FOR UPDATE',
                                   (completed_sha256,)).fetchone()
                if row is None:
                    target = self.safe_path('quarantine', str(key))
                    os.replace(part, target)
                    row = conn.execute('''INSERT INTO callops.recordings
                        (id,sha256,size_bytes,storage_key,duration_seconds,received_at,expires_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                        (uuid4(), completed_sha256, size, key, duration, now, now + timedelta(hours=72))).fetchone()
                conn.execute('INSERT INTO callops.receipts (id,recording_id,source_name) VALUES (%s,%s,%s)',
                             (uuid4(), row['id'], name))
                conn.execute('DELETE FROM callops.rejected_receipts WHERE source_name=%s', (name,))
                if row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED'):
                    conn.execute("UPDATE callops.recordings SET deletion_state='REQUESTED' WHERE id=%s", (row['id'],))
        except Conflict:
            # Bad or stalled transfers have a bounded holding time too. Preserve
            # identity so a different transfer reusing the name is not removed.
            if source.is_file():
                info = source.stat()
                identity = f'{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'
                with self.transaction() as conn:
                    conn.execute('''INSERT INTO callops.rejected_receipts (source_name,identity,expires_at)
                        VALUES (%s,%s,%s) ON CONFLICT (source_name) DO UPDATE SET
                        identity=EXCLUDED.identity,expires_at=LEAST(callops.rejected_receipts.expires_at,EXCLUDED.expires_at)''',
                        (name, identity, now + timedelta(hours=24)))
            raise
        finally:
            part.unlink(missing_ok=True)
            # Keep a renamed file on an uncertain DB commit. Startup recovery
            # determines whether it is referenced before removing an orphan.
        self.maintain()
        return str(row['id'])

    def classify(self, recording_id, decision):
        if decision not in ('BUSINESS_APPROVED', 'PRIVATE_EXCLUDED', 'DEFERRED'):
            raise Conflict('INVALID_DECISION')
        self.maintain()
        with self.transaction() as conn:
            row = self.recording(conn, recording_id)
            if row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED'):
                raise Conflict('RECORDING_DELETED')
            if row['classification_status'] == 'BUSINESS_APPROVED' and decision == 'DEFERRED':
                raise Conflict('INVALID_DECISION')
            if decision == 'BUSINESS_APPROVED' and not self.audio_path(row).is_file():
                raise Conflict('INPUT_MISSING')
            conn.execute('UPDATE callops.recordings SET classification_status=%s WHERE id=%s', (decision, row['id']))
            conn.execute('INSERT INTO callops.classification_events VALUES (%s,%s,%s,%s)',
                         (uuid4(), row['id'], decision, self.clock()))
            if decision == 'BUSINESS_APPROVED':
                conn.execute('''INSERT INTO callops.jobs (id,recording_id,stage,retry_at)
                    VALUES (%s,%s,'STT',%s) ON CONFLICT (recording_id,stage,pipeline_version) DO NOTHING''',
                    (uuid4(), row['id'], self.clock()))
            elif decision == 'PRIVATE_EXCLUDED':
                self.request_delete(conn, row['id'])
        self.maintain()

    def request_delete(self, conn, recording_id):
        conn.execute('''UPDATE callops.recordings SET deletion_state='REQUESTED',
            counterparty=NULL,call_started_at=NULL,call_time_source=NULL,call_time_trusted=false
            WHERE id=%s''', (recording_id,))
        conn.execute("UPDATE callops.jobs SET status='CANCELLED' WHERE recording_id=%s", (recording_id,))
        conn.execute('''UPDATE callops.attempts SET cleanup_requested=true WHERE job_id IN
            (SELECT id FROM callops.jobs WHERE recording_id=%s)''', (recording_id,))

    def maintain(self):
        """Explicit scheduler/CLI action; GET requests never expire or delete data.

        With DB unavailable, ingestion fails before copying. Real transfer control
        is deliberately not connected until C7 can prove it stops incoming writes.
        """
        with self.transaction() as conn:
            for rejected in conn.execute('SELECT * FROM callops.rejected_receipts WHERE expires_at<=%s', (self.clock(),)).fetchall():
                try:
                    path = self.safe_path('sync-inbox', rejected['source_name'])
                    if path.exists():
                        info = path.stat()
                        identity = f'{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'
                        if identity != rejected['identity']:
                            raise Conflict('SOURCE_REPLACED')
                        path.unlink()
                    conn.execute('DELETE FROM callops.rejected_receipts WHERE source_name=%s', (rejected['source_name'],))
                except (OSError, Conflict):
                    conn.execute('UPDATE callops.rejected_receipts SET cleanup_failed=true WHERE source_name=%s', (rejected['source_name'],))
            expired = conn.execute('''UPDATE callops.recordings SET classification_status='EXPIRED'
                WHERE classification_status IN ('PENDING','DEFERRED') AND expires_at<=%s RETURNING id''',
                (self.clock(),)).fetchall()
            for row in expired:
                self.request_delete(conn, row['id'])
            for row in conn.execute("SELECT * FROM callops.recordings WHERE deletion_state IN ('REQUESTED','RETRY_WAIT') FOR UPDATE").fetchall():
                try:
                    self.audio_path(row).unlink(missing_ok=True)
                    for receipt in conn.execute('SELECT * FROM callops.receipts WHERE recording_id=%s AND NOT cleaned', (row['id'],)).fetchall():
                        path = self.safe_path('sync-inbox', receipt['source_name'])
                        if path.exists():
                            with path.open('rb') as incoming:
                                if hashlib.file_digest(incoming, 'sha256').hexdigest() != row['sha256']:
                                    # A reused name is another transfer, never delete it.
                                    raise Conflict('SOURCE_REPLACED')
                            path.unlink()
                        conn.execute('UPDATE callops.receipts SET cleaned=true WHERE id=%s', (receipt['id'],))
                    for artifact in conn.execute('SELECT id FROM callops.artifacts WHERE recording_id=%s', (row['id'],)).fetchall():
                        self.safe_path('artifacts', str(artifact['id'])).unlink(missing_ok=True)
                    conn.execute('UPDATE callops.artifacts SET available=false WHERE recording_id=%s', (row['id'],))
                    pending = conn.execute('''SELECT 1 FROM callops.attempts a JOIN callops.jobs j ON j.id=a.job_id
                        WHERE j.recording_id=%s AND NOT a.cleaned LIMIT 1''', (row['id'],)).fetchone()
                    state = 'REQUESTED' if pending else 'COMPLETE'
                except (OSError, Conflict):
                    state = 'RETRY_WAIT'
                conn.execute('UPDATE callops.recordings SET deletion_state=%s WHERE id=%s', (state, row['id']))

    def recover(self):
        """Run before serving; exclusive transaction excludes active file writes."""
        with self.transaction() as conn:
            audio = {str(row['storage_key']) for row in conn.execute('SELECT storage_key FROM callops.recordings')}
            artifacts = {str(row['id']) for row in conn.execute('SELECT id FROM callops.artifacts WHERE available')}
            for area, expected in (('quarantine', audio), ('artifacts', artifacts)):
                for path in self.safe_path(area).iterdir():
                    if path.name not in expected:
                        # Only our own generated keys may be removed during recovery.
                        identifier(path.name.removesuffix('.part'))
                        self.safe_path(area, path.name).unlink()
            for row in conn.execute("SELECT * FROM callops.recordings WHERE classification_status='BUSINESS_APPROVED'").fetchall():
                if not self.audio_path(row).is_file():
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code='INPUT_MISSING' WHERE recording_id=%s AND stage='STT' AND status NOT IN ('SUCCEEDED','CANCELLED')", (row['id'],))
            for row in conn.execute('SELECT id FROM callops.artifacts WHERE available').fetchall():
                if not self.safe_path('artifacts', str(row['id'])).is_file():
                    conn.execute('UPDATE callops.artifacts SET available=false WHERE id=%s', (row['id'],))
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code='INPUT_MISSING' WHERE active_attempt IN (SELECT id FROM callops.attempts WHERE result_artifact_id=%s) AND status='SUCCEEDED'", (row['id'],))
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code='INPUT_MISSING' WHERE input_artifact_id=%s AND status NOT IN ('SUCCEEDED','CANCELLED')", (row['id'],))
        self.maintain()
