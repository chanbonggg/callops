from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg

from callops.common import Conflict, digest
from tests.support import ProductCase


class IngestionTests(ProductCase):
    def test_browser_login_accepts_multipart_and_limits_request_body(self):
        page = self.http('GET', '/login')
        cookie = page.headers['set-cookie'].lower()
        for flag in ('secure', 'httponly', 'samesite=strict'):
            self.assertIn(flag, cookie)
        self.assertEqual(self.http('GET', '/login', headers={'Host': 'untrusted.example'}).status_code, 400)
        self.assertEqual(self.http('POST', '/login', data={'username': 'x' * 16384}).status_code, 413)

        import re
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        fields = {'username': 'owner', 'password': 'test-password', 'csrf': csrf}
        response = self.http('POST', '/login', files={key: (None, value) for key, value in fields.items()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.http('GET', '/').status_code, 200)

    def test_login_csrf_wrong_user_and_throttle(self):
        import re
        page = self.http('GET', '/login')
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        self.assertEqual(self.http('POST', '/login', data={'username': 'owner', 'password': 'test-password'}).status_code, 403)
        for _ in range(5):
            response = self.http('POST', '/login', data={'csrf': csrf, 'username': '다른 사용자', 'password': 'test-password'})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.http('POST', '/login', data={'csrf': csrf, 'username': 'owner', 'password': 'test-password'}).status_code, 429)
        self.advance(minutes=5)
        self.login()

    def test_auth_csrf_audio_logout_and_session_expiry(self):
        recording = self.ingest()
        path = '/recordings/' + recording
        for method, url in [('GET', '/'), ('GET', path + '/audio'), ('POST', path + '/classify')]:
            self.assertEqual(self.http(method, url).status_code, 401)
        csrf = self.login()
        self.assertEqual(self.http('GET', path + '/audio').status_code, 200)
        self.assertEqual(self.http('GET', path + '/classify').status_code, 405)
        self.assertEqual(self.http('POST', path + '/classify', data={'decision': 'BUSINESS_APPROVED'}).status_code, 403)
        self.assertEqual(self.jobs(recording), [])
        cookie = next(cookie for cookie in self.client.cookies.jar if cookie.name == 'session')
        self.assertEqual(self.http('POST', '/logout', data={'csrf': csrf}).status_code, 302)
        self.client.cookies.jar.set_cookie(cookie)
        self.assertEqual(self.http('GET', '/').status_code, 401)
        self.login()
        self.advance(hours=8)
        self.assertEqual(self.http('GET', '/').status_code, 401)

    def test_duplicate_private_and_reappeared_copies_never_enter_queue(self):
        data = self.wav()
        phone = Path(self.temp.name) / 'synthetic-phone-original'
        phone.write_bytes(data)
        recording = self.ingest(data, 'first.wav')
        self.assertEqual(self.ingest(data, 'renamed.wav'), recording)
        self.assertEqual(self.jobs(recording), [])
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.row(recording)['deletion_state'], 'COMPLETE')
        self.assertFalse(list(self.service.safe_path('quarantine').iterdir()))
        self.assertFalse(list(self.service.safe_path('sync-inbox').iterdir()))
        self.assertEqual(self.ingest(data, 'again.wav'), recording)
        self.assertEqual(self.row(recording)['deletion_state'], 'COMPLETE')
        self.assertEqual(self.jobs(recording), [])
        self.assertEqual(phone.read_bytes(), data)

    def test_rejected_file_cleanup_after_24_hours_and_link_rejection(self):
        path = self.service.safe_path('sync-inbox', 'rejected.wav')
        path.write_bytes(b'not a recording')
        with self.assertRaises(Conflict):
            self.service.ingest(path.name, digest(path.read_bytes()))
        self.advance(hours=23)
        self.service.maintain()
        self.assertTrue(path.exists())
        self.advance(hours=1)
        self.service.maintain()
        self.assertFalse(path.exists())
        linked = self.service.safe_path('sync-inbox', 'linked.wav')
        linked.touch()
        original = Path.lstat
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
            def lstat(path):
                return SimpleNamespace(st_mode=mode, st_file_attributes=attributes) if path == linked else original(path)
            with patch.object(Path, 'lstat', lstat), self.assertRaises(Conflict):
                self.service.safe_path('sync-inbox', 'linked.wav')

    def test_replaced_rejected_file_gets_its_own_cleanup_deadline(self):
        path = self.service.safe_path('sync-inbox', 'replaced-rejected.wav')
        path.write_bytes(b'invalid first generation')
        with self.assertRaises(Conflict):
            self.service.ingest(path.name, digest(path.read_bytes()))
        self.advance(hours=23, minutes=59)
        replacement = self.wav()
        path.write_bytes(replacement)
        stale_hash = digest(b'invalid first generation')
        with self.assertRaisesRegex(Conflict, 'TRANSFER_INCOMPLETE'):
            self.service.ingest(path.name, stale_hash)
        self.advance(minutes=1)
        self.service.maintain()
        self.assertEqual(path.read_bytes(), replacement)
        self.advance(hours=12)
        with self.assertRaisesRegex(Conflict, 'TRANSFER_INCOMPLETE'):
            self.service.ingest(path.name, stale_hash)
        self.advance(hours=11, minutes=59)
        self.service.maintain()
        self.assertFalse(path.exists())

    def test_deferred_expiry_does_not_extend_and_get_does_not_mutate(self):
        recording = self.ingest()
        expires = self.row(recording)['expires_at']
        self.service.classify(recording, 'DEFERRED')
        self.assertEqual(self.row(recording)['expires_at'], expires)
        self.advance(hours=72)
        self.login()
        self.assertEqual(self.http('GET', '/recordings/' + recording + '/audio').status_code, 410)
        self.assertEqual(self.row(recording)['classification_status'], 'DEFERRED')
        with self.assertRaises(Conflict):
            self.service.classify(recording, 'BUSINESS_APPROVED')
        self.assertEqual(self.row(recording)['classification_status'], 'EXPIRED')
        self.assertEqual(self.row(recording)['deletion_state'], 'COMPLETE')

    def test_partial_disguised_oversize_and_path_escape_rejected(self):
        valid = self.wav()
        for content in (b'', b'<html>not audio</html>', valid[:-8]):
            name = str(uuid4()) + '.wav'
            self.service.safe_path('sync-inbox', name).write_bytes(content)
            with self.assertRaises(Conflict):
                self.service.ingest(name, digest(content))
        self.service.safe_path('sync-inbox', 'partial.wav').write_bytes(valid[:-8])
        with self.assertRaises(Conflict):
            self.service.ingest('partial.wav', digest(valid))
        with patch.object(self.service, 'MAX_AUDIO', 2), self.assertRaises(Conflict):
            self.ingest(valid)
        for name in ('../outside.wav', 'C:\\outside.wav', 'x/y.wav', 'a:stream', '..'):
            with self.assertRaises(Conflict):
                self.service.ingest(name, digest(valid))
        self.assertFalse(list(self.service.safe_path('quarantine').iterdir()))

    def test_atomic_approval_and_unique_job_and_no_ai_for_t04(self):
        recording = self.ingest()
        # Observe the real worker boundary: pending/private cannot even claim.
        from callops.processing.fake_worker import FakeWorker
        worker = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'fake-worker', lambda: self.now)
        with patch.object(worker, 'call', wraps=worker.call) as calls:
            self.assertIsNone(worker.once())
            self.assertFalse(any('/start' in str(item) for item in calls.call_args_list))
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertIsNone(worker.once())
        business = self.ingest()
        self.service.classify(business, 'BUSINESS_APPROVED')
        self.service.classify(business, 'BUSINESS_APPROVED')
        self.assertEqual(len(self.jobs(business)), 1)
        failed = self.ingest()
        original = psycopg.Connection.execute
        def fail_job(conn, query, *args, **kwargs):
            if 'INSERT INTO callops.jobs' in str(query):
                raise psycopg.OperationalError('synthetic failure after classification update')
            return original(conn, query, *args, **kwargs)
        with patch.object(psycopg.Connection, 'execute', fail_job), self.assertRaises(psycopg.OperationalError):
            self.service.classify(failed, 'BUSINESS_APPROVED')
        self.assertEqual(self.row(failed)['classification_status'], 'PENDING')
        self.assertEqual(self.jobs(failed), [])

    def test_modified_during_copy_is_rejected(self):
        data = self.wav()
        source = self.service.safe_path('sync-inbox', 'changing.wav')
        source.write_bytes(data)
        import os
        original = os.fsync
        def change_source(fd):
            original(fd)
            source.write_bytes(data[:-2])
        with patch('callops.calls.service.os.fsync', change_source), self.assertRaisesRegex(Conflict, 'TRANSFER_INCOMPLETE'):
            self.service.ingest(source.name, digest(data))
        self.assertEqual(list(self.service.safe_path('quarantine').iterdir()), [])

    def test_delete_failure_stays_pending_and_reused_name_is_preserved(self):
        recording = self.ingest(name='reuse.wav')
        replacement = self.wav()
        source = self.service.safe_path('sync-inbox', 'reuse.wav')
        source.write_bytes(replacement)
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.row(recording)['deletion_state'], 'RETRY_WAIT')
        self.assertEqual(source.read_bytes(), replacement)
        source.unlink()
        self.service.maintain()
        self.assertEqual(self.row(recording)['deletion_state'], 'COMPLETE')

    def test_private_cleanup_continues_when_an_inbox_path_is_reused(self):
        from callops.processing.fake_worker import FakeWorker

        original = self.wav()
        recording = self.ingest(original, 'verified-reuse.wav')
        self.assertEqual(self.ingest(original, 'unverified-reuse.wav'), recording)
        self.service.classify(recording, 'BUSINESS_APPROVED')
        worker = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'fake-worker', lambda: self.now)
        artifacts = [self.service.safe_path('artifacts', worker.once()['artifact_id']) for _ in range(2)]
        verified = self.wav()
        unverified = self.wav()
        self.assertNotEqual(self.ingest(verified, 'verified-reuse.wav'), recording)
        path = self.service.safe_path('sync-inbox', 'unverified-reuse.wav')
        path.write_bytes(unverified)

        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertFalse(any(artifact.exists() for artifact in artifacts))
        self.assertFalse(self.service.audio_path(self.row(recording)).exists())
        self.assertEqual(self.row(recording)['deletion_state'], 'RETRY_WAIT')
        self.assertEqual(path.read_bytes(), unverified)

        self.assertNotEqual(self.ingest(unverified, path.name), recording)
        self.assertEqual(self.row(recording)['deletion_state'], 'COMPLETE')
        self.assertEqual(path.read_bytes(), unverified)
        self.assertEqual(self.service.safe_path('sync-inbox', 'verified-reuse.wav').read_bytes(), verified)

    def test_db_failure_stops_ingestion_and_recovery_removes_orphans(self):
        data = self.wav()
        self.service.safe_path('sync-inbox', 'test.wav').write_bytes(data)
        with patch('callops.calls.service.psycopg.connect', side_effect=psycopg.OperationalError), self.assertRaises(psycopg.OperationalError):
            self.service.ingest('test.wav', digest(data))
        self.assertFalse(list(self.service.safe_path('quarantine').iterdir()))
        orphan = self.service.safe_path('quarantine', str(uuid4()))
        orphan.write_bytes(data)
        self.service.recover()
        self.assertFalse(orphan.exists())
