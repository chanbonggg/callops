"""Pull one approved job at a time through the CallOps worker API."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import signal
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from app.worker_io import Copies, MAX_AUDIO, MAX_RESULT, WorkerError, copy_key, json_bytes, open_private, verify_job


INVALID_ATTEMPT = {'INVALID_LEASE', 'APPROVAL_REVOKED', 'INPUT_EXPIRED', 'DEADLINE_EXCEEDED'}
INPUT_FAILURES = {'INPUT_MISSING', 'INPUT_CORRUPT'}
PHASE_TIMEOUTS = {'PREPARATION_TIMEOUT', 'STAGE_TIMEOUT', 'UPLOAD_TIMEOUT'}
REPORTABLE_FAILURES = PHASE_TIMEOUTS | INPUT_FAILURES | {'PREPARATION_ERROR', 'MODEL_ERROR', 'UPLOAD_ERROR'}
RETRYABLE_ERRORS = REPORTABLE_FAILURES | INVALID_ATTEMPT | {
    'INVALID_RESULT', 'WORKER_NOT_CLEAN', 'WAITING_FOR_FILE', 'LEASE_ENDED', 'SERVER_UNAVAILABLE',
}


class Offline(WorkerError):
    pass


class Rejected(WorkerError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Reply:
    data: object
    server_time: datetime
    sent_at: float

    def deadline(self, field):
        when = datetime.fromisoformat(self.data[field])
        # Subtract elapsed request time from the server's remaining duration.
        return self.sent_at + (when - self.server_time).total_seconds()


class WorkerAPI:
    def __init__(self, stream, token, *, transport_errors, monotonic=time.monotonic):
        self.stream = stream
        self.token = token
        self.transport_errors = transport_errors
        self.monotonic = monotonic

    @contextmanager
    def request(self, method, action, *, job=None, report=None, **body):
        headers = {'Authorization': 'Bearer ' + self.token}
        url = '/worker/' + action
        if job is not None:
            url = f"/worker/jobs/{job['job_id']}/{action}"
            headers['X-Lease-Token'] = job['lease_token']
        if report is not None:
            headers.update({'X-Result-Request-ID': report['request_id'], 'Content-Type': 'application/json'})
        try:
            with self.stream(method, url, headers=headers, **body) as response:
                if response.status_code in (502, 503, 504):
                    raise Offline('SERVER_UNAVAILABLE')
                if response.status_code != 200:
                    code = 'REQUEST_REJECTED'
                    if response.status_code == 409:
                        response.read()
                        value = response.json().get('error', '')
                        if isinstance(value, str) and re.fullmatch('[A-Z_]{1,64}', value):
                            code = value
                    raise Rejected(code)
                yield response
        except self.transport_errors:
            raise Offline('SERVER_UNAVAILABLE') from None

    def json(self, method, action, *, job=None, report=None, **body):
        sent = self.monotonic()
        with self.request(method, action, job=job, report=report, **body) as response:
            response.read()
            return Reply(response.json(), datetime.fromisoformat(response.headers['X-CallOps-Server-Time']), sent)

    def post(self, action, job=None, **body):
        return self.json('POST', action, job=job, **body)

    def download(self, job, target, check):
        part = target.with_suffix('.part')
        expected_id = job['input_artifact_id'] or ''
        limit = MAX_AUDIO if job['stage'] == 'STT' else MAX_RESULT
        with self.request('GET', 'input', job=job) as response:
            if (response.headers.get('X-Input-Artifact-ID') != expected_id
                    or response.headers.get('X-Input-Artifact-Version') != str(job['input_artifact_version'])):
                raise WorkerError('INPUT_CORRUPT')
            digest, size = hashlib.sha256(), 0
            with open_private(part) as output:
                try:
                    for block in response.iter_bytes(chunk_size=128 * 1024):
                        check()
                        size += len(block)
                        if size > limit:
                            raise WorkerError('INPUT_CORRUPT')
                        output.write(block)
                        digest.update(block)
                    if not size or digest.hexdigest() != job['input_sha256']:
                        raise WorkerError('INPUT_CORRUPT')
                    output.flush()
                    os.fsync(output.fileno())
                    check()
                    os.replace(part, target)
                finally:
                    part.unlink(missing_ok=True)


class Lease:
    def __init__(self, api, job, initial, heartbeat_seconds):
        self.api, self.job = api, job
        self.until = initial.deadline('lease_until')
        self.server_time = initial.server_time
        self.interval = heartbeat_seconds
        self.finished = threading.Event()
        self.invalid = threading.Event()
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.finished.set()
        self.thread.join()

    def heartbeat(self):
        while not self.finished.is_set() and self.api.monotonic() < self.until:
            try:
                reply = self.api.post('heartbeat', self.job, json={})
                self.until = reply.deadline('lease_until')
                self.server_time = reply.server_time
            except Offline:
                pass
            except (WorkerError, ValueError, KeyError, TypeError):
                self.invalid.set()
                return
            self.finished.wait(self.interval)

    def check(self, deadline, timeout):
        if self.api.monotonic() >= deadline:
            raise WorkerError(timeout)
        if self.invalid.is_set() or self.api.monotonic() >= self.until:
            raise WorkerError('LEASE_ENDED')


class Worker:
    def __init__(self, api, copies, run_model, *, ready=None, heartbeat_seconds=30, wall_time=time.time):
        self.api, self.copies, self.run_model = api, copies, run_model
        self.ready = ready
        self.heartbeat_seconds = heartbeat_seconds
        self.wall_time = wall_time

    def clean(self):
        for item in self.api.post('cleanup', json={}).data:
            self.copies.remove(item['copy_key'])
            self.api.post('cleanup', json={**item, 'stopped': True, 'removed': True})

    def accept(self, job, ack):
        if ack.get('accepted') is not True:
            raise WorkerError('INVALID_ACK')
        copy_key(ack['artifact_id'])
        self.copies.remove(job['copy_key'])
        self.clean()
        return ack

    def send(self, job, report, data, lease, state, *, prepared=False):
        if not prepared:
            lease.check(state.deadline('execution_deadline'), 'STAGE_TIMEOUT')
            state = self.api.post('prepared', job, json=report)
        lease.check(state.deadline('upload_deadline'), 'UPLOAD_TIMEOUT')
        return self.api.post('result', job, report=report, content=data).data

    def resume(self):
        for job, report, data in self.copies.pending():
            try:
                reply = self.api.post('status', job, json={'request_id': report['request_id']})
                if reply.data['accepted']:
                    return self.accept(job, reply.data)
                with Lease(self.api, job, reply, self.heartbeat_seconds) as lease:
                    ack = self.send(job, report, data, lease, reply, prepared=reply.data['status'] == 'UPLOADING')
                return self.accept(job, ack)
            except Rejected as error:
                if error.code not in INVALID_ATTEMPT | INPUT_FAILURES | {'INVALID_RESULT'}:
                    raise
                self.finish_failed(job, error)
            except WorkerError as error:
                if str(error) not in {'STAGE_TIMEOUT', 'UPLOAD_TIMEOUT', 'LEASE_ENDED'}:
                    raise
                self.finish_failed(job, error)
        return None

    def finish_failed(self, job, error):
        code = str(error)
        if code == 'ENGINE_STOP_UNCONFIRMED':
            # Keep the guard and copies until an operator can prove the engine stopped.
            return
        try:
            if code == 'WAITING_FOR_FILE':
                self.api.post('waiting-file', job, json={})
            elif code in REPORTABLE_FAILURES | {'INVALID_RESULT'}:
                if code in INPUT_FAILURES and job['stage'] == 'STT':
                    code = 'PREPARATION_ERROR'
                if code == 'INVALID_RESULT':
                    code = 'UPLOAD_ERROR'
                self.api.post('failed', job, json={'code': code})
        except Rejected as rejected:
            if rejected.code not in INVALID_ATTEMPT | {'INVALID_ERROR_CODE'}:
                raise
        self.clean()

    def process(self, claim):
        job = claim.data
        verify_job(job)
        with Lease(self.api, job, claim, self.heartbeat_seconds) as lease:
            directory = self.copies.work_dir(job['copy_key'])
            prepare_until = claim.deadline('preparation_deadline')

            def check_preparation():
                lease.check(prepare_until, 'PREPARATION_TIMEOUT')

            try:
                self.api.download(job, directory / 'input', check_preparation)
            except Offline:
                if job['stage'] == 'STT':
                    raise WorkerError('WAITING_FOR_FILE') from None
                check_preparation()
                self.api.download(job, directory / 'input', check_preparation)
            except Rejected as error:
                if job['stage'] == 'STT' and error.code == 'INPUT_MISSING':
                    raise WorkerError('WAITING_FOR_FILE') from None
                raise
            except OSError:
                raise WorkerError('PREPARATION_ERROR') from None

            start = {'request_id': str(uuid4())}
            check_preparation()
            try:
                started = self.api.post('start', job, json=start)
            except Offline:
                check_preparation()
                started = self.api.post('start', job, json=start)

            execution_until = started.deadline('execution_deadline')

            def check_execution():
                lease.check(execution_until, 'STAGE_TIMEOUT')

            check_execution()
            body = self.run_model(job, directory, check_execution)
            check_execution()
            binding = {name: job[name] for name in ('job_id', 'input_artifact_id', 'input_artifact_version', 'input_sha256')}
            data = json_bytes(body | binding | {'schema_version': 2, 'synthetic': False, 'attempt_id': job['copy_key']})
            if len(data) > MAX_RESULT:
                raise WorkerError('MODEL_ERROR')
            report = {'request_id': str(uuid4()), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                      'spool_created_at': max(started.server_time, lease.server_time).isoformat()}
            try:
                self.copies.save(job['copy_key'], job, report, data)
            except OSError:
                raise WorkerError('UPLOAD_ERROR') from None
            return self.send(job, report, data, lease, started)

    def once(self):
        with self.copies.locked():
            self.copies.expire(self.wall_time())
            recovered = self.resume()
            if recovered is not None:
                return recovered
            self.clean()
            ready = self.api.json('GET', 'readiness')
            if ready.data.get('real_results') is not True:
                raise WorkerError('REAL_RESULTS_DISABLED')
            if self.ready is not None:
                self.ready()
            claim = self.api.post('claim')
            if claim.data is None:
                return None
            try:
                ack = self.process(claim)
            except Offline:
                # A prepared result survives transport failures and keeps its original report.
                raise
            except WorkerError as error:
                self.finish_failed(claim.data, error)
                raise
            return self.accept(claim.data, ack)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True, help='CallOps base URL')
    parser.add_argument('--ca-file', type=Path, help='Development TLS certificate to trust')
    parser.add_argument('--root', required=True, type=Path, help='Private worker directory outside the repository')
    parser.add_argument('--whisper-model', required=True, type=Path)
    parser.add_argument('--ollama-models', required=True, type=Path)
    parser.add_argument('--two-speaker-models', nargs=2, type=Path, metavar=('SEGMENTATION', 'EMBEDDING'))
    parser.add_argument('--once', action='store_true', help='Process or reconcile at most one attempt')
    args = parser.parse_args(argv)
    try:
        import httpx
        from app.worker_engine import StageProcess

        url = urlsplit(args.server)
        if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1'))
                or not url.hostname or url.username is not None or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('Invalid server URL')
        token = os.environ.get('CALLOPS_WORKER_TOKEN', '')
        if len(token) < 32 or token.startswith('REPLACE_'):
            raise ValueError('Worker token required')
        verify = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else True
        copies = Copies(args.root)
        engine = StageProcess(copies.engine_guard, args.whisper_model, args.ollama_models, args.two_speaker_models)
        with httpx.Client(base_url=args.server, verify=verify, timeout=10.0, trust_env=False,
                          follow_redirects=False) as client:
            api = WorkerAPI(client.stream, token, transport_errors=(httpx.TransportError,))
            worker = Worker(api, copies, engine.run, ready=engine.ready)
            while True:
                ack = None
                try:
                    ack = worker.once()
                    if ack is not None:
                        print('RESULT_ACCEPTED', flush=True)
                except WorkerError as error:
                    if args.once or str(error) not in RETRYABLE_ERRORS:
                        raise
                    print(str(error), file=sys.stderr, flush=True)
                if args.once:
                    break
                if ack is None:
                    time.sleep(2)
    except KeyboardInterrupt:
        return 130
    except (WorkerError, ImportError, OSError, KeyError, TypeError, ValueError) as error:
        code = str(error) if isinstance(error, WorkerError) and re.fullmatch('[A-Z_]{1,64}', str(error)) else type(error).__name__
        print(f'FAIL: {code}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main())
