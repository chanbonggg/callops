import json
from uuid import uuid4

from callops.common import digest
from tests.support import ProductCase


class RealResultCase(ProductCase):
    def prepare(self, job, *, worker='one', **fields):
        response = self.api('start', job, worker=worker, json={'request_id': str(uuid4())})
        self.assertEqual(response.status_code, 200)
        payload = {field: job[field] for field in ('job_id', 'input_artifact_id', 'input_artifact_version', 'input_sha256')}
        payload.update(attempt_id=job['copy_key'], synthetic=False, schema_version=2, processing_seconds=0.5)
        payload.update(fields)
        data = json.dumps(payload).encode()
        report = {'request_id': str(uuid4()), 'sha256': digest(data), 'size': len(data),
                  'spool_created_at': self.now.isoformat()}
        self.assertEqual(self.api('prepared', job, worker=worker, json=report).status_code, 200)
        return data, report

    def upload(self, job, data, report, *, worker='one'):
        return self.api('result', job, worker=worker, content=data,
                        headers={'X-Result-Request-ID': report['request_id']})

    def real_stt(self, job, *, worker='one', silent=False):
        segments = [] if silent else [
            {'segment_id': 1, 'start': 0.1, 'end': 0.4, 'text': '가상 일정과 자료를 확인했습니다.', 'speaker': 'A'},
        ]
        return self.prepare(job, worker=worker, segments=segments, warnings=['NO_SPEECH'] if silent else [],
                            model_name='large-v3', language='ko', duration_seconds=2)


class RealResultOptInTests(RealResultCase):
    def test_valid_real_result_needs_server_opt_in(self):
        recording = self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        job = self.api('claim').json()
        data, report = self.real_stt(job)
        self.assertEqual(self.upload(job, data, report).status_code, 409)
        self.assertEqual(len(self.jobs(recording)), 1)
        self.assertEqual(list(self.service.safe_path('artifacts').iterdir()), [])


class RealResultTests(RealResultCase):
    real_worker = 'one'

    def test_real_silence_is_readable_and_does_not_queue_analysis(self):
        recording = self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        job = self.api('claim').json()
        data, report = self.real_stt(job, silent=True)
        result = self.upload(job, data, report)
        self.assertEqual(result.status_code, 200)
        self.assertEqual([(item['stage'], item['status']) for item in self.jobs(recording)], [('STT', 'SUCCEEDED')])
        self.login()
        page = self.http('GET', '/recordings/' + recording)
        self.assertIn('음성 발화를 찾지 못했습니다.', page.text)

    def test_only_configured_worker_can_submit_real_results(self):
        self.assertEqual(self.http('GET', '/worker/readiness').status_code, 401)
        for worker, expected in (('one', True), ('two', False)):
            response = self.http('GET', '/worker/readiness', headers={'Authorization': 'Bearer ' + self.tokens[worker]})
            self.assertIs(response.json()['real_results'], expected)
        recording = self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        job = self.api('claim', worker='two').json()
        data, report = self.real_stt(job, worker='two')
        self.assertEqual(self.upload(job, data, report, worker='two').status_code, 409)

    def test_result_status_and_real_analysis_use_the_same_input_after_retry(self):
        recording = self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        stt = self.api('claim').json()
        data, report = self.real_stt(stt)
        key = {'request_id': report['request_id']}
        status = self.api('status', stt, json=key)
        self.assertEqual(status.json()['status'], 'UPLOADING')
        self.assertIn('X-CallOps-Server-Time', status.headers)
        ack = self.upload(stt, data, report).json()
        self.assertTrue(ack['accepted'])
        self.assertEqual(self.api('status', stt, json=key).json(), ack)
        self.assertEqual(self.api('status', stt, json=key, worker='two').status_code, 409)
        self.assertEqual(self.api('status', stt, json={'request_id': str(uuid4())}).status_code, 409)
        for item in self.api('cleanup', json={}).json():
            self.assertEqual(self.api('cleanup', json={**item, 'stopped': True, 'removed': True}).status_code, 200)
        model = {
            'model_name': 'qwen3:8b', 'prompt_version': 'analysis-v6', 'contract_mentions': [],
            'summary': {'text': '가상 요약', 'segment_ids': [1]},
            'events': [{'title': '가상 일정', 'start': None, 'end': None, 'time_hint': '내일', 'segment_ids': [1]}],
            'tasks': [{'title': '가상 자료 보내기', 'due_at': None, 'segment_ids': [1]}],
        }
        for expected, review_id in ((409, 2), (200, 1)):
            analysis = self.api('claim').json()
            self.assertEqual(analysis['input_artifact_id'], ack['artifact_id'])
            input_url = f"/worker/jobs/{analysis['job_id']}/input"
            input_response = self.http('GET', input_url, headers={
                'Authorization': 'Bearer ' + self.tokens['one'], 'X-Lease-Token': analysis['lease_token'],
            })
            self.assertEqual(input_response.content, data)
            self.assertEqual(input_response.headers['X-Input-Artifact-ID'], ack['artifact_id'])
            self.assertEqual(input_response.headers['X-Input-Artifact-Version'], str(analysis['input_artifact_version']))
            output, prepared = self.prepare(analysis, **model, needs_review=[{'reason': '일시 확인', 'segment_ids': [review_id]}])
            result = self.upload(analysis, output, prepared)
            self.assertEqual(result.status_code, expected)
            if expected == 409:
                self.assertEqual(self.api('failed', analysis, json={'code': 'UPLOAD_ERROR'}).status_code, 200)
                for item in self.api('cleanup', json={}).json():
                    self.api('cleanup', json={**item, 'stopped': True, 'removed': True})
                self.advance(minutes=1)
        self.assertEqual([(job['stage'], job['status']) for job in self.jobs(recording)],
                         [('ANALYSIS', 'SUCCEEDED'), ('STT', 'SUCCEEDED')])
        source = f"/recordings/{recording}/transcripts/{ack['artifact_id']}"
        csrf = self.login()
        detail = self.http('GET', '/recordings/' + recording).text
        self.assertIn('AI 요약', detail)
        self.assertIn('일시 확인', detail)
        self.assertIn(source + '#segment-1', detail)
        self.assertIn('화자 A', detail)
        for page, title in (('/schedule', '가상 일정'), ('/task-candidates', '가상 자료 보내기')):
            candidate = self.http('GET', '/recordings/' + recording + page)
            self.assertEqual(candidate.status_code, 200)
            self.assertIn(title, candidate.text)
            self.assertIn(source + '#segment-1', candidate.text)
            self.assertNotIn('합성 전사', candidate.text)
        matches = self.http('POST', '/search', data={'q': '가상 일정', 'csrf': csrf})
        self.assertEqual(matches.status_code, 200)
        self.assertIn(source + '#segment-1', matches.text)
        self.assertNotIn('합성 전사', matches.text)
