import io
import json
import runpy
import sys
import wave
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from callops.processing.fake_worker import FakeWorker
from callops.common import digest
from callops.search.retrieval import retrieve_evidence
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

    def test_rag_filters_before_embedding_and_cites_original_segments(self):
        self.save_search_transcript('가상 A 거래처', '2030-01-01T00:00', '기간 밖 통화')
        self.save_search_transcript('가상 B 거래처', '2029-12-31T23:59', '거래처 밖 통화')
        recording = self.ingest()
        self.service.update_metadata(recording, '가상 A 거래처', '2029-12-31T23:59')
        segments = [
            {'segment_id': 1, 'start': 0, 'end': 0.25, 'text': '초기 제안 ' + '가' * 220},
            {'segment_id': 2, 'start': 0.25, 'end': 0.5, 'text': '날짜 변경 ' + '나' * 220},
            {'segment_id': 3, 'start': 0.5, 'end': 1, 'text': '수요일 방문 확정 ' + '다' * 220},
        ]
        _, job, data, report = self.prepare_transcript(recording=recording, segments=segments)
        uploaded = self.upload(job, data, report)
        self.assertEqual(uploaded.status_code, 200)
        artifact = uploaded.json()['artifact_id']
        query = '방문 날짜가 언제죠?'
        seen = []

        def embed(text):
            seen.append(text)
            if text == query or '수요일 방문 확정' in text:
                return (1, 0)
            return (1, 1)

        matches, limited, unavailable = retrieve_evidence(
            self.service, query, embed=embed, counterparty='가상 A',
            from_date='2029-12-31', to_date='2029-12-31')
        self.assertFalse(limited or unavailable)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all('밖 통화' not in text for text in seen))
        self.assertEqual([[segment['segment_id'] for segment in match['segments']] for match in matches],
                         [[2, 3], [1, 2]])
        first = matches[0]
        self.assertEqual((str(first['recording_id']), str(first['artifact_id']), first['start'], first['end']),
                         (recording, artifact, 0.25, 1))
        source = f'/recordings/{recording}/transcripts/{artifact}'
        self.assertEqual(first['segments'][1]['source_url'], source + '#segment-3')
        self.assertEqual(self.http('GET', source).status_code, 401)
        self.login()
        page = self.http('GET', source)
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="segment-3"', page.text)
        self.assertIn('수요일 방문 확정', page.text)
        self.assertEqual(self.http('GET', source + '/audio/3').status_code, 200)

    def test_question_without_filter_and_with_ambiguous_or_expired_evidence(self):
        fields = {'q': '내가 A 거래처랑 미팅이 언제였지?'}
        self.assertEqual(self.http('GET', '/questions').status_code, 401)
        self.assertEqual(self.http('POST', '/questions', data=fields).status_code, 401)
        csrf = self.login()
        self.assertIn('<details>', self.http('GET', '/questions').text)
        self.assertEqual(self.http('POST', '/questions', data=fields).status_code, 403)

        def ask(**overrides):
            return self.http('POST', '/questions', data={**fields, 'csrf': csrf, **overrides})

        self.assertIn('답변할 근거를 찾지 못했어요', ask().text)
        self.assertEqual(ask(q='   ').status_code, 400)
        self.assertEqual(ask(q='가' * 201).status_code, 400)
        self.assertEqual(ask(from_date='2030-01-01', to_date='2029-12-31').status_code, 400)
        recording, artifact = self.save_search_transcript(
            '가상 A 거래처', '2029-12-31T23:59', '견적 미팅은 1월 2일 오후 2시입니다. <script>가상</script>')
        self.save_search_transcript('가상 B 거래처', '2029-12-31T23:59', '다른 회사 미팅은 1월 3일입니다.')
        source = f'/recordings/{recording}/transcripts/{artifact}'
        response = ask()
        self.assertEqual(response.status_code, 200)
        self.assertIn('1월 2일 오후 2시', response.text)
        self.assertNotIn('다른 회사', response.text)
        self.assertNotIn('<script>가상</script>', response.text)
        self.assertIn(source + '#segment-1', response.text)
        self.assertEqual(self.http('GET', source + '/audio/1').status_code, 200)
        self.assertIn(source + '/audio/1', response.text)

        other, _ = self.save_search_transcript(
            '가상 A 거래처', '2030-01-01T00:00', '납품 미팅은 1월 4일이지만 취소됐습니다.')
        self.assertIn('어느 미팅을 말씀하시나요?', ask().text)
        self.assertIn('미팅 시점을 확정하기 어려워요', ask(from_date='2030-01-01').text)
        self.assertNotIn('납품 미팅은', ask(q='A 거래처랑 견적 미팅이 언제였지?').text)
        self.assertNotIn('납품 미팅은', ask(to_date='2029-12-31').text)
        self.service.classify(other, 'PRIVATE_EXCLUDED')
        self.assertNotIn('납품 미팅은', ask().text)
        self.advance(days=31)
        csrf = self.login()
        self.assertIn(source + '#segment-1', ask().text)
        self.assertNotIn(source + '/audio/1', ask().text)
        self.advance(days=60)
        csrf = self.login()
        self.assertIn('답변할 근거를 찾지 못했어요', ask().text)

    def test_rag_rechecks_evidence_removed_while_embeddings_run(self):
        started_at = '2029-12-31T23:59'
        self.save_search_transcript('가상 거래처', started_at, '곧 만료될 전사')
        self.advance(hours=1)
        deleted, _ = self.save_search_transcript('가상 거래처', started_at, '삭제 요청될 전사')
        private, _ = self.save_search_transcript('가상 거래처', started_at, '사적 전사')
        kept, _ = self.save_search_transcript('가상 거래처', started_at, '유지할 전사')
        self.service.classify(private, 'PRIVATE_EXCLUDED')
        self.advance(days=89, hours=22, minutes=59)
        seen = []

        def embed(text):
            seen.append(text)
            if text == '삭제 요청될 전사':
                with self.service.transaction() as conn:
                    self.service.request_delete(conn, deleted)
                self.advance(minutes=2)
            return (1, 0)

        matches, _, _ = retrieve_evidence(self.service, '확인할 내용', embed=embed)
        self.assertIn('곧 만료될 전사', seen)
        self.assertIn('삭제 요청될 전사', seen)
        self.assertNotIn('사적 전사', seen)
        self.assertEqual([str(match['recording_id']) for match in matches], [kept])

    def test_structured_transcript_round_trip_ack_and_immutable_source_links(self):
        module = Path(__file__).resolve().parents[2] / 'ai-worker/app/transcriber.py'
        transcribe_audio = runpy.run_path(str(module))['transcribe_audio']
        audio = Path(self.temp.name) / 'sample.wav'
        audio.write_bytes(self.wav())
        model = Mock()
        model.transcribe.return_value = (iter([
            SimpleNamespace(start=0, end=0.5, text=' 가상 상담을 확인합니다. '),
            SimpleNamespace(start=0.5, end=1, text='<script>예시 문구</script>'),
        ]), SimpleNamespace(duration=1, language='ko'))
        with patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Mock(return_value=model))}):
            output = transcribe_audio(audio)
        recording, job, data, report = self.prepare_transcript(
            recording=self.ingest(audio.read_bytes()), segments=output['segments'])
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

    def test_analysis_summary_uses_the_same_transcript_after_retry(self):
        requests = {}

        def transport(method, path, **kwargs):
            response = self.http(method, path, **kwargs)
            if path == '/worker/claim':
                requests['job'] = response.json()
            if path.endswith('/result'):
                requests['upload'] = (method, path, kwargs)
                requests['status'] = response.status_code
            return response

        recording = self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        worker = FakeWorker(transport, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)
        transcript = worker.once()['artifact_id']
        source = f'/recordings/{recording}/transcripts/{transcript}'
        self.login()
        self.assertNotIn('합성 요약', self.http('GET', source).text)
        with self.assertRaises(RuntimeError):
            worker.once(summary={'text': '잘못된 근거', 'segment_ids': [2]})
        self.assertEqual(requests['status'], 409)
        failed = requests['job']
        self.assertEqual(self.api('failed', failed, json={'code': 'UPLOAD_ERROR'}).status_code, 200)
        self.advance(minutes=1)
        accepted = worker.once(summary={'text': '<script>가상 상담 요약</script>', 'segment_ids': [1]})
        self.assertEqual(requests['job']['job_id'], failed['job_id'])
        self.assertEqual(requests['job']['input_artifact_id'], transcript)
        self.assertEqual(self.http(*requests['upload'][:2], **requests['upload'][2]).json(), accepted)
        page = self.http('GET', '/recordings/' + recording)
        self.assertEqual(page.status_code, 200)
        self.assertIn('&lt;script&gt;가상 상담 요약&lt;/script&gt;', page.text)
        self.assertIn(f'href="{source}#segment-1">근거 보기', page.text)
        evidence = self.http('GET', source).text
        self.assertIn('<h1>전사 근거</h1>', evidence)
        self.assertIn('id="segment-1"', evidence)
        self.service.safe_path('artifacts', accepted['artifact_id']).unlink()
        self.assertNotIn('합성 요약', self.http('GET', '/recordings/' + recording).text)
        self.assertIn('합성 전사', self.http('GET', source).text)

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
        self.assertIn('통화 분류', pending.text)
        self.assertNotIn('합성 전사', pending.text)
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
        detail = self.http('GET', '/recordings/' + recording)
        self.assertEqual(detail.status_code, 200)
        self.assertIn('전사 결과를 확인할 수 없습니다.', detail.text)
        self.assertNotIn('가상 상담을 확인합니다.', detail.text)
        self.assertIn(f'src="/recordings/{recording}/audio"', detail.text)
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
