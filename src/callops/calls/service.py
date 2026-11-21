"""Single-server ingestion, classification and copy accounting. No AI adapters."""

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from ..common import Conflict, digest, identifier, utcnow, valid_hash
from ..processing.transcript import validate_result
from .audio import audio_duration

CALL_TIMEZONE = timezone(timedelta(hours=9))
AUDIO_RETENTION = timedelta(days=30)


def audio_expires_at(recording):
    return recording['received_at'] + AUDIO_RETENTION


def audio_expired(recording, now):
    return recording['audio_deletion_state'] != 'NONE' or audio_expires_at(recording) <= now


def cleanup_state(files_removed, worker_pending):
    if not files_removed:
        return 'RETRY_WAIT'
    return 'REQUESTED' if worker_pending else 'COMPLETE'


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

    def transcript(self, recording_id, artifact_id=None, *, include_summary=False):
        with self.transaction() as conn:
            row = self.recording(conn, recording_id)
            if (row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED')
                    or row['deletion_state'] != 'NONE'
                    or (row['classification_status'] != 'BUSINESS_APPROVED' and row['expires_at'] <= self.clock())):
                raise Conflict('RECORDING_UNAVAILABLE')
            query = '''SELECT f.* FROM callops.artifacts f
                JOIN callops.attempts a ON a.result_artifact_id=f.id
                JOIN callops.jobs j ON j.id=a.job_id
                WHERE j.recording_id=%s AND f.recording_id=j.recording_id AND j.stage='STT' '''
            values = [row['id']]
            if artifact_id is not None:
                query += 'AND f.id=%s '
                values.append(identifier(artifact_id))
            query += 'ORDER BY j.pipeline_version DESC LIMIT 1'
            artifact = conn.execute(query, values).fetchone()
            if artifact is None:
                if artifact_id is not None:
                    raise Conflict('TRANSCRIPT_NOT_FOUND')
                return row, None, None
            if row['classification_status'] != 'BUSINESS_APPROVED':
                raise Conflict('APPROVAL_REVOKED')
            try:
                result = self.read_transcript_artifact(artifact, row['duration_seconds'])
            except Conflict:
                if artifact_id is not None:
                    raise
                # The call page also owns playback and classification when its transcript is unavailable.
                row['transcript_unavailable'] = True
                return row, None, None
            if include_summary:
                row['summary'] = self.analysis_summary(conn, row, artifact)
            return row, artifact, result

    def analysis_summary(self, conn, recording, transcript):
        artifact = conn.execute("""SELECT f.* FROM callops.jobs j
            JOIN callops.attempts a ON a.id=j.active_attempt
            JOIN callops.artifacts f ON f.id=a.result_artifact_id
            WHERE j.recording_id=%s AND j.input_artifact_id=%s
                AND j.stage='ANALYSIS' AND j.status='SUCCEEDED'
            ORDER BY j.pipeline_version DESC, f.id DESC LIMIT 1""",
            (recording['id'], transcript['id'])).fetchone()
        if artifact is None:
            return None
        try:
            result = self.read_result_artifact(artifact, 'ANALYSIS', recording['duration_seconds'])
        except Conflict:
            # A missing or expired analysis must not hide the valid transcript.
            return None
        return result.get('summary')

    def read_transcript_artifact(self, artifact, duration_seconds):
        return self.read_result_artifact(artifact, 'STT', duration_seconds)

    def read_result_artifact(self, artifact, stage, duration_seconds):
        """Read an authorized artifact while the caller holds the transaction."""
        if not artifact['available'] or artifact['expires_at'] <= self.clock():
            raise Conflict('TRANSCRIPT_UNAVAILABLE')
        size = artifact['size_bytes']
        if size <= 0:
            raise Conflict('INPUT_CORRUPT')
        try:
            with self.safe_path('artifacts', str(artifact['id'])).open('rb') as incoming:
                data = incoming.read(size + 1)
        except FileNotFoundError:
            raise Conflict('INPUT_MISSING') from None
        if len(data) != size or digest(data) != artifact['sha256']:
            raise Conflict('INPUT_CORRUPT')
        try:
            return validate_result(json.loads(data), stage, duration_seconds)
        except (ValueError, UnicodeError, RecursionError):
            raise Conflict('INPUT_CORRUPT') from None

    def ingest(self, name, completed_sha256):
        """Producer supplies a checksum only after closing its completed transfer.

        No directory watcher guesses completion from file size or age.
        """
        valid_hash(completed_sha256)
        source = self.safe_path('sync-inbox', name)
        if source.suffix.lower() == '.part':
            raise Conflict('TRANSFER_INCOMPLETE')
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
                with part.open('rb') as audio:
                    duration = audio_duration(audio, source.suffix.lower())
                row = conn.execute('SELECT * FROM callops.recordings WHERE sha256=%s FOR UPDATE',
                                   (completed_sha256,)).fetchone()
                if row is None:
                    target = self.safe_path('quarantine', str(key))
                    os.replace(part, target)
                    row = conn.execute('''INSERT INTO callops.recordings
                        (id,sha256,size_bytes,storage_key,duration_seconds,received_at,expires_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                        (uuid4(), completed_sha256, size, key, duration, now, now + timedelta(hours=72))).fetchone()
                # A verified transfer replaced any previous content at this path.
                conn.execute('''UPDATE callops.receipts SET cleaned=true
                    WHERE source_name=%s AND recording_id<>%s AND NOT cleaned''', (name, row['id']))
                conn.execute('INSERT INTO callops.receipts (id,recording_id,source_name) VALUES (%s,%s,%s)',
                             (uuid4(), row['id'], name))
                conn.execute('DELETE FROM callops.rejected_receipts WHERE source_name=%s', (name,))
                if row['deletion_state'] != 'NONE' or row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED'):
                    conn.execute("UPDATE callops.recordings SET deletion_state='REQUESTED' WHERE id=%s", (row['id'],))
                elif row['audio_deletion_state'] == 'COMPLETE':
                    conn.execute("UPDATE callops.recordings SET audio_deletion_state='REQUESTED' WHERE id=%s", (row['id'],))
        except Conflict:
            # Bad or stalled transfers have a bounded holding time too. Preserve
            # identity so a different transfer reusing the name is not removed.
            if source.is_file():
                info = source.stat()
                identity = f'{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'
                with self.transaction() as conn:
                    conn.execute('''INSERT INTO callops.rejected_receipts AS previous (source_name,identity,expires_at)
                        VALUES (%s,%s,%s) ON CONFLICT (source_name) DO UPDATE SET
                        identity=EXCLUDED.identity,
                        expires_at=CASE WHEN previous.identity=EXCLUDED.identity
                            THEN LEAST(previous.expires_at,EXCLUDED.expires_at) ELSE EXCLUDED.expires_at END,
                        cleanup_failed=previous.identity=EXCLUDED.identity AND previous.cleanup_failed''',
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
            now = self.clock()
            if (row['deletion_state'] != 'NONE' or row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED')
                    or (row['classification_status'] in ('PENDING', 'DEFERRED') and row['expires_at'] <= now)):
                raise Conflict('RECORDING_DELETED')
            if row['classification_status'] == 'BUSINESS_APPROVED' and decision == 'DEFERRED':
                raise Conflict('INVALID_DECISION')
            if decision == 'BUSINESS_APPROVED':
                if audio_expired(row, now):
                    raise Conflict('INPUT_EXPIRED')
                if not self.audio_path(row).is_file():
                    raise Conflict('INPUT_MISSING')
            conn.execute('UPDATE callops.recordings SET classification_status=%s WHERE id=%s', (decision, row['id']))
            conn.execute('INSERT INTO callops.classification_events VALUES (%s,%s,%s,%s)',
                         (uuid4(), row['id'], decision, now))
            if decision == 'BUSINESS_APPROVED':
                conn.execute('''INSERT INTO callops.jobs (id,recording_id,stage,retry_at)
                    VALUES (%s,%s,'STT',%s) ON CONFLICT (recording_id,stage,pipeline_version) DO NOTHING''',
                    (uuid4(), row['id'], now))
            elif decision == 'PRIVATE_EXCLUDED':
                self.request_delete(conn, row['id'])
        self.maintain()

    def request_delete(self, conn, recording_id):
        conn.execute('''UPDATE callops.recordings SET deletion_state='REQUESTED',
            counterparty=NULL,call_started_at=NULL,call_time_source=NULL,call_time_trusted=false
            WHERE id=%s''', (recording_id,))
        conn.execute('DELETE FROM callops.schedule_candidates WHERE recording_id=%s', (recording_id,))
        conn.execute('DELETE FROM callops.task_candidates WHERE recording_id=%s', (recording_id,))
        conn.execute('''UPDATE callops.tasks SET recording_id=NULL,title=NULL,due_at=NULL,status=NULL,
            completed_at=NULL,deleted_at=COALESCE(deleted_at,%s),revision=revision+1
            WHERE recording_id=%s''', (self.clock(), recording_id))
        conn.execute('''WITH removed_schedules AS (
            UPDATE callops.schedules SET recording_id=NULL,title=NULL,start_at=NULL,end_at=NULL,
                approved_candidate_id=NULL,approved_revision=NULL,deleted_at=%s,revision=revision+1
            WHERE recording_id=%s RETURNING id
        )
        UPDATE callops.mock_calendar_events e SET title=NULL,start_at=NULL,end_at=NULL
        FROM removed_schedules s WHERE e.event_id=s.id''', (self.clock(), recording_id))
        conn.execute("UPDATE callops.jobs SET status='CANCELLED' WHERE recording_id=%s", (recording_id,))
        conn.execute('''UPDATE callops.attempts SET cleanup_requested=true WHERE job_id IN
            (SELECT id FROM callops.jobs WHERE recording_id=%s)''', (recording_id,))

    def maintain(self):
        """Run cleanup and return the cutoff used for this pass.

        With DB unavailable, ingestion fails before copying. Real transfer control
        is deliberately not connected until C7 can prove it stops incoming writes.
        """
        with self.transaction() as conn:
            now = self.clock()
            expired = conn.execute('''UPDATE callops.recordings SET classification_status='EXPIRED'
                WHERE classification_status IN ('PENDING','DEFERRED') AND expires_at<=%s RETURNING id''',
                (now,)).fetchall()
            for row in expired:
                self.request_delete(conn, row['id'])
            self.request_retention_cleanup(conn, now)
            conn.execute('''DELETE FROM callops.schedule_candidates c USING callops.jobs j, callops.artifacts f
                WHERE c.analysis_job_id=j.id AND j.input_artifact_id=f.id
                  AND (c.expires_at<=%s OR f.expires_at<=%s OR f.deletion_state<>'NONE')''', (now, now))
            conn.execute('''DELETE FROM callops.task_candidates c USING callops.artifacts f
                WHERE c.transcript_id=f.id AND (c.expires_at<=%s OR f.expires_at<=%s OR NOT f.available)''', (now, now))
            pending = conn.execute('''SELECT
                EXISTS (SELECT 1 FROM callops.rejected_receipts WHERE expires_at<=%s)
                OR EXISTS (SELECT 1 FROM callops.recordings WHERE deletion_state IN ('REQUESTED','RETRY_WAIT')
                    OR (deletion_state='NONE' AND audio_deletion_state IN ('REQUESTED','RETRY_WAIT')))
                OR EXISTS (SELECT 1 FROM callops.artifacts f JOIN callops.recordings r ON r.id=f.recording_id
                    WHERE r.deletion_state='NONE' AND f.deletion_state IN ('REQUESTED','RETRY_WAIT')) AS pending''',
                (now,)).fetchone()['pending']
        if pending:
            # Commit intent before unlinking so a restart can distinguish expiry from lost files.
            with self.transaction() as conn:
                self.clean_rejected_receipts(conn, now)
                self.clean_recordings(conn)
                self.clean_expired_artifacts(conn)
        return now

    def request_retention_cleanup(self, conn, now):
        recordings = conn.execute('''UPDATE callops.recordings SET audio_deletion_state='REQUESTED'
            WHERE classification_status='BUSINESS_APPROVED' AND deletion_state='NONE'
                AND audio_deletion_state='NONE' AND received_at<=%s RETURNING id''',
            (now - AUDIO_RETENTION,)).fetchall()
        if recordings:
            ids = [row['id'] for row in recordings]
            conn.execute('''UPDATE callops.jobs SET status='CANCELLED',error_code='INPUT_EXPIRED'
                WHERE recording_id=ANY(%s) AND stage='STT' AND status NOT IN ('SUCCEEDED','CANCELLED')''', (ids,))
            conn.execute('''UPDATE callops.attempts a SET cleanup_requested=true FROM callops.jobs j
                WHERE a.job_id=j.id AND a.recording_id=ANY(%s) AND j.stage='STT'
                    AND NOT a.cleaned AND NOT a.cleanup_requested''', (ids,))

        artifacts = conn.execute('''UPDATE callops.artifacts f SET deletion_state='REQUESTED',available=false
            FROM callops.recordings r WHERE r.id=f.recording_id AND r.deletion_state='NONE'
                AND f.deletion_state='NONE' AND f.expires_at<=%s RETURNING f.id''', (now,)).fetchall()
        if artifacts:
            ids = [row['id'] for row in artifacts]
            conn.execute('''UPDATE callops.jobs SET status='CANCELLED',error_code='INPUT_EXPIRED'
                WHERE input_artifact_id=ANY(%s) AND status NOT IN ('SUCCEEDED','CANCELLED')''', (ids,))
            conn.execute('''UPDATE callops.attempts a SET cleanup_requested=true FROM callops.jobs j
                WHERE a.job_id=j.id AND NOT a.cleaned AND NOT a.cleanup_requested
                    AND (j.input_artifact_id=ANY(%s) OR a.result_artifact_id=ANY(%s))''', (ids, ids))

    def clean_rejected_receipts(self, conn, now):
        for rejected in conn.execute('SELECT * FROM callops.rejected_receipts WHERE expires_at<=%s', (now,)).fetchall():
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

    def clean_audio_copies(self, conn, row):
        removed = True
        try:
            self.audio_path(row).unlink(missing_ok=True)
        except (OSError, Conflict):
            removed = False
        receipts = conn.execute('SELECT id,source_name FROM callops.receipts WHERE recording_id=%s AND NOT cleaned', (row['id'],)).fetchall()
        for receipt in receipts:
            try:
                path = self.safe_path('sync-inbox', receipt['source_name'])
                if path.exists():
                    with path.open('rb') as incoming:
                        before = os.fstat(incoming.fileno())
                        checksum = hashlib.file_digest(incoming, 'sha256').hexdigest()
                    after = self.safe_path('sync-inbox', receipt['source_name']).stat()
                    if (checksum != row['sha256'] or not os.path.samestat(before, after)
                            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                        raise Conflict('SOURCE_REPLACED')
                    path.unlink()
            except (OSError, Conflict):
                removed = False
            else:
                conn.execute('UPDATE callops.receipts SET cleaned=true WHERE id=%s', (receipt['id'],))
        return removed

    def remove_artifact_file(self, artifact_id):
        try:
            self.safe_path('artifacts', str(artifact_id)).unlink(missing_ok=True)
        except (OSError, Conflict):
            return False
        return True

    def clean_recordings(self, conn):
        rows = conn.execute('''SELECT r.*, EXISTS (SELECT 1 FROM callops.attempts a JOIN callops.jobs j ON j.id=a.job_id
                WHERE a.recording_id=r.id AND NOT a.cleaned
                    AND (r.deletion_state<>'NONE' OR j.stage='STT')) AS worker_pending
            FROM callops.recordings r WHERE r.deletion_state IN ('REQUESTED','RETRY_WAIT')
                OR (r.deletion_state='NONE' AND r.audio_deletion_state IN ('REQUESTED','RETRY_WAIT'))''').fetchall()
        for row in rows:
            removed = self.clean_audio_copies(conn, row)
            if row['deletion_state'] == 'NONE':
                state = cleanup_state(removed, row['worker_pending'])
                if state != row['audio_deletion_state']:
                    conn.execute('UPDATE callops.recordings SET audio_deletion_state=%s WHERE id=%s', (state, row['id']))
                continue

            for artifact in conn.execute('SELECT id FROM callops.artifacts WHERE recording_id=%s', (row['id'],)).fetchall():
                if self.remove_artifact_file(artifact['id']):
                    conn.execute('UPDATE callops.artifacts SET available=false WHERE id=%s AND available', (artifact['id'],))
                else:
                    removed = False
            state = cleanup_state(removed, row['worker_pending'])
            if state == 'COMPLETE':
                conn.execute('''UPDATE callops.artifacts SET deletion_state='COMPLETE'
                    WHERE recording_id=%s AND deletion_state<>'COMPLETE' ''', (row['id'],))
            if state != row['deletion_state']:
                conn.execute('''UPDATE callops.recordings SET deletion_state=%s,
                    audio_deletion_state=CASE WHEN %s='COMPLETE' AND audio_deletion_state<>'NONE'
                        THEN 'COMPLETE' ELSE audio_deletion_state END WHERE id=%s''', (state, state, row['id']))

    def clean_expired_artifacts(self, conn):
        artifacts = conn.execute('''SELECT f.id,f.deletion_state, EXISTS (SELECT 1 FROM callops.attempts a
                JOIN callops.jobs j ON j.id=a.job_id WHERE a.recording_id=f.recording_id AND NOT a.cleaned
                    AND (j.input_artifact_id=f.id OR a.result_artifact_id=f.id)) AS worker_pending
            FROM callops.artifacts f JOIN callops.recordings r ON r.id=f.recording_id
            WHERE r.deletion_state='NONE' AND f.deletion_state IN ('REQUESTED','RETRY_WAIT')''').fetchall()
        for artifact in artifacts:
            state = cleanup_state(self.remove_artifact_file(artifact['id']), artifact['worker_pending'])
            if state != artifact['deletion_state']:
                conn.execute('UPDATE callops.artifacts SET deletion_state=%s WHERE id=%s', (state, artifact['id']))

    def recover(self):
        """Run before serving; exclusive transaction excludes active file writes."""
        with self.transaction() as conn:
            audio = {str(row['storage_key']) for row in conn.execute('SELECT storage_key FROM callops.recordings')}
            artifacts = {str(row['id']) for row in conn.execute('''SELECT f.id FROM callops.artifacts f
                JOIN callops.recordings r ON r.id=f.recording_id WHERE f.available
                    OR f.deletion_state IN ('REQUESTED','RETRY_WAIT') OR r.deletion_state IN ('REQUESTED','RETRY_WAIT')''')}
            for area, expected in (('quarantine', audio), ('artifacts', artifacts)):
                for path in self.safe_path(area).iterdir():
                    if path.name not in expected:
                        # Only our own generated keys may be removed during recovery.
                        identifier(path.name.removesuffix('.part'))
                        self.safe_path(area, path.name).unlink()
            now = self.clock()
            for row in conn.execute('''SELECT * FROM callops.recordings WHERE classification_status='BUSINESS_APPROVED'
                    AND deletion_state='NONE' AND audio_deletion_state='NONE' AND received_at>%s''',
                    (now - AUDIO_RETENTION,)).fetchall():
                if not self.audio_path(row).is_file():
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code='INPUT_MISSING' WHERE recording_id=%s AND stage='STT' AND status NOT IN ('SUCCEEDED','CANCELLED')", (row['id'],))
            for row in conn.execute('''SELECT f.id FROM callops.artifacts f JOIN callops.recordings r ON r.id=f.recording_id
                    WHERE f.available AND f.expires_at>%s AND r.deletion_state='NONE' ''', (now,)).fetchall():
                if not self.safe_path('artifacts', str(row['id'])).is_file():
                    conn.execute('UPDATE callops.artifacts SET available=false WHERE id=%s', (row['id'],))
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code='INPUT_MISSING' WHERE active_attempt IN (SELECT id FROM callops.attempts WHERE result_artifact_id=%s) AND status='SUCCEEDED'", (row['id'],))
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code='INPUT_MISSING' WHERE input_artifact_id=%s AND status NOT IN ('SUCCEEDED','CANCELLED')", (row['id'],))
        self.maintain()
