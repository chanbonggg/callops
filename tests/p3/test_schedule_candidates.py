import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from callops.common import digest
from callops.processing.fake_worker import FakeWorker
from callops.schedule.service import store_candidates
from tests.support import ProductCase


class ScheduleCandidateTests(ProductCase):
    def worker(self):
        return FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)

    def event(self, **fields):
        return {'title': '가상 거래처 방문 <검토>', 'start': '2030-01-03T14:00:00+09:00',
                'end': None, 'time_hint': '1월 3일 오후 2시', 'segment_ids': [1]} | fields

    def prepare_analysis(self, events, *, recording=None, synthetic=True):
        recording = recording or self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        stt = self.worker().once(transcript_text='1월 3일 오후 2시에 견적서 검토하러 방문할게요.')
        self.assertTrue(stt['accepted'])
        claim = self.api('claim')
        self.assertEqual(claim.status_code, 200)
        job = claim.json()
        self.assertEqual(job['stage'], 'ANALYSIS')
        self.assertEqual(self.api('start', job, json={'request_id': str(uuid4())}).status_code, 200)
        envelope = {key: job[key] for key in ('job_id', 'input_sha256', 'input_artifact_id', 'input_artifact_version')}
        data = json.dumps(envelope | {'attempt_id': job['copy_key'], 'schema_version': 2,
                                     'synthetic': synthetic, 'events': events}).encode()
        report = {'request_id': str(uuid4()), 'sha256': digest(data), 'size': len(data),
                  'spool_created_at': self.now.isoformat()}
        self.assertEqual(self.api('prepared', job, json=report).status_code, 200)
        return recording, stt['artifact_id'], job, data, report

    def upload(self, job, data, report):
        return self.api('result', job, content=data, headers={'X-Result-Request-ID': report['request_id']})

    def review(self, recording):
        return f'/recordings/{recording}/schedule'

    def candidates(self, recording):
        with self.service.transaction() as conn:
            return conn.execute('SELECT * FROM callops.schedule_candidates WHERE recording_id=%s ORDER BY position',
                                (recording,)).fetchall()

    def calendar_state(self, event_id):
        with self.service.transaction() as conn:
            return conn.execute('''SELECT d.*, e.event_id, e.payload_sha256 AS event_sha256
                FROM callops.calendar_deliveries d LEFT JOIN callops.mock_calendar_events e ON e.event_id=d.logical_item_id
                WHERE d.logical_item_id=%s''', (event_id,)).fetchone()

    def remove_manual_schedule(self, request_id):
        with self.service.transaction() as conn:
            conn.execute("DELETE FROM callops.schedules WHERE source='manual' AND request_id=%s", (request_id,))

    def test_manual_schedule_web_lifecycle_and_stale_edit(self):
        create = '/schedules/manual/new'
        self.assertEqual(self.http('GET', create).status_code, 401)
        csrf = self.login()
        self.assertIn(f'href="{create}"', self.http('GET', '/schedules').text)
        page = self.http('GET', create)
        self.assertEqual(page.status_code, 200)
        request_id = re.search(r'name="request_id" value="([^"]+)"', page.text)[1]
        self.addCleanup(self.remove_manual_schedule, request_id)
        form = {'request_id': request_id, 'title': '<가상 검토 일정>',
                'start': '2030-01-02T23:55:15', 'end': '2030-01-03T00:15'}
        self.assertEqual(self.http('POST', create, data=form).status_code, 403)
        form['csrf'] = csrf
        bad_end = '2030-01-02T23:40'
        invalid = self.http('POST', create, data=form | {'end': bad_end})
        self.assertEqual(invalid.status_code, 409)
        for value in (request_id, '&lt;가상 검토 일정&gt;', form['start'], bad_end):
            self.assertIn(f'value="{value}"', invalid.text)

        for _ in range(2):
            response = self.http('POST', create, data=form)
            self.assertEqual((response.status_code, response.headers['location']),
                             (303, '/schedules?date=2030-01-02'))
        schedule, = self.http('GET', '/api/schedules').json()['schedules']
        self.assertEqual(schedule['start_at'], '2030-01-02T14:55:15+00:00')
        path = f'/schedules/manual/{schedule["id"]}'
        self.assertIn(f'href="{path}"', self.http('GET', '/schedules?date=2030-01-02').text)
        edit_page = self.http('GET', path)
        self.assertIn(f'value="{form["start"]}"', edit_page.text)
        self.assertIn(f'href="{path}/delete"', edit_page.text)
        revision = re.search(r'name="revision" value="([^"]+)"', edit_page.text)[1]
        edit = form | {'revision': revision, 'title': '수정한 검토 일정',
                       'start': '2030-02-01T00:05', 'end': '2030-02-02T00:00'}
        response = self.http('POST', path, data=edit)
        self.assertEqual((response.status_code, response.headers['location']), (303, '/schedules?date=2030-02-01'))
        february = self.http('GET', response.headers['location']).text
        self.assertIn('2030년 2월 1일 금요일, 일정 1개', february)
        self.assertIn('2030년 2월 2일 토요일, 일정 0개', february)
        self.assertIn(edit['title'], february)
        self.assertNotIn(edit['title'], self.http('GET', '/schedules?date=2030-01-31').text)
        stale = self.http('POST', path, data=edit | {'title': '이전 탭 제목'})
        self.assertEqual(stale.status_code, 409)
        self.assertIn('href="/schedules"', stale.text)
        self.assertNotIn('name="revision"', stale.text)

        delete = f'{path}/delete'
        confirm = self.http('GET', delete)
        self.assertEqual(confirm.status_code, 200)
        self.assertIn(edit['title'], confirm.text)
        latest_revision = re.search(r'name="revision" value="([^"]+)"', confirm.text)[1]
        self.assertEqual(self.http('POST', delete, data={'csrf': csrf, 'revision': revision}).status_code, 409)
        self.assertEqual(self.http('POST', delete, data={'revision': latest_revision}).status_code, 403)
        response = self.http('POST', delete, data={'csrf': csrf, 'revision': latest_revision})
        self.assertEqual((response.status_code, response.headers['location']), (303, '/schedules'))
        self.assertEqual(self.http('POST', path, data=edit | {'revision': latest_revision}).status_code, 409)
        self.assertEqual(self.http('GET', path).status_code, 404)
        self.assertEqual(self.http('GET', '/api/schedules').json()['schedules'], [])

    def test_manual_schedule_crud_shares_list_and_keeps_approved_call(self):
        event = self.event(end='2030-01-03T15:00:00+09:00')
        recording, artifact, job, data, report = self.prepare_analysis([event])
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        csrf = self.login()
        candidate, = self.candidates(recording)
        self.assertEqual(self.http('POST', f'{self.review(recording)}/{candidate["id"]}',
                                   data={'csrf': csrf, 'revision': 1, 'action': 'approve'}).status_code, 303)
        response = self.http('GET', '/api/schedules')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['csrf'], csrf)
        form = {'csrf': csrf, 'request_id': str(candidate['id']), 'title': '가상 일정 <직접>',
                'start': '2030-01-02T23:55', 'end': '2030-01-03T00:15'}
        response = self.http('POST', '/api/schedules/manual', data=form)
        self.assertEqual(response.status_code, 201)
        manual_id = response.json()['id']
        self.assertNotEqual(manual_id, form['request_id'])
        self.assertEqual(response.json()['start_at'], '2030-01-02T14:55:00+00:00')
        replay = self.http('POST', '/api/schedules/manual', data=form)
        self.assertEqual((replay.status_code, replay.json()['id']), (200, manual_id))
        self.assertEqual(self.http('POST', '/api/schedules/manual', data=form | {'title': '겹친 요청'}).status_code, 409)

        manual, call = self.http('GET', '/api/schedules').json()['schedules']
        self.assertEqual((manual['source'], manual['evidence'], call['source']), ('manual', [], 'call'))
        self.assertIn(f'/recordings/{recording}/transcripts/{artifact}#segment-1', call['evidence'])
        today = self.http('GET', '/schedules?date=2030-02-30').text
        self.assertIn('<h2 id="agenda-date">1월 1일 화요일</h2>', today)
        self.assertIn('이 날짜에 일정이 없습니다.', today)
        self.assertIn('href="/schedules?date=2029-12-01" aria-label="이전 달"', today)
        self.assertIn('href="/schedules?date=2030-02-01" aria-label="다음 달"', today)
        selected = self.http('GET', '/schedules?date=2030-01-03').text
        self.assertIn('<h2 id="agenda-date">1월 3일 목요일</h2>', selected)
        self.assertIn('2030년 1월 3일 목요일, 일정 2개', selected)
        self.assertLess(selected.index('가상 일정 &lt;직접&gt;'), selected.index('가상 거래처 방문 &lt;검토&gt;'))
        self.assertIn('직접 입력</span>', selected)
        self.assertIn('통화에서 승인</span>', selected)
        self.assertIn(f'href="{call["evidence"][0]}"', selected)
        self.assertEqual(self.http('POST', f'/schedules/{manual_id}/send', data={'csrf': csrf, 'revision': 1}).status_code, 409)
        self.assertIsNone(self.calendar_state(manual_id))

        path = f'/api/schedules/manual/{manual_id}'
        edit = form | {'action': 'edit', 'revision': 1, 'title': '수정된 가상 일정'}
        response = self.http('POST', path, data=edit)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()['title'], response.json()['revision']), ('수정된 가상 일정', 2))
        self.assertEqual(self.http('POST', path, data=edit).status_code, 409)
        delete = {'csrf': csrf, 'action': 'delete', 'revision': 2}
        self.assertEqual(self.http('POST', f'/api/schedules/manual/{candidate["id"]}', data=delete).status_code, 404)
        self.assertEqual(self.http('POST', f'/api/schedules/manual/{call["id"]}', data=delete).status_code, 404)
        self.assertEqual(self.http('POST', path, data=delete | {'revision': 1}).status_code, 409)
        self.assertEqual(self.http('POST', path, data=delete).status_code, 204)
        self.assertEqual(self.http('POST', path, data=delete).status_code, 204)
        self.assertEqual(self.http('POST', '/api/schedules/manual', data=form).status_code, 409)
        remaining, = self.http('GET', '/api/schedules').json()['schedules']
        approved, = self.candidates(recording)
        self.assertEqual(remaining['id'], str(approved['logical_item_id']))
        self.assertEqual(remaining['candidate_id'], str(candidate['id']))
        with self.service.transaction() as conn:
            removed = conn.execute('SELECT title,start_at,end_at FROM callops.schedules WHERE id=%s',
                                   (manual_id,)).fetchone()
        self.assertEqual(tuple(removed.values()), (None, None, None))

    def test_call_schedule_survives_draft_edit_and_evidence_expiry(self):
        event = self.event(end='2030-01-03T15:00:00+09:00')
        recording, _, job, data, report = self.prepare_analysis([event])
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        csrf = self.login()
        candidate, = self.candidates(recording)
        review = f'{self.review(recording)}/{candidate["id"]}'
        send = f'/schedules/{candidate["id"]}/send'
        form = {'csrf': csrf, 'revision': 1}
        self.assertEqual(self.http('POST', review, data=form | {'action': 'approve'}).status_code, 303)
        self.assertEqual(self.http('POST', send, data=form).status_code, 303)
        approved, = self.candidates(recording)
        schedule_id = str(approved['logical_item_id'])

        edit = {'action': 'edit', 'status': 'APPROVED', 'title': '수정한 가상 일정',
                'start': '2030-01-03T16:00', 'end': '2030-01-03T17:00'}
        self.assertEqual(self.http('POST', review, data=form | edit).status_code, 303)
        draft, = self.http('GET', '/api/schedules').json()['schedules']
        self.assertEqual((draft['id'], draft['title'], draft['sendable']), (schedule_id, event['title'], False))
        self.assertEqual(draft['calendar_status'], 'SUCCEEDED')
        self.assertEqual(self.http('POST', send, data=form).status_code, 409)

        self.assertEqual(self.http('POST', review, data=form | {'revision': 2, 'action': 'approve'}).status_code, 303)
        current, = self.http('GET', '/api/schedules').json()['schedules']
        self.assertEqual((current['id'], current['title'], current['revision']), (schedule_id, edit['title'], 2))
        self.assertEqual(current['calendar_event_id'], schedule_id)
        self.assertEqual(current['calendar_revision'], 1)

        self.advance(days=90)
        self.service.maintain()
        self.login()
        expired, = self.http('GET', '/api/schedules').json()['schedules']
        self.assertEqual((expired['id'], expired['evidence'], expired['agreement']), (schedule_id, [], None))
        self.assertEqual(expired['calendar_event_id'], schedule_id)
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.http('GET', '/api/schedules').json()['schedules'], [])
        self.assertEqual(str(self.calendar_state(schedule_id)['event_id']), schedule_id)
        with self.service.transaction() as conn:
            retired = conn.execute('SELECT recording_id,title,start_at,end_at FROM callops.schedules WHERE id=%s',
                                   (schedule_id,)).fetchone()
        self.assertEqual(tuple(retired.values()), (None, None, None, None))

    def test_manual_schedule_rejects_invalid_fields_and_unauthorized_writes(self):
        path = '/api/schedules/manual'
        form = {'request_id': str(uuid4()), 'title': '가상 방문',
                'start': '2030-02-28T23:59:59', 'end': '2030-03-01T00:00:01'}
        self.assertEqual(self.http('GET', '/api/schedules').status_code, 401)
        self.assertEqual(self.http('POST', path, data=form).status_code, 401)
        self.assertEqual(self.http('POST', path, data=form, headers={
            'Authorization': 'Bearer ' + self.tokens['one'],
        }).status_code, 401)
        csrf = self.login()
        self.assertEqual(self.http('POST', path, data=form).status_code, 403)
        for changes in ({'title': '  '}, {'start': '2030-02-29T23:59'}, {'end': ''},
                        {'end': form['start']}, {'start': '2030-02-28T23:59:59+09:00'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.http('POST', path, data=form | changes | {'csrf': csrf}).status_code, 409)
        self.assertEqual(self.http('GET', '/api/schedules').json()['schedules'], [])

    def test_synthetic_upload_review_edit_approve_revise_and_reject(self):
        recording, artifact, job, data, report = self.prepare_analysis([self.event()])
        accepted = self.upload(job, data, report)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.upload(job, data, report).json(), accepted.json())
        candidate, = self.candidates(recording)
        self.assertEqual(candidate['status'], 'NEEDS_CONFIRMATION')
        path = self.review(recording)
        action = f"{path}/{candidate['id']}"
        self.assertEqual(self.http('GET', path).status_code, 401)
        csrf = self.login()
        detail = self.http('GET', f'/recordings/{recording}')
        self.assertIn(path, detail.text)
        page = self.http('GET', path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'/recordings/{recording}/transcripts/{artifact}#segment-1', page.text)
        self.assertIn('가상 거래처 방문 &lt;검토&gt;', page.text)
        self.assertIn('1월 3일 오후 2시', page.text)
        self.assertIn('aria-current="page"', page.text)
        self.assertNotIn('name="action" value="approve"', page.text)
        self.assertNotIn('name="start"', page.text)
        edit_path = f"{path}?edit={candidate['id']}"
        self.assertIn(f'href="{edit_path}#candidate-{candidate["id"]}"', page.text)
        edit_page = self.http('GET', edit_path)
        self.assertIn('name="start" type="datetime-local"', edit_page.text)
        self.assertIn('name="action" value="edit"', edit_page.text)
        self.assertEqual(self.http('GET', action).status_code, 405)
        self.assertEqual(self.http('POST', action, data={'action': 'approve'}).status_code, 403)
        base = {'csrf': csrf, 'revision': 1, 'status': candidate['status']}
        self.assertEqual(self.http('POST', action, data=base | {'action': 'approve'}).status_code, 409)
        edit = {'action': 'edit', 'title': '견적 검토 방문',
                'start': '2030-01-03T14:00', 'end': '2030-01-03T15:00'}
        invalid = self.http('POST', f"{action}?edit={candidate['id']}", data=base | edit | {'end': edit['start']})
        self.assertEqual(invalid.status_code, 409)
        self.assertIn('role="alert"', invalid.text)
        self.assertIn('name="action" value="edit"', invalid.text)
        self.assertEqual(self.http('POST', action, data=base | edit).status_code, 303)
        saved = self.http('GET', path).text
        self.assertIn('value="2030-01-03 15:00"', saved)
        self.assertIn('name="action" value="approve"', saved)
        self.assertEqual(self.http('POST', action, data=base | {'action': 'approve'}).status_code, 409)
        base |= {'revision': 2, 'status': 'AWAITING_APPROVAL'}
        approval = base | {'action': 'approve'}
        self.assertEqual(self.http('POST', action, data=approval).status_code, 303)
        self.assertEqual(self.http('POST', action, data=approval).status_code, 303)
        self.assertEqual(self.http('POST', action, data=base | edit | {'title': '오래된 화면'}).status_code, 409)
        approved, = self.candidates(recording)
        self.assertEqual(approved['status'], 'APPROVED')
        self.assertEqual(self.row(recording)['output_decision_version'], 1)
        with self.service.transaction() as conn:
            snapshot, = conn.execute('SELECT * FROM callops.schedule_approvals WHERE candidate_id=%s',
                                     (candidate['id'],)).fetchall()
        self.assertEqual((snapshot['revision'], snapshot['actor']), (2, 'owner'))
        self.assertEqual(snapshot['snapshot']['start_at'], '2030-01-03T05:00:00+00:00')
        self.assertEqual(self.http('POST', action, data=base | edit | {'status': 'APPROVED', 'title': '변경 후 방문'}).status_code, 303)
        rejection = base | {'revision': 3, 'action': 'reject'}
        self.assertEqual(self.http('POST', action, data=rejection).status_code, 303)
        self.assertEqual(self.http('POST', action, data=rejection).status_code, 303)
        rejected, = self.candidates(recording)
        self.assertEqual((rejected['status'], rejected['logical_item_id']), ('REJECTED', approved['logical_item_id']))
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertEqual(self.http('GET', path).status_code, 409)
        self.assertEqual(self.candidates(recording), [])
        with self.service.transaction() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM callops.schedule_approvals WHERE candidate_id=%s',
                                           (candidate['id'],)).fetchone())

    def test_invalid_worker_evidence_rejects_whole_upload(self):
        recording, _, job, data, report = self.prepare_analysis([self.event(), self.event(segment_ids=[2])])
        response = self.upload(job, data, report)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'INVALID_RESULT')
        self.assertEqual(self.candidates(recording), [])
        self.assertEqual(next(row['status'] for row in self.jobs(recording) if row['stage'] == 'ANALYSIS'), 'UPLOADING')

    def test_approved_schedule_list_and_mock_send_require_current_approval(self):
        from callops.schedule.calendar import commit_mock_event

        approved = self.event(title='가상 방문 <승인>', end='2030-01-03T15:00:00+09:00')
        waiting = approved | {'title': '가상 미승인'}
        rejected = approved | {'title': '가상 거절'}
        recording, artifact, job, data, report = self.prepare_analysis([approved, waiting, rejected])
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        self.assertEqual(self.http('GET', '/schedules').status_code, 401)
        csrf = self.login()
        self.assertIn('href="/schedules"', self.http('GET', '/').text)
        first, second, third = self.candidates(recording)
        form = {'csrf': csrf, 'revision': 1}
        review = self.review(recording)
        for candidate, action in ((first, 'approve'), (third, 'reject')):
            self.assertEqual(self.http('POST', f'{review}/{candidate["id"]}',
                                       data=form | {'action': action}).status_code, 303)
        first, _, _ = self.candidates(recording)
        page = self.http('GET', '/schedules?date=2030-01-03')
        self.assertEqual(page.status_code, 200)
        self.assertIn('가상 방문 &lt;승인&gt;', page.text)
        self.assertIn(f'/recordings/{recording}/transcripts/{artifact}#segment-1', page.text)
        manual_path = f'/schedules/manual/{first["logical_item_id"]}'
        self.assertNotIn(f'href="{manual_path}"', page.text)
        self.assertEqual(self.http('GET', manual_path).status_code, 404)
        self.assertNotIn('가상 미승인', page.text)
        self.assertNotIn('가상 거절', page.text)
        for blocked in (second, third):
            self.assertEqual(self.http('POST', f'/schedules/{blocked["id"]}/send', data=form).status_code, 409)
        path = f'/schedules/{first["id"]}/send?date=2030-01-03'
        self.assertEqual(self.http('GET', path).status_code, 405)
        self.assertEqual(self.http('POST', path, data={'revision': 1}).status_code, 403)
        self.assertEqual(self.http('POST', path, data=form | {'revision': 2}).status_code, 409)
        self.assertIsNone(self.calendar_state(first['logical_item_id']))
        barrier = Barrier(2)

        def concurrent_commit(*args):
            barrier.wait(timeout=10)
            return commit_mock_event(*args)

        with patch('callops.schedule.calendar.commit_mock_event', side_effect=concurrent_commit):
            with ThreadPoolExecutor(max_workers=2) as pool:
                requests = [pool.submit(self.http, 'POST', path, data=form) for _ in range(2)]
                for request in requests:
                    response = request.result()
                    self.assertEqual((response.status_code, response.headers['location']),
                                     (303, '/schedules?date=2030-01-03'))
        self.assertIn('모의 등록 완료', self.http('GET', '/schedules?date=2030-01-03').text)
        delivery = self.calendar_state(first['logical_item_id'])
        self.assertEqual((delivery['event_id'], delivery['status']), (first['logical_item_id'], 'SUCCEEDED'))

    def test_mock_timeout_reconciles_existing_event_before_retrying(self):
        from callops.schedule.calendar import commit_mock_event

        event = self.event(end='2030-01-03T15:00:00+09:00')
        recording, _, job, data, report = self.prepare_analysis([event, event | {'title': '다른 가상 방문'}])
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        csrf = self.login()
        form = {'csrf': csrf, 'revision': 1}

        def lost_response(*args):
            commit_mock_event(*args)
            raise TimeoutError('synthetic lost acknowledgement')

        for candidate, registered in zip(self.candidates(recording), (True, False), strict=True):
            with self.subTest(registered_before_timeout=registered):
                review = f'{self.review(recording)}/{candidate["id"]}'
                send = f'/schedules/{candidate["id"]}/send'
                self.assertEqual(self.http('POST', review, data=form | {'action': 'approve'}).status_code, 303)
                row = next(row for row in self.candidates(recording) if row['id'] == candidate['id'])
                event_id = row['logical_item_id']
                timeout = lost_response if registered else TimeoutError('synthetic failure before registration')
                with patch('callops.schedule.calendar.commit_mock_event', side_effect=timeout):
                    self.assertEqual(self.http('POST', send, data=form).status_code, 303)
                before = self.calendar_state(event_id)
                self.assertEqual(before['status'], 'UNKNOWN')
                self.assertEqual(before['event_id'], event_id if registered else None)
                self.assertIn('전송 결과 확인 필요', self.http('GET', '/schedules?date=2030-01-03').text)

                edit = {'action': 'edit', 'status': 'APPROVED', 'title': '보정한 가상 방문',
                        'start': '2030-01-03T14:00', 'end': '2030-01-03T16:00'}
                self.assertEqual(self.http('POST', review, data=form | edit).status_code, 303)
                self.assertEqual(self.http('POST', send, data=form).status_code, 409)
                self.assertEqual(self.http('POST', review, data=form | {'revision': 2, 'action': 'approve'}).status_code, 303)
                current = form | {'revision': 2}
                self.assertEqual(self.http('POST', send, data=current).status_code, 303)
                self.assertEqual(self.http('POST', send, data=current).status_code, 303)
                after = self.calendar_state(event_id)
                self.assertEqual((after['event_id'], after['status'], after['revision']),
                                 (event_id, 'SUCCEEDED', 1 if registered else 2))
                self.assertEqual(after['event_sha256'], after['payload_sha256'])
                if registered:
                    self.assertEqual(after['event_sha256'], before['event_sha256'])
                    self.assertIn('등록 이후 승인 내용이 변경됐습니다.',
                                  self.http('GET', '/schedules?date=2030-01-03').text)
                else:
                    payload = {'title': '보정한 가상 방문', 'start_at': '2030-01-03T05:00:00+00:00',
                               'end_at': '2030-01-03T07:00:00+00:00'}
                    self.assertEqual(after['event_sha256'], digest(json.dumps(payload, ensure_ascii=False,
                                                                             sort_keys=True).encode('utf-8')))

    def test_mock_creation_rechecks_privacy_and_rejects_past_schedule(self):
        from callops.schedule.calendar import commit_mock_event

        past = self.event(start='2030-01-01T09:00:01+09:00', end='2030-01-01T09:30:00+09:00')
        future = self.event(end='2030-01-03T15:00:00+09:00')
        recording, _, job, data, report = self.prepare_analysis([past, future])
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        csrf = self.login()
        form = {'csrf': csrf, 'revision': 1}
        for candidate in self.candidates(recording):
            self.assertEqual(self.http('POST', f'{self.review(recording)}/{candidate["id"]}',
                                       data=form | {'action': 'approve'}).status_code, 303)
        first, second = self.candidates(recording)
        self.advance(seconds=2)
        self.assertEqual(self.http('POST', f'/schedules/{first["id"]}/send', data=form).status_code, 409)
        self.assertIsNone(self.calendar_state(first['logical_item_id']))

        def private_before_creation(*args):
            self.service.classify(recording, 'PRIVATE_EXCLUDED')
            return commit_mock_event(*args)

        with patch('callops.schedule.calendar.commit_mock_event', side_effect=private_before_creation):
            self.assertEqual(self.http('POST', f'/schedules/{second["id"]}/send', data=form).status_code, 409)
        self.assertIsNone(self.calendar_state(second['logical_item_id'])['event_id'])
        for day in ('2030-01-01', '2030-01-03'):
            self.assertNotIn('가상 거래처 방문', self.http('GET', f'/schedules?date={day}').text)

    def test_duplicate_event_keeps_one_candidate_and_distinct_times_remain(self):
        event = self.event(end='2030-01-03T15:00:00+09:00')
        same_in_utc = event | {'start': '2030-01-03T05:00Z', 'end': '2030-01-03T06:00Z'}
        different = event | {'start': '2030-01-03T16:00:00+09:00', 'end': '2030-01-03T17:00:00+09:00'}
        recording, _, job, data, report = self.prepare_analysis([event, same_in_utc, different])
        accepted = self.upload(job, data, report)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.upload(job, data, report).json(), accepted.json())
        first, second = self.candidates(recording)
        self.assertEqual([row['start_at'].isoformat() for row in (first, second)],
                         ['2030-01-03T05:00:00+00:00', '2030-01-03T07:00:00+00:00'])

    def test_lease_expiring_during_result_save_rolls_back_candidates(self):
        recording, transcript, job, data, report = self.prepare_analysis([self.event()])
        self.advance(minutes=4, seconds=59)
        fsync = os.fsync

        def finish_after_expiry(fd):
            fsync(fd)
            self.advance(seconds=2)

        with patch('callops.processing.queue.os.fsync', side_effect=finish_after_expiry):
            response = self.upload(job, data, report)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'INVALID_LEASE')
        self.assertEqual(self.candidates(recording), [])
        self.assertEqual([path.name for path in self.service.safe_path('artifacts').iterdir()], [transcript])
        self.assertEqual(next(row['status'] for row in self.jobs(recording) if row['stage'] == 'ANALYSIS'), 'UPLOADING')

    def test_failed_candidate_write_rolls_back_result_and_allows_retry(self):
        recording, _, job, data, report = self.prepare_analysis([self.event()])

        def fail_before_commit(*args):
            store_candidates(*args)
            raise OSError('Synthetic persistence failure')

        with patch('callops.processing.queue.store_candidates', side_effect=fail_before_commit):
            self.assertEqual(self.upload(job, data, report).status_code, 503)
        self.assertEqual(self.candidates(recording), [])
        self.service.recover()
        self.assertEqual(len(list(self.service.safe_path('artifacts').iterdir())), 1)
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        self.assertEqual(len(self.candidates(recording)), 1)

    def test_fake_worker_can_supply_schedule_fixture_without_approval(self):
        recording = self.ingest()
        path = self.review(recording)
        self.login()
        self.assertEqual(self.http('GET', path).status_code, 409)
        self.service.classify(recording, 'BUSINESS_APPROVED')
        worker = self.worker()
        self.assertTrue(worker.once(transcript_text='견적서 설명은 내일로 하죠. 시간은 다시 잡아요.')['accepted'])
        self.assertTrue(worker.once(events=[self.event(start=None, time_hint='내일')])['accepted'])
        candidate, = self.candidates(recording)
        self.assertEqual((candidate['status'], candidate['start_at'], candidate['end_at']),
                         ('NEEDS_CONFIRMATION', None, None))
        self.assertIn('내일', self.http('GET', path).text)

    def test_past_event_requires_confirmation_and_expired_source_is_hidden(self):
        event = self.event(start='2029-12-31T14:00:00+09:00', end='2029-12-31T15:00:00+09:00')
        recording, artifact, job, data, report = self.prepare_analysis([event])
        self.assertEqual(self.upload(job, data, report).status_code, 200)
        candidate, = self.candidates(recording)
        self.assertEqual(candidate['status'], 'AWAITING_APPROVAL')
        csrf = self.login()
        path = self.review(recording)
        action = f"{path}/{candidate['id']}"
        fields = {'csrf': csrf, 'revision': 1, 'status': candidate['status'], 'action': 'approve'}
        self.assertIn('이미 시작 시각이 지났습니다', self.http('GET', path).text)
        self.assertEqual(self.http('POST', action, data=fields).status_code, 409)
        other = self.ingest()
        self.assertEqual(self.http('POST', f"{self.review(other)}/{candidate['id']}",
                                   data=fields | {'confirm_past': 'yes'}).status_code, 409)
        evidence = self.service.safe_path('artifacts', artifact)
        original = evidence.read_bytes()
        evidence.write_bytes(original + b' ')
        self.assertEqual(self.http('POST', action, data=fields | {'confirm_past': 'yes'}).status_code, 409)
        evidence.write_bytes(original)
        self.assertEqual(self.http('POST', action, data=fields | {'confirm_past': 'yes'}).status_code, 303)
        self.advance(days=90)
        csrf = self.login()
        self.assertNotIn(event['title'], self.http('GET', path).text)
        self.assertEqual(self.http('POST', action, data=fields | {'csrf': csrf, 'confirm_past': 'yes'}).status_code, 409)
        self.service.maintain()
        self.assertEqual(self.candidates(recording), [])
