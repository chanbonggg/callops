import io
import os
import re
import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from werkzeug.security import generate_password_hash

from callops.config import Settings
from callops.service import digest
from callops.web import create_app


class ProductCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.settings = Settings.from_env({'CALLOPS_DATABASE_URL': os.environ.get('CALLOPS_TEST_DATABASE_URL', ''),
                                          'CALLOPS_DATA_ROOT': self.temp.name})
        if self.settings.database != 'callops_test' or self.settings.host != '127.0.0.1':
            raise RuntimeError('Dedicated test DB required')
        self.tokens = {'one': 'test-worker-one-' + 'x' * 32, 'two': 'test-worker-two-' + 'y' * 32}
        self.app = create_app(self.settings, secret='test-session-' + 'z' * 32, owner='owner',
                              password_hash=generate_password_hash('test-password'), workers=self.tokens, clock=lambda: self.now)
        self.client = self.app.test_client()
        self.service = self.app.extensions['callops']
        self.queue = self.app.extensions['queue']
        self.recordings = []
        self.addCleanup(self.clear_records)

    def clear_records(self):
        for recording in self.recordings:
            with self.service.transaction() as conn:
                conn.execute("UPDATE callops.recordings SET classification_status='PRIVATE_EXCLUDED' WHERE id=%s", (recording,))
                self.service.request_delete(conn, recording)
        for worker in self.tokens:
            for item in self.queue.cleanup(worker):
                self.queue.cleanup(worker, {**item, 'stopped': True, 'removed': True})
        self.service.maintain()

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)

    def wav(self):
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            audio.writeframes(uuid4().bytes * 1000)
        return output.getvalue()

    def ingest(self, data=None, name=None):
        data = self.wav() if data is None else data
        name = name or str(uuid4()) + '.wav'
        self.service.safe_path('sync-inbox', name).write_bytes(data)
        recording = self.service.ingest(name, digest(data))
        if recording not in self.recordings:
            self.recordings.append(recording)
        return recording

    def row(self, recording):
        with self.service.transaction() as conn:
            return self.service.recording(conn, recording)

    def jobs(self, recording):
        with self.service.transaction() as conn:
            return conn.execute('SELECT * FROM callops.jobs WHERE recording_id=%s ORDER BY stage', (recording,)).fetchall()

    def http(self, method, path, **kwargs):
        return self.client.open(path, method=method, base_url='https://localhost', **kwargs)

    def login(self):
        page = self.http('GET', '/login')
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        result = self.http('POST', '/login', data={'csrf': csrf, 'username': 'owner', 'password': 'test-password'})
        self.assertEqual(result.status_code, 302)
        return re.search(r'name="csrf" value="([^"]+)"', self.http('GET', '/').text)[1]

    def api(self, action, job=None, *, worker='one', **kwargs):
        headers = {'Authorization': 'Bearer ' + self.tokens[worker]}
        path = '/worker/' + action
        if job:
            headers['X-Lease-Token'] = job['lease_token']
            path = '/worker/jobs/' + job['job_id'] + '/' + action
        headers.update(kwargs.pop('headers', {}))
        return self.http('POST', path, headers=headers, **kwargs)
