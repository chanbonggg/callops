"""PostgreSQL queue, lease and artifact storage for processing workers."""

import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

from ..calls.service import audio_expired, audio_expires_at
from ..common import Conflict, digest, identifier, valid_hash
from ..schedule.service import store_candidates, validated_events
from ..task_candidates import store_task_candidates, validated_tasks
from .transcript import validate_result


class Queue:
    MAX_RESULT = 8 * 1024 * 1024

    def __init__(self, service, *, real_worker=''):
        self.service = service
        self.real_worker = real_worker

    def input_info(self, conn, job, recording, now):
        if job['stage'] == 'STT':
            if audio_expired(recording, now):
                raise Conflict('INPUT_EXPIRED')
            return (self.service.audio_path(recording), recording['sha256'],
                    audio_expires_at(recording), None, 1)
        artifact = conn.execute('SELECT * FROM callops.artifacts WHERE id=%s', (job['input_artifact_id'],)).fetchone()
        if artifact is None:
            raise Conflict('INPUT_MISSING')
        if artifact['deletion_state'] != 'NONE' or artifact['expires_at'] <= now:
            raise Conflict('INPUT_EXPIRED')
        if not artifact['available']:
            raise Conflict('INPUT_MISSING')
        return (self.service.safe_path('artifacts', str(artifact['id'])), artifact['sha256'],
                artifact['expires_at'], artifact['id'], artifact['version'])

    def reap(self):
        # Apply input expiry and lease timeouts at the same cutoff.
        now = self.service.maintain()
        with self.service.transaction() as conn:
            rows = conn.execute('''SELECT j.*,a.lease_until,a.preparation_deadline,a.execution_deadline,a.upload_deadline
                FROM callops.jobs j JOIN callops.attempts a ON a.id=j.active_attempt
                WHERE j.status IN ('PREPARING','RUNNING','UPLOADING') FOR UPDATE OF j,a''').fetchall()
            for job in rows:
                deadline = job[{'PREPARING': 'preparation_deadline', 'RUNNING': 'execution_deadline', 'UPLOADING': 'upload_deadline'}[job['status']]]
                if deadline <= now or job['lease_until'] <= now:
                    error = {'PREPARING': 'PREPARATION_TIMEOUT', 'RUNNING': 'STAGE_TIMEOUT', 'UPLOADING': 'UPLOAD_TIMEOUT'}[job['status']]
                    if deadline > now:
                        error = 'LEASE_EXPIRED'
                    # An offline worker is not a model failure. Its outstanding
                    # process/copies must still be cleared before it claims again.
                    self.retry(conn, job, error, count=error != 'LEASE_EXPIRED')

    def retry(self, conn, job, error, *, count=True):
        failures = job['failures'] + int(count)
        state = 'FAILED_MANUAL' if failures >= 4 else 'RETRY_WAIT'
        delay = (1, 5, 30)[min(max(failures - 1, 0), 2)] if count else 0
        conn.execute('UPDATE callops.jobs SET status=%s,failures=%s,retry_at=%s,error_code=%s WHERE id=%s',
                     (state, failures, self.service.clock() + timedelta(minutes=delay), error, job['id']))
        conn.execute('UPDATE callops.attempts SET cleanup_requested=true WHERE id=%s', (job['active_attempt'],))

    def claim(self, worker):
        self.reap()
        with self.service.transaction() as conn:
            if conn.execute('SELECT 1 FROM callops.attempts WHERE worker_id=%s AND NOT cleaned LIMIT 1', (worker,)).fetchone():
                raise Conflict('WORKER_NOT_CLEAN')
            now = self.service.clock()
            rows = conn.execute('''SELECT j.* FROM callops.jobs j JOIN callops.recordings r ON r.id=j.recording_id
                WHERE j.status IN ('WAITING_FOR_WORKER','WAITING_FOR_FILE','RETRY_WAIT') AND j.retry_at<=%s
                AND r.classification_status='BUSINESS_APPROVED' AND r.deletion_state='NONE'
                ORDER BY j.retry_at,j.id FOR UPDATE OF j SKIP LOCKED''', (now,)).fetchall()
            for job in rows:
                recording = self.service.recording(conn, job['recording_id'])
                try:
                    path, sha, expires, artifact, version = self.input_info(conn, job, recording, now)
                    if not path.is_file():
                        raise Conflict('INPUT_MISSING')
                except Conflict as error:
                    status = 'CANCELLED' if str(error) == 'INPUT_EXPIRED' else 'FAILED_MANUAL'
                    conn.execute('UPDATE callops.jobs SET status=%s,error_code=%s WHERE id=%s', (status, str(error), job['id']))
                    continue
                attempt, token = uuid4(), uuid4()
                conn.execute('''INSERT INTO callops.attempts
                    (id,job_id,recording_id,worker_id,lease_token,lease_until,preparation_deadline)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                    (attempt, job['id'], job['recording_id'], worker, token, now + timedelta(minutes=5),
                     min(now + timedelta(minutes=10), expires)))
                conn.execute("UPDATE callops.jobs SET status='PREPARING',active_attempt=%s,error_code=NULL WHERE id=%s", (attempt, job['id']))
                return dict(job_id=str(job['id']), stage=job['stage'], lease_token=str(token),
                            copy_key=str(attempt), input_sha256=sha, input_artifact_id=str(artifact) if artifact else None,
                            input_artifact_version=version, schema_version=1,
                            call_started_at=recording['call_started_at'].isoformat() if recording['call_started_at'] else None,
                            call_time_trusted=recording['call_time_trusted'],
                            lease_until=(now + timedelta(minutes=5)).isoformat(),
                            preparation_deadline=min(now + timedelta(minutes=10), expires).isoformat())
        return None

    def lookup(self, conn, worker, job_id, token):
        job = conn.execute('SELECT * FROM callops.jobs WHERE id=%s FOR UPDATE', (identifier(job_id),)).fetchone()
        attempt = conn.execute('SELECT * FROM callops.attempts WHERE job_id=%s AND lease_token=%s AND worker_id=%s FOR UPDATE',
                               (identifier(job_id), identifier(token), worker)).fetchone()
        if not job or not attempt:
            raise Conflict('INVALID_LEASE')
        return job, attempt

    def active(self, conn, job, attempt):
        now = self.service.clock()
        if job['active_attempt'] != attempt['id'] or attempt['cleaned'] or attempt['cleanup_requested'] or attempt['lease_until'] <= now:
            raise Conflict('INVALID_LEASE')
        if job['status'] not in ('PREPARING', 'RUNNING', 'UPLOADING'):
            raise Conflict('INVALID_STATE')
        recording = self.service.recording(conn, job['recording_id'])
        if recording['classification_status'] != 'BUSINESS_APPROVED' or recording['deletion_state'] != 'NONE':
            raise Conflict('APPROVAL_REVOKED')
        info = self.input_info(conn, job, recording, now)
        deadline = attempt[{'PREPARING': 'preparation_deadline', 'RUNNING': 'execution_deadline', 'UPLOADING': 'upload_deadline'}[job['status']]]
        if deadline <= now:
            raise Conflict('DEADLINE_EXCEEDED')
        return info

    def input(self, worker, job_id, token):
        with self.service.transaction() as conn:
            job, attempt = self.lookup(conn, worker, job_id, token)
            path, sha, _, artifact, version = self.active(conn, job, attempt)
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                raise Conflict('INPUT_MISSING') from None
            if digest(data) != sha:
                raise Conflict('INPUT_CORRUPT')
            self.active(conn, job, attempt)
            return data, artifact, version

    def report(self, worker, job_id, token, action, body):
        self.reap()
        with self.service.transaction() as conn:
            job, attempt = self.lookup(conn, worker, job_id, token)
            if action == 'status':
                key = identifier(body.get('request_id'))
                if attempt['result_request_id'] not in (None, key):
                    raise Conflict('REQUEST_CONFLICT')
                if attempt['result_artifact_id'] is not None:
                    return {'accepted': True, 'artifact_id': str(attempt['result_artifact_id'])}
                self.active(conn, job, attempt)
                return {'accepted': False, 'status': job['status'], 'lease_until': attempt['lease_until'].isoformat(),
                        'execution_deadline': attempt['execution_deadline'].isoformat() if attempt['execution_deadline'] else None,
                        'upload_deadline': attempt['upload_deadline'].isoformat() if attempt['upload_deadline'] else None}
            _, _, expires, _, _ = self.active(conn, job, attempt)
            now = self.service.clock()
            if action == 'heartbeat':
                until = min(now + timedelta(minutes=5), expires)
                conn.execute('UPDATE callops.attempts SET lease_until=%s WHERE id=%s', (until, attempt['id']))
                return {'lease_until': until.isoformat()}
            if action == 'start':
                key = identifier(body.get('request_id'))
                if attempt['start_request_id'] is not None:
                    if attempt['start_request_id'] != key:
                        raise Conflict('REQUEST_CONFLICT')
                    return {'execution_deadline': attempt['execution_deadline'].isoformat()}
                if job['status'] != 'PREPARING':
                    raise Conflict('INVALID_STATE')
                deadline = min(now + timedelta(minutes=60 if job['stage'] == 'STT' else 30), expires)
                conn.execute('UPDATE callops.attempts SET start_request_id=%s,execution_deadline=%s WHERE id=%s', (key, deadline, attempt['id']))
                conn.execute("UPDATE callops.jobs SET status='RUNNING' WHERE id=%s", (job['id'],))
                return {'execution_deadline': deadline.isoformat()}
            if action == 'prepared':
                key, sha = identifier(body.get('request_id')), valid_hash(body.get('sha256'))
                size = body.get('size')
                if type(size) is not int or not 0 < size <= self.MAX_RESULT:
                    raise Conflict('INVALID_SIZE')
                try:
                    created = datetime.fromisoformat(body['spool_created_at'])
                    if created.tzinfo is None or created > now or created + timedelta(hours=24) <= now:
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    raise Conflict('INVALID_SPOOL_TIME') from None
                if attempt['result_request_id'] is not None:
                    if (attempt['result_request_id'], attempt['result_sha256'], attempt['result_size'], attempt['spool_created_at']) != (key, sha, size, created):
                        raise Conflict('REQUEST_CONFLICT')
                    return {'upload_deadline': attempt['upload_deadline'].isoformat()}
                if job['status'] != 'RUNNING':
                    raise Conflict('INVALID_STATE')
                if conn.execute('SELECT 1 FROM callops.attempts WHERE result_request_id=%s', (key,)).fetchone():
                    raise Conflict('REQUEST_CONFLICT')
                deadline = min(now + timedelta(minutes=30), expires, created + timedelta(hours=24))
                conn.execute('''UPDATE callops.attempts SET result_request_id=%s,result_sha256=%s,result_size=%s,
                    spool_created_at=%s,upload_deadline=%s WHERE id=%s''', (key, sha, size, created, deadline, attempt['id']))
                conn.execute("UPDATE callops.jobs SET status='UPLOADING' WHERE id=%s", (job['id'],))
                return {'upload_deadline': deadline.isoformat()}
            if action == 'failed':
                code = body.get('code')
                timeout = {'PREPARING': 'PREPARATION_TIMEOUT', 'RUNNING': 'STAGE_TIMEOUT',
                           'UPLOADING': 'UPLOAD_TIMEOUT'}[job['status']]
                if code in ('INPUT_MISSING', 'INPUT_CORRUPT') and job['stage'] == 'ANALYSIS':
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code=%s WHERE id=%s", (code, job['id']))
                    conn.execute('UPDATE callops.attempts SET cleanup_requested=true WHERE id=%s', (attempt['id'],))
                elif code in ('MODEL_ERROR', 'PREPARATION_ERROR', 'UPLOAD_ERROR', timeout):
                    self.retry(conn, job, code)
                else:
                    raise Conflict('INVALID_ERROR_CODE')
                return {'status': 'STOP_AND_CLEAN'}
            if action == 'waiting-file' and job['status'] == 'PREPARING':
                conn.execute("UPDATE callops.jobs SET status='WAITING_FOR_FILE',retry_at=%s WHERE id=%s", (now + timedelta(minutes=1), job['id']))
                conn.execute('UPDATE callops.attempts SET cleanup_requested=true WHERE id=%s', (attempt['id'],))
                return {'status': 'STOP_AND_CLEAN'}
            raise Conflict('INVALID_ACTION')

    def upload(self, worker, job_id, token, request_id, data):
        key, sha = identifier(request_id), digest(data)
        if not 0 < len(data) <= self.MAX_RESULT:
            raise Conflict('INVALID_SIZE')
        with self.service.transaction() as conn:
            job, attempt = self.lookup(conn, worker, job_id, token)
            if (attempt['result_request_id'], attempt['result_sha256'], attempt['result_size']) != (key, sha, len(data)):
                raise Conflict('REQUEST_CONFLICT')
            if attempt['result_artifact_id'] is not None:
                return {'accepted': True, 'artifact_id': str(attempt['result_artifact_id'])}
            _, input_sha, input_expires_at, artifact, version = self.active(conn, job, attempt)
            if job['status'] != 'UPLOADING':
                raise Conflict('INVALID_STATE')
            candidates, tasks = [], []
            try:
                recording = self.service.recording(conn, job['recording_id'])
                payload = validate_result(json.loads(data), job['stage'], recording['duration_seconds'])
                if not payload['synthetic'] and worker != self.real_worker:
                    raise ValueError('Real result intake disabled for worker')
                if (payload.get('job_id') != str(job['id'])
                        or payload.get('attempt_id') != str(attempt['id'])
                        or payload.get('input_artifact_id') != (str(artifact) if artifact else None)
                        or type(payload.get('input_artifact_version')) is not int
                        or payload.get('input_artifact_version') != version
                        or payload.get('input_sha256') != input_sha):
                    raise ValueError
                if job['stage'] == 'ANALYSIS' and payload['schema_version'] == 2:
                    source = conn.execute('SELECT * FROM callops.artifacts WHERE id=%s AND recording_id=%s',
                                          (artifact, recording['id'])).fetchone()
                    if source is None:
                        raise ValueError('Invalid transcript source')
                    transcript = self.service.read_transcript_artifact(source, recording['duration_seconds'])
                    candidates = validated_events(payload['events'], transcript)
                    if 'tasks' in payload:
                        tasks = validated_tasks(payload['tasks'], transcript)
                    evidence = ([payload['summary']] if 'summary' in payload else [])
                    evidence += payload.get('needs_review', []) + payload.get('contract_mentions', [])
                    if any(max(item['segment_ids']) > len(transcript['segments']) for item in evidence):
                        raise ValueError('Invalid analysis evidence')
            except Conflict:
                raise
            except (ValueError, OverflowError, RecursionError):
                raise Conflict('INVALID_RESULT') from None
            artifact_id = uuid4()
            part = self.service.safe_path('artifacts', str(artifact_id) + '.part')
            target = self.service.safe_path('artifacts', str(artifact_id))
            with part.open('xb') as outgoing:
                outgoing.write(data)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            os.replace(part, target)
            conn.execute('''INSERT INTO callops.artifacts (id,recording_id,version,sha256,size_bytes,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s)''', (artifact_id, job['recording_id'], job['pipeline_version'], sha, len(data), self.service.clock() + timedelta(days=90)))
            if job['stage'] == 'STT' and 'NO_SPEECH' not in payload.get('warnings', ()):
                conn.execute('''INSERT INTO callops.jobs (id,recording_id,stage,input_artifact_id,pipeline_version,retry_at)
                    VALUES (%s,%s,'ANALYSIS',%s,%s,%s)''', (uuid4(), job['recording_id'], artifact_id, job['pipeline_version'], self.service.clock()))
            if candidates:
                store_candidates(conn, job, input_expires_at, candidates)
            if tasks:
                store_task_candidates(conn, job, input_expires_at, tasks)
            try:
                self.active(conn, job, attempt)
            except Conflict:
                # This transaction has not published the result and will roll back.
                target.unlink(missing_ok=True)
                raise
            conn.execute('UPDATE callops.attempts SET result_artifact_id=%s,cleanup_requested=true WHERE id=%s', (artifact_id, attempt['id']))
            conn.execute("UPDATE callops.jobs SET status='SUCCEEDED' WHERE id=%s", (job['id'],))
            return {'accepted': True, 'artifact_id': str(artifact_id)}

    def cleanup(self, worker, body=None):
        self.reap()
        with self.service.transaction() as conn:
            if body is not None:
                if body.get('stopped') is not True or body.get('removed') is not True or type(body.get('version')) is not int:
                    raise Conflict('CLEANUP_UNCONFIRMED')
                row = conn.execute('''UPDATE callops.attempts SET cleaned=true WHERE id=%s AND worker_id=%s
                    AND cleanup_requested AND cleanup_version=%s RETURNING id''',
                    (identifier(body.get('copy_key')), worker, body['version'])).fetchone()
                if not row:
                    raise Conflict('INVALID_CLEANUP')
            rows = conn.execute('SELECT id,cleanup_version FROM callops.attempts WHERE worker_id=%s AND cleanup_requested AND NOT cleaned', (worker,)).fetchall()
        self.service.maintain()
        return [{'copy_key': str(row['id']), 'version': row['cleanup_version']} for row in rows]
