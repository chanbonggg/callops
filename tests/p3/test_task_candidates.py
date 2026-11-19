from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from callops.processing.fake_worker import FakeWorker
from tests.support import ProductCase


class TaskCandidateTests(ProductCase):
    def start_analysis(self, transport=None):
        recording = self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        worker = FakeWorker(transport or self.http, self.tokens['one'],
                            Path(self.temp.name) / 'worker', lambda: self.now)
        transcript = worker.once(transcript_text='검토한 견적서를 보내겠습니다.')['artifact_id']
        return recording, transcript, worker

    def candidate(self, **fields):
        return {'title': '가상 견적서 <전달>', 'due_at': None, 'segment_ids': [1]} | fields

    def rows(self, recording):
        with self.service.transaction() as conn:
            return conn.execute('SELECT * FROM callops.task_candidates WHERE recording_id=%s ORDER BY title',
                                (recording,)).fetchall()

    def review(self, recording):
        return f'/recordings/{recording}/task-candidates'

    def approval(self, csrf, candidate, **fields):
        return {'csrf': csrf, 'candidate_id': str(candidate['id']), 'revision': candidate['revision'],
                'action': 'approve', 'title': candidate['title'], 'due': ''} | fields

    def tasks(self):
        return self.http('GET', '/api/tasks').json()['tasks']

    def test_review_concurrent_approval_reject_and_task_lifecycle(self):
        recording, transcript, worker = self.start_analysis()
        proposal = self.candidate(due_at='2030-01-03T19:00:00+09:00')
        self.assertTrue(worker.once(tasks=[proposal, self.candidate(title='취소할 후보')])['accepted'])
        candidate, rejected = self.rows(recording)
        path = self.review(recording)
        self.assertEqual(self.http('GET', path).status_code, 401)
        csrf = self.login()
        self.assertIn(f'href="{path}"', self.http('GET', f'/recordings/{recording}').text)
        evidence = f'/recordings/{recording}/transcripts/{transcript}'
        page = self.http('GET', path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'href="{evidence}#segment-1"', page.text)
        self.assertIn('가상 견적서 &lt;전달&gt;', page.text)
        self.assertIn('검토한 견적서를 보내겠습니다.', self.http('GET', evidence).text)
        self.assertEqual(self.tasks(), [])

        approval = self.approval(csrf, candidate, title='검토한 견적서 발송')
        self.assertEqual(self.http('POST', path, data=approval | {'csrf': ''}).status_code, 403)
        self.assertEqual(self.http('POST', path, data=approval | {'revision': 99}).status_code, 409)
        self.assertEqual(self.http('POST', path, data=approval | {'due': '2030-02-30T09:00'}).status_code, 409)
        other = self.ingest()
        self.service.classify(other, 'BUSINESS_APPROVED')
        self.assertEqual(self.http('POST', self.review(other), data=approval).status_code, 409)
        barrier = Barrier(2)

        def approve():
            barrier.wait(timeout=10)
            return self.http('POST', path, data=approval)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = (pool.submit(approve), pool.submit(approve))
            self.assertEqual([future.result(timeout=15).status_code for future in results], [303, 303])
        task, = self.tasks()
        self.assertEqual((task['id'], task['title'], task['due_at'], task['status']),
                         (str(candidate['id']), approval['title'], None, 'OPEN'))
        self.assertIn(f'href="{evidence}#segment-1"', self.http('GET', '/tasks').text)
        self.assertEqual(self.http('POST', path, data=approval | {'title': '충돌한 승인 제목'}).status_code, 409)
        self.assertEqual(self.http('POST', path, data=approval | {'action': 'reject'}).status_code, 409)
        reject = self.approval(csrf, rejected, action='reject')
        self.assertEqual(self.http('POST', path, data=reject).status_code, 303)
        self.assertEqual(self.http('POST', path, data=reject | {'action': 'approve'}).status_code, 409)

        item = f'/tasks/{task["id"]}'
        change = {'csrf': csrf, 'revision': task['revision']}
        edit = change | {'action': 'edit', 'title': '사용자 수정', 'due': '2030-01-01T08:00'}
        self.assertEqual(self.http('POST', item, data=edit).status_code, 303)
        self.assertEqual(self.http('POST', item, data=edit).status_code, 409)
        self.assertIn(edit['title'], self.http('GET', '/tasks?overdue=1').text)
        for revision, action, status in [(2, 'complete', 'DONE'), (3, 'reopen', 'OPEN'),
                                         (4, 'cancel', 'CANCELLED')]:
            self.assertEqual(self.http('POST', item, data=change | {'action': action, 'revision': revision}).status_code, 303)
            current, = self.tasks()
            self.assertEqual((current['status'], current['title']), (status, edit['title']))
        approved = next(row for row in self.rows(recording) if row['id'] == candidate['id'])
        self.assertEqual((approved['title'], approved['due_at']), (approval['title'], None))

    def test_invalid_evidence_rejects_whole_analysis_result(self):
        errors = []

        def transport(method, path, **kwargs):
            response = self.http(method, path, **kwargs)
            if path.endswith('/result') and response.status_code != 200:
                errors.append((response.status_code, response.json()['error']))
            return response

        recording, _, worker = self.start_analysis(transport)
        with self.assertRaises(RuntimeError):
            worker.once(tasks=[self.candidate(), self.candidate(segment_ids=[2])])
        self.assertEqual(errors, [(409, 'INVALID_RESULT')])
        self.assertEqual(self.rows(recording), [])

    def test_expiry_retains_task_and_private_transition_removes_it(self):
        recording, transcript, worker = self.start_analysis()
        worker.once(tasks=[self.candidate(title='첫 번째 견적'), self.candidate(title='후속 견적')])
        approved, pending = self.rows(recording)
        csrf = self.login()
        path = self.review(recording)
        self.assertEqual(self.http('POST', path, data=self.approval(csrf, approved)).status_code, 303)
        self.advance(days=90)
        csrf = self.login()
        late = self.approval(csrf, pending)
        self.assertEqual(self.http('POST', path, data=late).status_code, 409)
        page = self.http('GET', '/tasks')
        self.assertIn('근거 만료', page.text)
        self.assertNotIn(f'/recordings/{recording}/transcripts/{transcript}', page.text)
        self.service.maintain()
        self.assertEqual(self.rows(recording), [])
        self.assertEqual(self.http('POST', path, data=late).status_code, 409)
        self.assertEqual([task['id'] for task in self.tasks()], [str(approved['id'])])
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.tasks(), [])
        item = f'/tasks/{approved["id"]}'
        self.assertEqual(self.http('GET', item).status_code, 404)
        self.assertEqual(self.http('POST', item, data={'csrf': csrf, 'action': 'complete',
                                                      'revision': 1}).status_code, 404)

    def test_reanalysis_can_link_existing_completed_task_without_overwriting(self):
        recording, transcript, worker = self.start_analysis()
        worker.once(tasks=[self.candidate()])
        original, = self.rows(recording)
        csrf = self.login()
        path = self.review(recording)
        self.assertEqual(self.http('POST', path, data=self.approval(csrf, original)).status_code, 303)
        task, = self.tasks()
        item = f'/tasks/{task["id"]}'
        edit = {'csrf': csrf, 'revision': 1, 'action': 'edit', 'title': '내가 확정한 제목', 'due': ''}
        self.assertEqual(self.http('POST', item, data=edit).status_code, 303)
        self.assertEqual(self.http('POST', item, data=edit | {'action': 'complete', 'revision': 2}).status_code, 303)
        output_version = self.row(recording)['output_decision_version']
        with self.service.transaction() as conn:
            conn.execute('''INSERT INTO callops.jobs (id,recording_id,stage,input_artifact_id,pipeline_version,retry_at)
                VALUES (%s,%s,'ANALYSIS',%s,2,%s)''', (uuid4(), recording, transcript, self.now))
        worker.once(tasks=[self.candidate(title='후속 분석이 제안한 제목')])
        _, followup = self.rows(recording)
        new = self.approval(csrf, followup)
        self.assertEqual(self.http('POST', path, data=new).status_code, 409)
        link = new | {'action': 'link', 'task_id': task['id'], 'output_version': output_version}
        response = self.http('POST', path, data=link)
        self.assertEqual((response.status_code, response.headers.get('location')), (303, item))
        current, = self.tasks()
        self.assertEqual((current['id'], current['title'], current['status'], current['revision']),
                         (task['id'], edit['title'], 'DONE', 3))
