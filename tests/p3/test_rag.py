import json
from pathlib import Path
from uuid import UUID, uuid4

from callops.common import digest
from callops.processing.fake_worker import FakeWorker
from callops.processing.rag import ANSWER_DIGEST, EMBED_DIGEST
from tests.p3.test_real_worker import RealResultCase


def unit(index):
    values = [0.0] * 1024
    values[index] = 1.0
    return values


class RagTests(RealResultCase):
    real_worker = 'one'
    rag_enabled = True

    def clean(self):
        for item in self.api('cleanup', json={}).json():
            self.assertEqual(self.api('cleanup', json={**item, 'stopped': True, 'removed': True}).status_code, 200)

    def transcript(self, label, started_at, text):
        recording = self.ingest()
        self.service.update_metadata(recording, label, started_at)
        self.service.classify(recording, 'BUSINESS_APPROVED')
        stt = self.api('claim').json()
        data, report = self.prepare(
            stt, segments=[{'segment_id': 1, 'start': 0, 'end': 0.5, 'text': text, 'speaker': 'A'}],
            warnings=[], model_name='large-v3', language='ko', duration_seconds=2,
        )
        response = self.upload(stt, data, report)
        self.assertEqual(response.status_code, 200)
        fake = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)
        self.assertTrue(fake.once()['accepted'])
        return recording, response.json()['artifact_id']

    def submit(self, **fields):
        csrf = self.login()
        response = self.http('POST', '/questions', data={'q': '일정과 자료를 알려 줘', 'csrf': csrf} | fields)
        self.assertEqual(response.status_code, 303)
        return response.headers['location']

    def claim_rag(self, stage):
        response = self.api('claim', json={'rag_contract_version': 1})
        self.assertEqual(response.status_code, 200)
        job = response.json()
        self.assertEqual(job['stage'], stage)
        source = self.http('GET', f"/worker/jobs/{job['job_id']}/input",
                           headers={'Authorization': 'Bearer ' + self.tokens['one'],
                                    'X-Lease-Token': job['lease_token']})
        self.assertEqual(source.status_code, 200)
        self.assertEqual(digest(source.content), job['input_sha256'])
        self.assertEqual(source.headers['X-Input-Snapshot-ID'], job['input_snapshot_id'])
        self.assertEqual(source.headers['X-Input-Snapshot-Version'], str(job['input_snapshot_version']))
        return job, source.json()

    def prepare_rag(self, job, result):
        self.assertEqual(self.api('start', job, json={'request_id': str(uuid4())}).status_code, 200)
        envelope = {field: job[field] for field in ('job_id', 'input_snapshot_id', 'input_snapshot_version', 'input_sha256')}
        data = json.dumps(envelope | {'attempt_id': job['copy_key'],
                                     'schema_version': 1, 'output': result}).encode()
        report = {'request_id': str(uuid4()), 'sha256': digest(data), 'size': len(data),
                  'spool_created_at': self.now.isoformat()}
        self.assertEqual(self.api('prepared', job, json=report).status_code, 200)
        return data, report

    def embedding(self, *vectors):
        return {'schema_version': 1, 'model_name': 'bge-m3:567m', 'model_digest': EMBED_DIGEST,
                'dimensions': 1024, 'embeddings': list(vectors)}

    def answer(self, *citations):
        return {'schema_version': 1, 'model_name': 'qwen3:8b', 'model_digest': ANSWER_DIGEST,
                'prompt_version': 'rag-v10', 'answer': '가상 약속과 자료 <script>예시</script>',
                'reason': None, 'segment_ids': list(citations)}

    def complete(self, job, result):
        data, report = self.prepare_rag(job, result)
        response = self.upload(job, data, report)
        self.assertEqual(response.status_code, 200, response.text)
        ack = response.json()
        self.assertIs(ack['accepted'], True)
        self.assertEqual(str(UUID(ack['result_id'])), ack['result_id'])
        self.assertEqual(self.api('status', job, json={'request_id': report['request_id']}).json(), ack)
        self.assertEqual(self.upload(job, data, report).json(), ack)
        self.clean()

    def test_questions_filter_before_pgvector_and_validate_multi_call_citations(self):
        outside = self.transcript('가상 거래처 A', '2030-01-01T00:00', '범위 밖 합의')
        index, payload = self.claim_rag('RAG_EMBED')
        self.assertEqual(payload['texts'], ['범위 밖 합의'])
        self.complete(index, self.embedding(unit(0)))
        first = self.transcript('가상 거래처 A', '2029-12-31T22:00', '방문을 합의했어요')
        second = self.transcript('가상 거래처 A', '2029-12-31T23:00', '자료를 보낼게요')
        url = self.submit(counterparty='가상 거래처 A', to_date='2029-12-31')
        self.assertIn('검색할 전사를 준비하고 있어요', self.http('GET', url).text)
        self.assertIsNone(self.api('claim').json())
        self.assertIsNone(self.api('claim', worker='two', json={'rag_contract_version': 1}).json())
        for unsupported in ({'rag_contract_version': 2}, {}):
            self.assertIsNone(self.api('claim', json=unsupported).json())
        for invalid in (True, '1', 1.0, None):
            self.assertEqual(self.api('claim', json={'rag_contract_version': invalid}).status_code, 400)

        vectors = {'방문을 합의했어요': unit(1), '자료를 보낼게요': unit(0)}
        for _ in range(2):
            job, payload = self.claim_rag('RAG_EMBED')
            self.assertEqual(len(payload['texts']), 1)
            self.complete(job, self.embedding(vectors.pop(payload['texts'][0])))
        query, payload = self.claim_rag('RAG_EMBED')
        self.assertNotIn('input_artifact_id', query)
        self.assertEqual(payload['texts'], ['일정과 자료를 알려 줘'])
        self.complete(query, self.embedding(unit(0)))
        answer, payload = self.claim_rag('RAG_ANSWER')
        ids = [f'{second[1]}:1', f'{first[1]}:1']
        self.assertEqual([item['id'] for item in payload['evidence']], ids)
        rejected, report = self.prepare_rag(answer, self.answer(f'{outside[1]}:1'))
        self.assertEqual(self.upload(answer, rejected, report).json()['error'], 'INVALID_RESULT')
        self.clean()
        self.assertIn('질문 처리를 마치지 못했어요', self.http('GET', url).text)
        self.assertIsNone(self.api('claim', json={'rag_contract_version': 1}).json())
        url = self.submit(counterparty='가상 거래처 A', to_date='2029-12-31')
        query, _ = self.claim_rag('RAG_EMBED')
        self.complete(query, self.embedding(unit(0)))
        answer, _ = self.claim_rag('RAG_ANSWER')
        self.complete(answer, self.answer(*ids))

        page = self.http('GET', url).text
        self.assertIn('가상 약속과 자료 &lt;script&gt;예시&lt;/script&gt;', page)
        for recording, artifact in (first, second):
            self.assertIn(f'/recordings/{recording}/transcripts/{artifact}#segment-1', page)
            audio = f'/recordings/{recording}/transcripts/{artifact}/audio/1'
            self.assertIn(audio, page)
            self.assertEqual(self.http('GET', audio).status_code, 200)
        self.service.classify(first[0], 'PRIVATE_EXCLUDED')
        page = self.http('GET', url).text
        self.assertIn('질문에 사용된 원본을 이용할 수 없어요', page)
        self.assertNotIn('가상 약속과 자료', page)
        with self.service.transaction() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM callops.rag_chunks WHERE artifact_id=%s', (first[1],)).fetchone())

    def test_expired_index_batch_cannot_publish(self):
        _, artifact = self.transcript('가상 거래처 A', '2029-12-31T22:00', '만료 시험 발화')
        job, _ = self.claim_rag('RAG_EMBED')
        data, report = self.prepare_rag(job, self.embedding(unit(0)))
        with self.service.transaction() as conn:
            conn.execute('UPDATE callops.artifacts SET expires_at=%s WHERE id=%s', (self.now, artifact))
        self.service.maintain()
        rejected = self.upload(job, data, report)
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()['error'], 'INVALID_LEASE')
        with self.service.transaction() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM callops.rag_chunks WHERE artifact_id=%s', (artifact,)).fetchone())
        self.clean()

    def test_expired_answer_cannot_publish_and_cleanup_is_confirmed(self):
        _, artifact = self.transcript('가상 거래처 A', '2029-12-31T22:00', '만료 시험 발화')
        index, _ = self.claim_rag('RAG_EMBED')
        self.complete(index, self.embedding(unit(0)))
        url = self.submit()
        query, _ = self.claim_rag('RAG_EMBED')
        self.complete(query, self.embedding(unit(0)))
        answer, _ = self.claim_rag('RAG_ANSWER')
        data, report = self.prepare_rag(answer, self.answer(f'{artifact}:1'))
        with self.service.transaction() as conn:
            conn.execute('UPDATE callops.artifacts SET expires_at=%s WHERE id=%s', (self.now, artifact))
        self.service.maintain()
        self.assertEqual(self.upload(answer, data, report).json()['error'], 'INVALID_LEASE')
        self.assertIn('질문에 사용된 원본을 이용할 수 없어요', self.http('GET', url).text)
        self.assertEqual(self.api('claim', json={'rag_contract_version': 1}).json()['error'], 'WORKER_NOT_CLEAN')
        self.clean()
        with self.service.transaction() as conn:
            result = conn.execute('SELECT deletion_state FROM callops.artifacts WHERE id=%s', (artifact,)).fetchone()
            self.assertEqual(result['deletion_state'], 'COMPLETE')

    def test_empty_scope_completes_without_worker_and_result_expires_after_one_day(self):
        url = self.submit(counterparty='가상 통화 없음')
        self.assertIn('검색 범위에서 답할 근거를 찾지 못했어요.', self.http('GET', url).text)
        self.assertIsNone(self.api('claim', json={'rag_contract_version': 1}).json())
        self.advance(hours=23)
        self.service.maintain()
        self.login()
        self.assertIn('검색 범위에서 답할 근거를 찾지 못했어요.', self.http('GET', url).text)
        self.advance(hours=1)
        self.service.maintain()
        page = self.http('GET', url).text
        self.assertIn('질문 결과가 만료됐어요', page)
        self.assertNotIn('일정과 자료를 알려 줘', page)

    def test_oversized_transcript_is_failed_without_model_input(self):
        self.transcript('가상 거래처 A', '2029-12-31T22:00', '가' * 1400)
        url = self.submit()
        for _ in range(2):
            self.assertIsNone(self.api('claim', json={'rag_contract_version': 1}).json())
        self.assertIn('검색 범위에 준비하지 못한 전사가 있어요', self.http('GET', url).text)

    def test_question_continues_before_background_and_context_error_is_terminal(self):
        self.transcript('가상 거래처 A', '2029-12-31T22:00', '방문 준비')
        index, _ = self.claim_rag('RAG_EMBED')
        self.complete(index, self.embedding(unit(0)))
        url = self.submit()
        background = self.ingest()
        self.service.classify(background, 'BUSINESS_APPROVED')
        query, _ = self.claim_rag('RAG_EMBED')
        self.complete(query, self.embedding(unit(0)))
        answer, payload = self.claim_rag('RAG_ANSWER')
        self.assertEqual(self.api('start', answer, json={'request_id': str(uuid4())}).status_code, 200)
        self.assertEqual(self.api('failed', answer, json={'code': 'MODEL_ERROR'}).status_code, 200)
        self.clean()
        retry, retained = self.claim_rag('RAG_ANSWER')
        self.assertEqual(retry['input_snapshot_id'], answer['input_snapshot_id'])
        self.assertEqual(retry['input_sha256'], answer['input_sha256'])
        self.assertNotEqual(retry['lease_token'], answer['lease_token'])
        self.assertEqual(retained, payload)
        self.assertEqual(self.api('start', retry, json={'request_id': str(uuid4())}).status_code, 200)
        response = self.api('failed', retry, json={'code': 'RAG_CONTEXT_TOO_LONG'})
        self.assertEqual(response.status_code, 200)
        self.clean()
        self.assertIn('질문 범위를 확인해 주세요', self.http('GET', url).text)
        self.assertEqual(self.api('claim', json={'rag_contract_version': 1}).json()['stage'], 'STT')
