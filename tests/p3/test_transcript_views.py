import io
import json
import wave
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from callops.processing.fake_worker import FakeWorker
from callops.common import digest
from tests.support import ProductCase


class TranscriptViewTests(ProductCase):
    def prepare_transcript(self, *, recording=None, segments=None, synthetic=True, schema_version=2):
        recording = recording or self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        response = self.api('claim')
        self.assertEqual(response.status_code, 200)
        job = response.json()
        self.assertEqual(self.api('start', job, json={'request_id': str(uuid4())}).status_code, 200)
        segments = [{'segment_id': 1, 'start': 0, 'end': 0.5, 'text': '가상 상담을 확인합니다.'},
                    {'segment_id': 2, 'start': 0.5, 'end': 1, 'text': '<script>예시 문구</script>'}] if segments is None else segments
        envelope = {key: job[key] for key in ('job_id', 'input_sha256', 'input_artifact_id', 'input_artifact_version')}
        content = {'segments': segments} if schema_version == 2 else {'text': 'synthetic test result'}
        data = json.dumps(envelope | {'attempt_id': job['copy_key'], 'schema_version': schema_version,
                                      'synthetic': synthetic} | content).encode()
        report = {'request_id': str(uuid4()), 'sha256': digest(data), 'size': len(data),
                  'spool_created_at': self.now.isoformat()}
        self.assertEqual(self.api('prepared', job, json=report).status_code, 200)
        return recording, job, data, report

    def upload(self, job, data, report):
        return self.api('result', job, content=data, headers={'X-Result-Request-ID': report['request_id']})

    def save_search_transcript(self, counterparty, started_at, text):
        recording = self.ingest()
        self.service.update_metadata(recording, counterparty, started_at)
        _, job, data, report = self.prepare_transcript(
            recording=recording, segments=[{'segment_id': 1, 'start': 0, 'end': 1, 'text': text}])
        response = self.upload(job, data, report)
        self.assertEqual(response.status_code, 200)
        worker = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)
        self.assertTrue(worker.once()['accepted'])
        return recording, response.json()['artifact_id']

    def test_search_respects_owner_csrf_party_and_korean_call_date(self):
        text = '방문 상담 확정 <script>가상 발화</script>'
        recording, artifact = self.save_search_transcript('가상 A 거래처', '2029-12-31T23:59', text)
        self.save_search_transcript('가상 A 거래처', '2030-01-01T00:00', '다음 날 방문 상담')
        self.save_search_transcript('가상 B 거래처', '2029-12-31T23:59', '다른 거래처 방문 상담')
        fields = {'q': '상담 방문', 'counterparty': '가상 A', 'from_date': '2029-12-31', 'to_date': '2029-12-31'}
        self.assertEqual(self.http('GET', '/search').status_code, 401)
        self.assertEqual(self.http('POST', '/search', data=fields).status_code, 401)
        csrf = self.login()
        self.assertEqual(self.http('GET', '/search').status_code, 200)
        self.assertEqual(self.http('POST', '/search', data=fields).status_code, 403)
        response = self.http('POST', '/search', data=dict(fields, csrf=csrf))
        self.assertEqual(response.status_code, 200)
        self.assertIn('방문 상담 확정', response.text)
        self.assertNotIn('<script>가상 발화</script>', response.text)
        self.assertNotIn('다음 날 방문 상담', response.text)
        self.assertNotIn('다른 거래처 방문 상담', response.text)
        source = f'/recordings/{recording}/transcripts/{artifact}'
        self.assertIn(source + '#segment-1', response.text)
        self.assertEqual(self.http('GET', source).status_code, 200)
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        response = self.http('POST', '/search', data=dict(fields, csrf=csrf))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(source, response.text)
        self.assertNotIn('방문 상담 확정', response.text)

    def test_search_omits_corrupt_expired_and_legacy_transcripts(self):
        _, artifact = self.save_search_transcript('가상 회사', '2029-12-31T20:00', '견적서 발송 합의')
        _, job, data, report = self.prepare_transcript(schema_version=1)
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        csrf = self.login()
        file = self.service.safe_path('artifacts', artifact)
        original = file.read_bytes()
        file.write_bytes(original + b' ')
        response = self.http('POST', '/search', data={'q': '견적서', 'csrf': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('견적서 발송 합의', response.text)
        self.assertIn('일부 전사를 확인할 수 없습니다', response.text)
        response = self.http('POST', '/search', data={'q': 'synthetic test result', 'csrf': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertIn('일치하는 전사 구간이 없습니다', response.text)
        file.write_bytes(original)
        self.advance(days=91)
        csrf = self.login()
        response = self.http('POST', '/search', data={'q': '견적서', 'csrf': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('견적서 발송 합의', response.text)
        self.assertIn('일치하는 전사 구간이 없습니다', response.text)

    def test_structured_transcript_round_trip_ack_and_immutable_source_links(self):
        recording, job, data, report = self.prepare_transcript()
        result = self.upload(job, data, report)
        self.assertEqual(result.status_code, 200)
        artifact = result.json()['artifact_id']
        self.assertEqual(self.upload(job, data, report).json(), result.json())
        self.assertEqual(len(self.jobs(recording)), 2)
        path = '/recordings/' + recording + '/transcripts/' + artifact
        self.assertEqual(self.http('GET', path).status_code, 401)
        self.login()
        page = self.http('GET', '/recordings/' + recording)
        self.assertEqual(page.status_code, 200)
        self.assertIn('합성 전사', page.text)
        self.assertIn('가상 상담을 확인합니다.', page.text)
        self.assertNotIn('<script>예시 문구</script>', page.text)
        self.assertIn('&lt;script&gt;', page.text)
        self.assertIn(path + '#segment-2', page.text)
        self.assertIn(path + '/audio/2', page.text)
        self.assertIn(recording, self.http('GET', '/').text)
        self.assertEqual(self.http('GET', path).status_code, 200)
        worker = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)
        worker.clean()
        analysis = self.api('claim').json()
        response = self.http('GET', '/worker/jobs/' + analysis['job_id'] + '/input',
                             headers={'Authorization': 'Bearer ' + self.tokens['one'],
                                      'X-Lease-Token': analysis['lease_token']})
        self.assertEqual(response.content, data)

    def test_citation_audio_contains_only_the_authorized_samples(self):
        original = self.wav()
        recording, job, data, report = self.prepare_transcript(recording=self.ingest(original))
        artifact = self.upload(job, data, report).json()['artifact_id']
        path = f'/recordings/{recording}/transcripts/{artifact}/audio/2'
        self.assertEqual(self.http('GET', path).status_code, 401)
        self.login()
        response = self.http('GET', path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'audio/wav')
        with wave.open(io.BytesIO(original), 'rb') as source, wave.open(io.BytesIO(response.content), 'rb') as clip:
            self.assertEqual(clip.getnframes(), 4000)
            source.setpos(4000)
            self.assertEqual(clip.readframes(4000), source.readframes(4000))
        self.assertEqual(self.http('GET', path[:-1] + '3').status_code, 404)
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.http('GET', path).status_code, 409)

    def test_pending_private_and_other_recording_cannot_expose_a_transcript(self):
        recording, job, data, report = self.prepare_transcript()
        artifact = self.upload(job, data, report).json()['artifact_id']
        other = self.ingest()
        path = '/recordings/' + recording + '/transcripts/' + artifact
        self.assertEqual(self.http('GET', path, headers={'Authorization': 'Bearer ' + self.tokens['one']}).status_code, 401)
        self.login()
        pending = self.http('GET', '/recordings/' + other)
        self.assertEqual(pending.status_code, 200)
        self.assertIn('전사 결과가 아직 없습니다.', pending.text)
        self.assertNotIn('가상 상담을 확인합니다.', pending.text)
        self.assertEqual(self.http('GET', '/recordings/' + other + '/transcripts/' + artifact).status_code, 409)
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        private = self.http('GET', path)
        self.assertEqual(private.status_code, 409)
        self.assertNotIn('가상 상담을 확인합니다.', private.text)
        self.assertFalse(self.service.safe_path('artifacts', artifact).exists())

    def test_legacy_synthetic_text_remains_readable_without_fabricated_segments(self):
        recording, job, data, report = self.prepare_transcript(schema_version=1)
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        self.login()
        page = self.http('GET', '/recordings/' + recording)
        self.assertEqual(page.status_code, 200)
        self.assertIn('synthetic test result', page.text)
        self.assertIn('시간 구간이 없는 합성 결과', page.text)
        self.assertNotIn('id="segment-1"', page.text)

    def test_corrupt_missing_and_expired_files_fail_without_disclosing_content(self):
        recording, job, data, report = self.prepare_transcript()
        artifact = self.upload(job, data, report).json()['artifact_id']
        file = self.service.safe_path('artifacts', artifact)
        path = '/recordings/' + recording + '/transcripts/' + artifact
        self.login()
        file.write_bytes(data + b' ')
        response = self.http('GET', path)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'INPUT_CORRUPT')
        file.unlink()
        self.assertEqual(self.http('GET', path).status_code, 409)
        file.write_bytes(data)
        self.advance(days=91)
        self.login()
        self.assertEqual(self.http('GET', path).status_code, 409)

    def test_audio_expiry_hides_playback_links_but_retains_the_unexpired_transcript(self):
        recording, job, data, report = self.prepare_transcript()
        result = self.upload(job, data, report)
        self.assertEqual(result.status_code, 200)
        artifact = result.json()['artifact_id']
        self.advance(days=31)
        self.login()
        page = self.http('GET', '/recordings/' + recording)
        self.assertEqual(page.status_code, 200)
        self.assertIn('가상 상담을 확인합니다.', page.text)
        self.assertNotIn(f'/transcripts/{artifact}/audio/', page.text)
        self.assertIn('원음을 재생할 수 없습니다.', page.text)
        self.assertEqual(self.http('GET', '/recordings/' + recording + '/audio').status_code, 410)
        self.assertEqual(self.http('GET', f'/recordings/{recording}/transcripts/{artifact}/audio/1').status_code, 410)

    def test_cancelled_upload_and_real_model_results_remain_rejected(self):
        recording, job, data, report = self.prepare_transcript(synthetic=False)
        self.assertEqual(self.upload(job, data, report).status_code, 409)
        self.assertEqual(len(self.jobs(recording)), 1)
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.upload(job, data, report).status_code, 409)
        FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now).clean()
        recording, job, data, report = self.prepare_transcript()
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.upload(job, data, report).status_code, 409)
        self.assertEqual(list(self.service.safe_path('artifacts').iterdir()), [])

    def test_segment_validator_and_upload_reject_invalid_ids_times_and_text(self):
        # The upload must call the same validator used when reading stored files.
        from callops.processing.transcript import validate_segments

        valid = [{'segment_id': 1, 'start': 0, 'end': 1, 'text': '가상 발화'}]
        invalid = [None, {}, [{'segment_id': True, 'start': 0, 'end': 1, 'text': '가상'}],
                   [dict(valid[0], start=-1)], [dict(valid[0], end=3)],
                   [dict(valid[0], start=float('nan'))], [dict(valid[0], end=float('inf'))],
                   [dict(valid[0], start=10 ** 400)], [valid[0]] * 10001,
                   [dict(valid[0], text=10)], [dict(valid[0], text='\ud800')],
                   [dict(valid[0], text='x' * 100001)],
                   [dict(valid[0], path='untrusted')], [valid[0], valid[0]],
                   [dict(valid[0], start=1), {'segment_id': 2, 'start': 0, 'end': 1, 'text': '역순'}]]
        for segments in invalid:
            with self.subTest(kind='invalid segments'), self.assertRaises(ValueError):
                validate_segments(segments, 2)
        self.assertEqual(validate_segments([], 2), [])
        self.assertEqual(validate_segments(deepcopy(valid), 2), valid)
        _, job, data, report = self.prepare_transcript(segments=[dict(valid[0], end=3)])
        with patch('callops.processing.transcript.validate_segments', wraps=validate_segments) as validator:
            response = self.upload(job, data, report)
        validator.assert_called_once()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'INVALID_RESULT')
