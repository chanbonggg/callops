"""Private work files and durable spool snapshots for one worker."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from uuid import UUID


MAX_RESULT = 8 * 1024 * 1024
MAX_AUDIO = 500 * 1024 * 1024
MAX_RAG_INPUT = 80 * 1024
MAX_RAG_RESULT = 1024 * 1024
RAG_CONTRACT_VERSION = 1
RAG_STAGES = ('RAG_EMBED', 'RAG_ANSWER')
RAG_CONTEXT_TOO_LONG = 'RAG_CONTEXT_TOO_LONG'
COPY_TTL = 24 * 60 * 60
REPO_ROOT = Path(__file__).resolve().parents[2]


class WorkerError(RuntimeError):
    pass


def copy_key(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('INVALID_COPY_KEY')
    return value


def verify_job(job):
    for field in ('job_id', 'copy_key', 'lease_token'):
        copy_key(job[field])
    stage = job['stage']
    if stage in RAG_STAGES:
        schema = job.get('schema_version')
        if type(schema) is not int or schema != RAG_CONTRACT_VERSION:
            raise ValueError('INVALID_JOB_SCHEMA')
        copy_key(job['input_snapshot_id'])
        version = job['input_snapshot_version']
    else:
        if stage == 'ANALYSIS':
            copy_key(job['input_artifact_id'])
        elif stage != 'STT' or job['input_artifact_id'] is not None:
            raise ValueError('INVALID_JOB_STAGE')
        version = job['input_artifact_version']
    sha = job['input_sha256']
    if (not isinstance(sha, str) or re.fullmatch('[0-9a-f]{64}', sha) is None
            or type(version) is not int or version < 1):
        raise ValueError('INVALID_JOB_INPUT')


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def result_limit(stage):
    return MAX_RAG_RESULT if stage in RAG_STAGES else MAX_RESULT


def result_bytes(job, output):
    binding = {'job_id': job['job_id'], 'attempt_id': job['copy_key'], 'input_sha256': job['input_sha256']}
    if job['stage'] in RAG_STAGES:
        packet = binding | {'schema_version': RAG_CONTRACT_VERSION, 'output': output,
                            'input_snapshot_id': job['input_snapshot_id'],
                            'input_snapshot_version': job['input_snapshot_version']}
    else:
        packet = output | binding | {'schema_version': 2, 'synthetic': False,
                                     'input_artifact_id': job['input_artifact_id'],
                                     'input_artifact_version': job['input_artifact_version']}
    data = json_bytes(packet)
    if len(data) > result_limit(job['stage']):
        raise WorkerError('MODEL_ERROR')
    return data


def open_private(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600), 'wb')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path, data):
    part = path.with_suffix('.part')
    with open_private(part) as stream:
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(part, path)
            sync_directory(path.parent)
        finally:
            part.unlink(missing_ok=True)


class Copies:
    def __init__(self, root):
        root = root.expanduser().absolute()
        if root.resolve() != root or root.is_relative_to(REPO_ROOT) or REPO_ROOT.is_relative_to(root):
            raise ValueError('INVALID_WORKER_ROOT')
        self.root = root
        self.work = root / 'work'
        self.spool = root / 'spool'
        self.engine_guard = root / 'engine-active'
        for directory in (root, self.work, self.spool):
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('WORKER_DIRECTORIES_MUST_BE_PRIVATE')

    @contextmanager
    def locked(self):
        fd = os.open(self.root / 'worker.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkerError('WORKER_ALREADY_RUNNING') from None
            if self.engine_guard.exists() or self.engine_guard.is_symlink():
                raise WorkerError('ENGINE_STOP_UNCONFIRMED')
            self.engine_guard.with_suffix('.part').unlink(missing_ok=True)
            yield
        finally:
            os.close(fd)

    def work_dir(self, key):
        directory = self.work / copy_key(key)
        directory.mkdir(mode=0o700)
        return directory

    def save(self, key, job, report, payload):
        path = self.spool / (copy_key(key) + '.json')
        publish(path, json_bytes({'job': job, 'report': report, 'payload': payload.decode()}))

    def pending(self):
        for path in sorted(self.spool.glob('*.json')):
            key = copy_key(path.stem)
            if path.is_symlink() or not path.is_file():
                raise WorkerError('UNSAFE_SPOOL')
            with path.open('rb') as stream:
                snapshot = json.loads(stream.read(2 * MAX_RESULT + 8192))
            job, report = snapshot['job'], snapshot['report']
            verify_job(job)
            copy_key(report['request_id'])
            data = snapshot['payload'].encode()
            if (job['copy_key'] != key or not 0 < len(data) <= result_limit(job['stage']) or report['size'] != len(data)
                    or hashlib.sha256(data).hexdigest() != report['sha256']):
                raise WorkerError('SPOOL_MISMATCH')
            yield job, report, data

    def remove(self, key):
        key = copy_key(key)
        directory = self.work / key
        if directory.is_symlink():
            raise WorkerError('UNSAFE_WORK_DIRECTORY')
        if directory.exists():
            shutil.rmtree(directory)
            sync_directory(self.work)
        for suffix in ('.json', '.part'):
            (self.spool / (key + suffix)).unlink(missing_ok=True)
        sync_directory(self.spool)

    def expire(self, now):
        for parent in (self.work, self.spool):
            for path in list(parent.iterdir()):
                try:
                    key = copy_key(path.name if parent == self.work else path.stem)
                except ValueError:
                    continue
                if path.is_symlink():
                    raise WorkerError('UNSAFE_COPY')
                if path.exists() and path.stat().st_mtime <= now - COPY_TTL:
                    self.remove(key)
