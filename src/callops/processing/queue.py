"""PostgreSQL queue, lease and artifact storage for processing workers."""

import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

from ..calls.service import audio_expired, audio_expires_at
from ..common import Conflict, digest, identifier, valid_hash
from ..schedule.service import store_candidates, validated_events
from ..task_candidates import store_task_candidates, validated_tasks
from .rag import MAX_INPUT_BYTES, MAX_RESULT_BYTES, RAG_STAGES, json_bytes
from .transcript import validate_result


class Queue:
    MAX_RESULT = 8 * 1024 * 1024

    def __init__(self, service, *, real_worker='', rag=None):
        self.service = service
        self.real_worker = real_worker
        self.rag = rag

    def input_info(self, conn, job, recording, now):
        if job['stage'] in RAG_STAGES:
            if self.rag is None:
                raise Conflict('INVALID_LEASE')
            return self.rag.input_info(conn, job, now)
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

    def retry(self, conn, job, error, *, count=True, terminal=False):
        failures = job['failures'] + int(count)
        if job['stage'] in RAG_STAGES:
            state = 'FAILED_MANUAL' if terminal or job['rag_tries'] >= 2 else 'RETRY_WAIT'
            conn.execute('''UPDATE callops.jobs SET status=%s,failures=%s,retry_at=%s,error_code=%s,
                           rag_input=CASE WHEN %s THEN NULL ELSE rag_input END WHERE id=%s''',
                         (state, failures, self.service.clock(), error, state == 'FAILED_MANUAL', job['id']))
            conn.execute('UPDATE callops.attempts SET cleanup_requested=true WHERE id=%s', (job['active_attempt'],))
            if state == 'FAILED_MANUAL':
                self.fail_rag_scope(conn, job, error)
            return
        state = 'FAILED_MANUAL' if failures >= 4 else 'RETRY_WAIT'
        delay = (1, 5, 30)[min(max(failures - 1, 0), 2)] if count else 0
        conn.execute('UPDATE callops.jobs SET status=%s,failures=%s,retry_at=%s,error_code=%s WHERE id=%s',
                     (state, failures, self.service.clock() + timedelta(minutes=delay), error, job['id']))
        conn.execute('UPDATE callops.attempts SET cleanup_requested=true WHERE id=%s', (job['active_attempt'],))

    @staticmethod
    def fail_rag_scope(conn, job, error):
        if job['rag_question_id'] is not None:
            conn.execute('UPDATE callops.rag_questions SET error_code=%s WHERE id=%s',
                         (error, job['rag_question_id']))
        else:
            conn.execute('DELETE FROM callops.rag_chunks WHERE artifact_id=%s', (job['input_artifact_id'],))

    def claim(self, worker, *, rag_capable=False):
        self.reap()
        rag_allowed = self.rag is not None and rag_capable and worker == self.real_worker
        with self.service.transaction() as conn:
            if conn.execute('SELECT 1 FROM callops.attempts WHERE worker_id=%s AND NOT cleaned LIMIT 1', (worker,)).fetchone():
                raise Conflict('WORKER_NOT_CLEAN')
            now = self.service.clock()
            preferred, streak = set(), 0
            if rag_allowed:
                preferred = self.rag.prepare_questions(conn, now)
                self.rag.enqueue_index(conn, now, preferred)
                schedule = conn.execute('SELECT question_streak FROM callops.rag_schedule WHERE worker_id=%s',
                                        (worker,)).fetchone()
                streak = schedule['question_streak'] if schedule else 0
            rows = conn.execute("""SELECT j.* FROM callops.jobs j LEFT JOIN callops.recordings r ON r.id=j.recording_id
                WHERE j.status IN ('WAITING_FOR_WORKER','WAITING_FOR_FILE','RETRY_WAIT') AND j.retry_at<=%s
                AND (j.recording_id IS NULL OR
                    (r.classification_status='BUSINESS_APPROVED' AND r.deletion_state='NONE'))
                AND (j.stage IN ('STT','ANALYSIS') OR %s)
                ORDER BY j.retry_at,j.id FOR UPDATE OF j SKIP LOCKED""", (now, rag_allowed)).fetchall()
            if rag_allowed:
                rows.sort(key=lambda job: self.priority(job, preferred, streak))
            for job in rows:
                result = self.claim_job(conn, worker, job, now)
                if result is not None:
                    category = self.priority(job, preferred, streak)
                    new_question = job['rag_question_id'] is not None and not job['rag_tries'] and job['stage'] == 'RAG_EMBED'
                    if rag_allowed and (new_question or category == 2):
                        conn.execute('''INSERT INTO callops.rag_schedule (worker_id,question_streak) VALUES (%s,%s)
                            ON CONFLICT (worker_id) DO UPDATE SET question_streak=excluded.question_streak''',
                                     (worker, min(streak + 1, 2) if new_question else 0))
                    return result
        return None

    @staticmethod
    def priority(job, preferred, streak):
        if job['rag_question_id'] is not None:
            if job['stage'] == 'RAG_ANSWER' or job['rag_tries']:
                return 0
            return 3 if streak >= 2 else 1
        return 1 if job['input_artifact_id'] in preferred and job['rag_offset'] is not None else 2

    def claim_job(self, conn, worker, job, now):
        recording = self.service.recording(conn, job['recording_id']) if job['recording_id'] else None
        rag_stage = job['stage'] in RAG_STAGES
        try:
            path, sha, expires, artifact, version = self.input_info(conn, job, recording, now)
            if rag_stage:
                if job['rag_input'] is None:
                    payload = self.rag.payload(conn, job, now)
                    if job['rag_offset'] is not None and not payload['texts']:
                        conn.execute('UPDATE callops.artifacts SET rag_indexed=true WHERE id=%s',
                                     (job['input_artifact_id'],))
                        conn.execute("UPDATE callops.jobs SET status='SUCCEEDED' WHERE id=%s", (job['id'],))
                        return None
                    data = json_bytes(payload)
                    if len(data) > MAX_INPUT_BYTES:
                        raise Conflict('RAG_CONTEXT_TOO_LONG')
                    sha = digest(data)
                    conn.execute('UPDATE callops.jobs SET rag_input_hash=%s,rag_input=%s WHERE id=%s',
                                 (sha, data, job['id']))
                conn.execute('UPDATE callops.jobs SET rag_tries=rag_tries+1 WHERE id=%s', (job['id'],))
            elif not path.is_file():
                raise Conflict('INPUT_MISSING')
        except Conflict as error:
            status = 'CANCELLED' if str(error) in ('INPUT_EXPIRED', 'APPROVAL_REVOKED') else 'FAILED_MANUAL'
            conn.execute('UPDATE callops.jobs SET status=%s,error_code=%s,rag_input=NULL WHERE id=%s',
                         (status, str(error), job['id']))
            if rag_stage:
                self.fail_rag_scope(conn, job, str(error))
            return None
        attempt, token = uuid4(), uuid4()
        lease_until = min(now + timedelta(minutes=5), expires) if rag_stage else now + timedelta(minutes=5)
        prepare_until = min(now + timedelta(minutes=1 if rag_stage else 10), expires)
        conn.execute('''INSERT INTO callops.attempts
            (id,job_id,recording_id,rag_question_id,worker_id,lease_token,lease_until,preparation_deadline)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
            (attempt, job['id'], job['recording_id'], job['rag_question_id'], worker, token,
             lease_until, prepare_until))
        conn.execute("UPDATE callops.jobs SET status='PREPARING',active_attempt=%s,error_code=NULL WHERE id=%s", (attempt, job['id']))
        when = recording['call_started_at'] if recording else None
        envelope = dict(job_id=str(job['id']), stage=job['stage'], lease_token=str(token),
                        copy_key=str(attempt), input_sha256=sha, schema_version=1,
                        lease_until=lease_until.isoformat(), preparation_deadline=prepare_until.isoformat())
        if rag_stage:
            return envelope | {'input_snapshot_id': str(artifact), 'input_snapshot_version': version}
        return envelope | dict(input_artifact_id=str(artifact) if artifact else None,
                               input_artifact_version=version,
                               call_started_at=when.isoformat() if when else None,
                               call_time_trusted=recording['call_time_trusted'])

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
        recording = self.service.recording(conn, job['recording_id']) if job['recording_id'] else None
        if recording and (recording['classification_status'] != 'BUSINESS_APPROVED' or recording['deletion_state'] != 'NONE'):
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
                rag_stage = job['stage'] in RAG_STAGES
                data = job['rag_input'] if rag_stage else path.read_bytes()
            except FileNotFoundError:
                raise Conflict('INPUT_MISSING') from None
            if digest(data) != sha:
                raise Conflict('INPUT_CORRUPT')
            self.active(conn, job, attempt)
            return data, artifact, version, rag_stage

    @staticmethod
    def acceptance(job, attempt):
        if attempt['rag_result_id'] is not None:
            return {'accepted': True, 'result_id': str(attempt['rag_result_id'])}
        if attempt['result_artifact_id'] is not None:
            return {'accepted': True, 'artifact_id': str(attempt['result_artifact_id'])}
        return None

    def report(self, worker, job_id, token, action, body):
        self.reap()
        with self.service.transaction() as conn:
            job, attempt = self.lookup(conn, worker, job_id, token)
            if action == 'status':
                key = identifier(body.get('request_id'))
                if attempt['result_request_id'] not in (None, key):
                    raise Conflict('REQUEST_CONFLICT')
                ack = self.acceptance(job, attempt)
                if ack is not None:
                    return ack
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
                minutes = {'STT': 60, 'ANALYSIS': 30}.get(job['stage'], 2)
                deadline = min(now + timedelta(minutes=minutes), expires)
                conn.execute('UPDATE callops.attempts SET start_request_id=%s,execution_deadline=%s WHERE id=%s', (key, deadline, attempt['id']))
                conn.execute("UPDATE callops.jobs SET status='RUNNING' WHERE id=%s", (job['id'],))
                return {'execution_deadline': deadline.isoformat()}
            if action == 'prepared':
                key, sha = identifier(body.get('request_id')), valid_hash(body.get('sha256'))
                size = body.get('size')
                limit = MAX_RESULT_BYTES if job['stage'] in RAG_STAGES else self.MAX_RESULT
                if type(size) is not int or not 0 < size <= limit:
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
                minutes = 2 if job['stage'] in RAG_STAGES else 30
                deadline = min(now + timedelta(minutes=minutes), expires, created + timedelta(hours=24))
                conn.execute('''UPDATE callops.attempts SET result_request_id=%s,result_sha256=%s,result_size=%s,
                    spool_created_at=%s,upload_deadline=%s WHERE id=%s''', (key, sha, size, created, deadline, attempt['id']))
                conn.execute("UPDATE callops.jobs SET status='UPLOADING' WHERE id=%s", (job['id'],))
                return {'upload_deadline': deadline.isoformat()}
            if action == 'failed':
                code = body.get('code')
                timeout = {'PREPARING': 'PREPARATION_TIMEOUT', 'RUNNING': 'STAGE_TIMEOUT',
                           'UPLOADING': 'UPLOAD_TIMEOUT'}[job['status']]
                if job['stage'] in RAG_STAGES and code in ('INPUT_MISSING', 'INPUT_CORRUPT', 'RAG_CONTEXT_TOO_LONG'):
                    self.retry(conn, job, code, terminal=True)
                elif code in ('INPUT_MISSING', 'INPUT_CORRUPT') and job['stage'] == 'ANALYSIS':
                    conn.execute("UPDATE callops.jobs SET status='FAILED_MANUAL',error_code=%s WHERE id=%s", (code, job['id']))
                    conn.execute('UPDATE callops.attempts SET cleanup_requested=true WHERE id=%s', (attempt['id'],))
                elif code in ('MODEL_ERROR', 'PREPARATION_ERROR', 'UPLOAD_ERROR', timeout):
                    self.retry(conn, job, code)
                else:
                    raise Conflict('INVALID_ERROR_CODE')
                return {'status': 'STOP_AND_CLEAN'}
            if action == 'waiting-file' and job['status'] == 'PREPARING' and job['stage'] not in RAG_STAGES:
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
            ack = self.acceptance(job, attempt)
            if ack is not None:
                return ack
            _, input_sha, input_expires_at, artifact, version = self.active(conn, job, attempt)
            if job['status'] != 'UPLOADING':
                raise Conflict('INVALID_STATE')
            if job['stage'] in RAG_STAGES:
                return self.upload_rag(conn, worker, job, attempt, data, input_sha, artifact, version)
            candidates, tasks = [], []
            try:
                payload = json.loads(data)
                if (payload.get('job_id') != str(job['id'])
                        or payload.get('attempt_id') != str(attempt['id'])
                        or payload.get('input_artifact_id') != (str(artifact) if artifact else None)
                        or type(payload.get('input_artifact_version')) is not int
                        or payload.get('input_artifact_version') != version
                        or payload.get('input_sha256') != input_sha):
                    raise ValueError
                recording = self.service.recording(conn, job['recording_id'])
                payload = validate_result(payload, job['stage'], recording['duration_seconds'])
                if not payload['synthetic'] and worker != self.real_worker:
                    raise ValueError('Real result intake disabled for worker')
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
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
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
            if job['stage'] == 'STT':
                previous = conn.execute('''SELECT a.result_artifact_id AS id FROM callops.jobs j
                    JOIN callops.attempts a ON a.job_id=j.id
                    WHERE j.recording_id=%s AND j.stage='STT' AND j.pipeline_version<%s
                        AND a.result_artifact_id IS NOT NULL''',
                    (job['recording_id'], job['pipeline_version'])).fetchall()
                if previous:
                    self.service.prune_rag(conn, self.service.clock(), [row['id'] for row in previous])
            return {'accepted': True, 'artifact_id': str(artifact_id)}

    def upload_rag(self, conn, worker, job, attempt, data, sha, snapshot, version):
        try:
            with conn.transaction():
                packet = json.loads(data)
                binding = {'job_id': str(job['id']), 'attempt_id': str(attempt['id']),
                           'input_snapshot_id': str(snapshot), 'input_snapshot_version': version,
                           'input_sha256': sha, 'schema_version': 1}
                if (worker != self.real_worker or len(data) > MAX_RESULT_BYTES
                        or not isinstance(packet, dict) or set(packet) != binding.keys() | {'output'}
                        or any(type(packet[key]) is not type(value) or packet[key] != value for key, value in binding.items())):
                    raise ValueError('Invalid RAG result binding')
                self.rag.finish(conn, job, packet['output'], self.service.clock())
                self.active(conn, job, attempt)
        except (TypeError, ValueError, OverflowError, RecursionError):
            self.retry(conn, job, 'INVALID_RESULT', terminal=True)
            return {'error': 'INVALID_RESULT'}
        except Conflict as error:
            if str(error) not in ('INPUT_CORRUPT', 'INPUT_MISSING', 'APPROVAL_REVOKED', 'INPUT_EXPIRED'):
                raise
            self.retry(conn, job, str(error), terminal=True)
            return {'error': str(error)}
        result_id = uuid4()
        conn.execute('UPDATE callops.attempts SET rag_result_id=%s,cleanup_requested=true WHERE id=%s',
                     (result_id, attempt['id']))
        conn.execute("UPDATE callops.jobs SET status='SUCCEEDED',rag_input=NULL WHERE id=%s", (job['id'],))
        return {'accepted': True, 'result_id': str(result_id)}

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
