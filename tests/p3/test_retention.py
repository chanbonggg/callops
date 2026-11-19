import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from callops.common import Conflict, digest
from callops.processing.fake_worker import FakeWorker
from tests.support import ProductCase


class RetentionTests(ProductCase):
    def worker(self):
        return FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)

    def approved_call(self):
        recording = self.ingest(name='sample.wav')
        self.service.classify(recording, 'BUSINESS_APPROVED')
        return recording

    def transcript(self):
        return self.worker().once(transcript_text='Review the synthetic quote.')['artifact_id']

    def analysis(self):
        event = {'title': 'Quote meeting', 'start': '2030-02-05T14:00:00+09:00',
                 'end': '2030-02-05T15:00:00+09:00', 'time_hint': '', 'segment_ids': [1]}
        task = {'title': 'Send synthetic quote', 'due_at': None, 'segment_ids': [1]}
        return self.worker().once(events=[event, event | {'title': 'Pending meeting'}],
                                  tasks=[task, task | {'title': 'Pending task'}],
                                  summary={'text': 'Quote summary', 'segment_ids': [1]})['artifact_id']

    def approval_forms(self, recording, csrf):
        with self.service.transaction() as conn:
            events = conn.execute('SELECT id,revision FROM callops.schedule_candidates WHERE recording_id=%s ORDER BY position',
                                  (recording,)).fetchall()
            tasks = conn.execute('SELECT id,revision,title FROM callops.task_candidates WHERE recording_id=%s ORDER BY position',
                                 (recording,)).fetchall()
        base = {'csrf': csrf, 'action': 'approve'}
        return (
            [(f'/recordings/{recording}/schedule/{row["id"]}', base | {'revision': row['revision']}) for row in events],
            [(f'/recordings/{recording}/task-candidates', base | {'revision': row['revision'],
              'candidate_id': str(row['id']), 'title': row['title'], 'due': ''}) for row in tasks],
        )

    def prepared(self, stage):
        response = self.api('claim')
        self.assertEqual(response.status_code, 200)
        job = response.json()
        self.assertEqual(job['stage'], stage)
        self.assertEqual(self.api('start', job, json={'request_id': str(uuid4())}).status_code, 200)
        envelope = {key: job[key] for key in ('job_id', 'input_sha256', 'input_artifact_id', 'input_artifact_version')}
        data = json.dumps(envelope | {'attempt_id': job['copy_key'], 'schema_version': 1, 'synthetic': True,
                                     'text': 'synthetic late result'}).encode()
        key = str(uuid4())
        response = self.api('prepared', job, json={'request_id': key, 'sha256': digest(data), 'size': len(data),
                                                  'spool_created_at': self.now.isoformat()})
        self.assertEqual(response.status_code, 200)
        return job, {'content': data, 'headers': {'X-Result-Request-ID': key}}

    def artifact(self, artifact_id):
        with self.service.transaction() as conn:
            return conn.execute('SELECT * FROM callops.artifacts WHERE id=%s', (artifact_id,)).fetchone()

    def test_audio_expiry_cleans_copies_and_preserves_transcript_workflow(self):
        recording = self.approved_call()
        received = self.row(recording)['received_at']
        transcript = self.transcript()
        quarantine = self.service.audio_path(self.row(recording))
        inbox = self.service.safe_path('sync-inbox', 'sample.wav')
        audio = inbox.read_bytes()
        self.assertEqual(self.ingest(audio, 'duplicate.wav'), recording)
        self.advance(days=30)
        self.login()
        path = f'/recordings/{recording}'
        self.assertEqual(self.http('GET', path + '/audio').status_code, 410)
        self.assertTrue(quarantine.is_file())
        self.service.maintain()
        self.assertFalse(quarantine.exists())
        self.assertFalse(inbox.exists())
        self.assertFalse(self.service.safe_path('sync-inbox', 'duplicate.wav').exists())
        self.assertEqual(self.row(recording)['audio_deletion_state'], 'COMPLETE')
        self.assertEqual(self.row(recording)['deletion_state'], 'NONE')

        self.assertEqual(self.ingest(audio, 'resynced.wav'), recording)
        self.assertFalse(self.service.safe_path('sync-inbox', 'resynced.wav').exists())
        self.assertFalse(quarantine.exists())
        self.assertEqual(self.row(recording)['received_at'], received)
        with self.assertRaisesRegex(Conflict, 'INPUT_EXPIRED'):
            self.service.classify(recording, 'BUSINESS_APPROVED')
        self.analysis()
        self.assertEqual([row['status'] for row in self.jobs(recording)], ['SUCCEEDED', 'SUCCEEDED'])
        self.assertEqual(self.http('GET', path + f'/transcripts/{transcript}').status_code, 200)
        self.assertIn('Quote summary', self.http('GET', path).text)
        self.assertIn('Quote meeting', self.http('GET', path + '/schedule').text)
        self.assertIn('Send synthetic quote', self.http('GET', path + '/task-candidates').text)

    def test_derived_files_expire_individually_and_approved_items_survive(self):
        recording = self.approved_call()
        transcript = self.transcript()
        self.advance(days=1)
        analysis = self.analysis()
        self.assertTrue(self.service.safe_path('artifacts', analysis).is_file())
        csrf = self.login()
        schedules, tasks = self.approval_forms(recording, csrf)
        for path, form in (schedules[0], tasks[0]):
            self.assertEqual(self.http('POST', path, data=form).status_code, 303)
        evidence = f'/recordings/{recording}/transcripts/{transcript}'
        self.assertIn(evidence, self.http('POST', '/search', data={'csrf': csrf, 'q': 'synthetic quote'}).text)

        self.advance(days=89)
        csrf = self.login()
        self.assertEqual(self.http('GET', evidence).status_code, 409)
        self.service.maintain()
        self.assertFalse(self.service.safe_path('artifacts', transcript).exists())
        self.assertTrue(self.service.safe_path('artifacts', analysis).is_file())
        self.assertFalse(self.artifact(transcript)['available'])
        self.assertTrue(self.artifact(analysis)['available'])
        self.assertNotIn(evidence, self.http('POST', '/search', data={'csrf': csrf, 'q': 'synthetic quote'}).text)
        for path, form in (schedules[1], tasks[1]):
            self.assertEqual(self.http('POST', path, data=form | {'csrf': csrf}).status_code, 409)
        self.assertEqual(self.approval_forms(recording, csrf), ([], []))
        schedule, = self.http('GET', '/api/schedules').json()['schedules']
        self.assertEqual((schedule['title'], schedule['evidence'], schedule['sendable']), ('Quote meeting', [], False))
        task, = self.http('GET', '/api/tasks').json()['tasks']
        self.assertEqual((task['title'], task['status']), ('Send synthetic quote', 'OPEN'))

        self.advance(days=1)
        self.service.maintain()
        self.assertFalse(self.service.safe_path('artifacts', analysis).exists())
        self.assertEqual(self.artifact(analysis)['deletion_state'], 'COMPLETE')

    def test_expired_audio_retries_unlink_then_waits_for_worker(self):
        recording = self.approved_call()
        quarantine = self.service.audio_path(self.row(recording))
        inbox = self.service.safe_path('sync-inbox', 'sample.wav')
        self.advance(days=30, minutes=-1)
        job, result = self.prepared('STT')
        read_bytes = type(quarantine).read_bytes

        def expire_during_read(path):
            data = read_bytes(path)
            if path == quarantine:
                self.advance(minutes=1)
            return data

        with patch.object(type(quarantine), 'read_bytes', expire_during_read):
            response = self.http('GET', f'/worker/jobs/{job["job_id"]}/input', headers={
                'Authorization': f'Bearer {self.tokens["one"]}', 'X-Lease-Token': job['lease_token'],
            })
            self.assertEqual((response.status_code, response.json()['error']), (409, 'INPUT_EXPIRED'))
        self.assertEqual(self.api('result', job, **result).status_code, 409)
        original_unlink = type(quarantine).unlink

        def fail_quarantine(path, *args, **kwargs):
            if path == quarantine:
                raise PermissionError('synthetic deletion failure')
            return original_unlink(path, *args, **kwargs)

        with patch.object(type(quarantine), 'unlink', fail_quarantine):
            self.service.maintain()
            self.assertTrue(quarantine.exists())
            self.assertFalse(inbox.exists())
            self.assertEqual(self.row(recording)['audio_deletion_state'], 'RETRY_WAIT')
        self.service.recover()
        self.assertFalse(quarantine.exists())
        self.assertEqual(self.row(recording)['audio_deletion_state'], 'REQUESTED')
        stt, = self.jobs(recording)
        self.assertEqual((stt['status'], stt['error_code']), ('CANCELLED', 'INPUT_EXPIRED'))
        self.assertEqual(self.api('claim').status_code, 409)
        self.assertIsNone(self.api('claim', worker='two').json())
        item, = self.api('cleanup', json={}).json()
        self.assertEqual(self.api('cleanup', json=item | {'stopped': True, 'removed': False}).status_code, 409)
        self.assertEqual(self.row(recording)['audio_deletion_state'], 'REQUESTED')
        self.assertEqual(self.api('cleanup', json=item | {'stopped': True, 'removed': True}).status_code, 200)
        self.assertEqual(self.row(recording)['audio_deletion_state'], 'COMPLETE')
        self.assertEqual(self.api('result', job, **result).status_code, 409)
        self.assertEqual(list(self.service.safe_path('artifacts').iterdir()), [])

    def test_expired_analysis_input_recovers_after_unlink_before_commit(self):
        recording = self.approved_call()
        transcript = self.transcript()
        path = self.service.safe_path('artifacts', transcript)
        self.advance(days=90, minutes=-1)
        job, result = self.prepared('ANALYSIS')
        self.advance(minutes=1)
        self.assertEqual(self.api('result', job, **result).status_code, 409)
        original_unlink = type(path).unlink

        def interrupt_after_unlink(file, *args, **kwargs):
            original_unlink(file, *args, **kwargs)
            if file == path:
                raise RuntimeError('synthetic interruption after unlink')

        with patch.object(type(path), 'unlink', interrupt_after_unlink):
            with self.assertRaisesRegex(RuntimeError, 'synthetic interruption after unlink'):
                self.service.maintain()
        self.assertFalse(path.exists())
        artifact = self.artifact(transcript)
        self.assertEqual((artifact['deletion_state'], artifact['available']), ('REQUESTED', False))
        self.service.recover()
        analysis, stt = self.jobs(recording)
        self.assertEqual((analysis['status'], analysis['error_code']), ('CANCELLED', 'INPUT_EXPIRED'))
        self.assertEqual(stt['status'], 'SUCCEEDED')
        item, = self.api('cleanup', json={}).json()
        self.assertEqual(self.artifact(transcript)['deletion_state'], 'REQUESTED')
        self.assertEqual(self.api('cleanup', json=item | {'stopped': True, 'removed': True}).status_code, 200)
        self.assertEqual(self.artifact(transcript)['deletion_state'], 'COMPLETE')
        self.assertEqual(self.api('result', job, **result).status_code, 409)
        self.assertEqual(list(self.service.safe_path('artifacts').iterdir()), [])

    def test_expired_result_file_retries_and_ack_does_not_restore_it(self):
        recording = self.approved_call()
        job, result = self.prepared('STT')
        saved = self.api('result', job, **result)
        self.assertEqual(saved.status_code, 200)
        artifact_id = saved.json()['artifact_id']
        path = self.service.safe_path('artifacts', artifact_id)
        self.advance(days=90)
        original_unlink = type(path).unlink

        def fail_artifact(file, *args, **kwargs):
            if file == path:
                raise PermissionError('synthetic deletion failure')
            return original_unlink(file, *args, **kwargs)

        with patch.object(type(path), 'unlink', fail_artifact):
            self.service.maintain()
            self.service.recover()
            self.assertTrue(path.is_file())
            self.assertEqual(self.artifact(artifact_id)['deletion_state'], 'RETRY_WAIT')
            self.login()
            self.assertEqual(self.http('GET', f'/recordings/{recording}/transcripts/{artifact_id}').status_code, 409)
        self.service.maintain()
        self.assertFalse(path.exists())
        self.assertEqual(self.artifact(artifact_id)['deletion_state'], 'REQUESTED')
        self.assertEqual(self.api('result', job, **result).json(), saved.json())
        self.assertFalse(path.exists())
        item, = self.api('cleanup', json={}).json()
        self.assertEqual(self.api('cleanup', json=item | {'stopped': True, 'removed': True}).status_code, 200)
        self.assertEqual(self.artifact(artifact_id)['deletion_state'], 'COMPLETE')
        self.assertEqual(self.row(recording)['audio_deletion_state'], 'COMPLETE')

    def test_full_delete_finishes_pending_partial_cleanup_and_preserves_replacement(self):
        recording = self.approved_call()
        transcript = self.transcript()
        self.analysis()
        inbox = self.service.safe_path('sync-inbox', 'sample.wav')
        replacement = self.wav()
        inbox.write_bytes(replacement)
        self.advance(days=30)
        self.service.maintain()
        self.assertEqual(self.row(recording)['audio_deletion_state'], 'RETRY_WAIT')
        self.assertEqual(inbox.read_bytes(), replacement)
        self.assertTrue(self.service.safe_path('artifacts', transcript).is_file())
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.row(recording)['deletion_state'], 'RETRY_WAIT')
        self.assertFalse(self.service.safe_path('artifacts', transcript).exists())
        self.assertNotEqual(self.ingest(replacement, inbox.name), recording)
        row = self.row(recording)
        self.assertEqual((row['deletion_state'], row['audio_deletion_state']), ('COMPLETE', 'COMPLETE'))
        self.assertEqual(inbox.read_bytes(), replacement)
