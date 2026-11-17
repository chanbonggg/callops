"""Deterministic P3 worker using the real HTTP routes, never a model or Calendar.

The transport takes (method, path, headers, json/content) and returns a response.
Tests supply an ASGI client so the actual API/auth/DB code runs offline.
This synchronous worker is for short synthetic jobs, not a GPU process runner.
"""

import io
import json
import os
from uuid import uuid4

from ..calls.audio import audio_duration
from ..common import digest, identifier


class FakeWorker:
    def __init__(self, transport, token, root, clock):
        self.transport, self.token, self.root, self.clock = transport, token, root, clock
        root.mkdir(parents=True, exist_ok=True)

    def call(self, method, path, *, lease=None, request_id=None, **kwargs):
        headers = {'Authorization': 'Bearer ' + self.token}
        if lease:
            headers['X-Lease-Token'] = lease
        if request_id:
            headers['X-Result-Request-ID'] = request_id
        response = self.transport(method, path, headers=headers, **kwargs)
        if response.status_code != 200:
            raise RuntimeError('Synthetic worker API rejected request')
        return response

    def clean(self):
        for item in self.call('POST', '/worker/cleanup', json={}).json():
            key = str(identifier(item['copy_key']))
            for suffix in ('.input', '.spool'):
                path = self.root / (key + suffix)
                if path.is_symlink():
                    raise RuntimeError('Unsafe synthetic worker path')
                path.unlink(missing_ok=True)
            self.call('POST', '/worker/cleanup', json={**item, 'stopped': True, 'removed': True})

    def once(self, *, transcript_text='synthetic test result', events=None, summary=None):
        self.clean()
        job = self.call('POST', '/worker/claim').json()
        if job is None:
            return None
        path = '/worker/jobs/' + job['job_id']
        lease = job['lease_token']
        data = self.call('GET', path + '/input', lease=lease).content
        if digest(data) != job['input_sha256']:
            raise RuntimeError('Synthetic input hash mismatch')
        key = str(identifier(job['copy_key']))
        (self.root / (key + '.input')).write_bytes(data)
        self.call('POST', path + '/heartbeat', lease=lease, json={})
        self.call('POST', path + '/start', lease=lease, json={'request_id': str(uuid4())})
        result = {name: job[name] for name in ('job_id', 'input_sha256', 'input_artifact_id', 'input_artifact_version')}
        result.update(attempt_id=job['copy_key'], schema_version=1, synthetic=True)
        if job['stage'] == 'STT':
            duration = audio_duration(io.BytesIO(data))
            result.update(schema_version=2, segments=[{'segment_id': 1, 'start': 0, 'end': duration,
                                                       'text': transcript_text}])
        elif events is not None or summary is not None:
            result.update(schema_version=2, events=[] if events is None else events)
            if summary is not None:
                result['summary'] = summary
        else:
            result['text'] = 'synthetic test result'
        payload = json.dumps(result, sort_keys=True).encode()
        spool = self.root / (key + '.spool')
        with spool.open('xb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        request_id = str(uuid4())
        self.call('POST', path + '/prepared', lease=lease, json={'request_id': request_id, 'sha256': digest(payload),
                  'size': len(payload), 'spool_created_at': self.clock().isoformat()})
        ack = self.call('POST', path + '/result', lease=lease, request_id=request_id, content=spool.read_bytes()).json()
        self.clean()
        return ack
