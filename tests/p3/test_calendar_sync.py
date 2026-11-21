from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from psycopg import OperationalError

from callops.common import Conflict
from callops.processing.fake_worker import FakeWorker
from tests.support import ProductCase


LOCAL_EDIT = {'title': '웹에서 바꾼 가상 방문', 'start': '2030-01-04T23:59:59', 'end': '2030-01-05T00:30'}
REMOTE_EDIT = {'title': '모의 원격 <변경>', 'start': '2030-01-04T06:30:00-08:00', 'end': '2030-01-04T07:30:00-08:00'}


class CalendarSyncTests(ProductCase):
    def approved_schedule(self, *, link=True):
        recording = self.ingest()
        self.service.classify(recording, 'BUSINESS_APPROVED')
        worker = FakeWorker(self.http, self.tokens['one'], Path(self.temp.name) / 'worker', lambda: self.now)
        worker.once(transcript_text='1월 3일 오후 2시에 방문하겠습니다.')
        worker.once(events=[{'title': '가상 방문', 'start': '2030-01-03T14:00:00+09:00',
                             'end': '2030-01-03T15:00:00+09:00', 'time_hint': '1월 3일 오후 2시',
                             'segment_ids': [1]}])
        with self.service.transaction() as conn:
            candidate, = conn.execute('SELECT id FROM callops.schedule_candidates WHERE recording_id=%s',
                                      (recording,)).fetchall()
        review = f'/recordings/{recording}/schedule/{candidate["id"]}'
        send = f'/schedules/{candidate["id"]}/send'
        self.csrf = self.login()
        form = {'csrf': self.csrf, 'revision': 1}
        self.assertEqual(self.http('POST', review, data=form | {'action': 'approve'}).status_code, 303)
        with self.service.transaction() as conn:
            schedule, = conn.execute('SELECT id FROM callops.schedules WHERE recording_id=%s',
                                     (recording,)).fetchall()
        if link:
            self.assertEqual(self.http('POST', send, data=form).status_code, 303)
        return str(schedule['id']), recording, review, send

    def current_schedule(self, schedule_id):
        response = self.http('GET', '/api/schedules')
        self.assertEqual(response.status_code, 200)
        return next((row for row in response.json()['schedules'] if row['id'] == schedule_id), None)

    def calendar(self, schedule_id):
        response = self.http('GET', f'/api/schedules/{schedule_id}/calendar')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def sync(self, state, action, **fields):
        form = {'csrf': self.csrf, 'action': action, 'revision': state['revision'],
                'remote_version': state['remote_version']} | fields
        return self.http('POST', f'/api/schedules/{state["id"]}/calendar', data=form)

    def applied(self, state, action, **fields):
        response = self.sync(state, action, **fields)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def remote_change(self, state, action, **fields):
        from callops.schedule.calendar import change_mock_event

        change_mock_event(self.service, state['id'], {
            'action': action, 'remote_version': str(state['remote_version']),
        } | fields)

    def reapprove(self, review, revision, **fields):
        form = {'csrf': self.csrf, 'action': 'edit', 'revision': revision, 'status': 'APPROVED'} | fields
        self.assertEqual(self.http('POST', review, data=form).status_code, 303)
        self.assertEqual(self.http('POST', review, data={
            'csrf': self.csrf, 'action': 'approve', 'revision': revision + 1,
        }).status_code, 303)

    def assert_rejected(self, response, error):
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], error)

    def assert_deleted(self, state, agreement):
        self.assertEqual(state['status'], 'deleted')
        self.assertTrue(state['remote_deleted'])
        self.assertIsNone(state['current'])
        self.assertIsNone(state['remote'])
        self.assertEqual(state['agreement'], agreement)
        self.assertIsNone(self.current_schedule(state['id']))

    def test_linked_detail_imports_remote_time_and_preserves_the_original_quote(self):
        schedule_id, recording, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        path = f'/schedules/{schedule_id}/calendar?date=2030-01-03'
        calendar = self.http('GET', '/schedules?date=2030-01-03')
        self.assertIn(f'href="{path}"', calendar.text)
        self.remote_change(original, 'edit', title='가상 방문', start='2030-01-03T15:00:00+09:00',
                           end='2030-01-03T16:00:00+09:00')
        pending = self.calendar(schedule_id)
        page = self.http('GET', path)
        self.assertIn('Google 변경 가져오기', page.text)
        self.assertIn('오후 3:00–4:00', page.text)
        self.assertEqual(self.calendar(schedule_id), pending)
        form = {'csrf': self.csrf, 'action': 'pull', 'revision': pending['revision'],
                'remote_version': pending['remote_version']}
        self.assertEqual(self.http('POST', path, data=form | {'csrf': 'bad'}).status_code, 403)
        response = self.http('POST', path, data=form)
        self.assertEqual(response.status_code, 303)
        detail = self.http('GET', response.headers['location'])
        self.assertEqual(detail.status_code, 200)
        for text in ('현재 일정', '오후 3:00–4:00', '통화 당시 합의', '오후 2:00–3:00', '1월 3일 오후 2시에 방문하겠습니다.'):
            self.assertIn(text, detail.text)
        link = original['evidence'][0]
        self.assertIn(f'href="{link}"', detail.text)
        self.assertEqual(self.http('GET', link).status_code, 200)

        with patch.object(self.service, 'transcript', side_effect=Conflict('RECORDING_UNAVAILABLE')):
            unavailable = self.http('GET', path)
        self.assertEqual(unavailable.status_code, 404)
        self.assertNotIn('가상 방문', unavailable.text)

        imported = self.calendar(schedule_id)
        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        unavailable = self.http('GET', path)
        self.assertEqual(unavailable.status_code, 404)
        self.assertNotIn('가상 방문', unavailable.text)
        self.assertEqual(imported['agreement'], original['agreement'])

    def test_linked_web_draft_collision_requires_a_fresh_choice(self):
        for choice in ('local', 'remote'):
            with self.subTest(choice=choice):
                schedule_id, _, _, _ = self.approved_schedule()
                original = self.calendar(schedule_id)
                path = f'/schedules/{schedule_id}/calendar?date=2030-01-03'
                fields = {'title': '가상 방문', 'start': '2030-01-03T15:00', 'end': '2030-01-03T15:30'}
                edit = {'csrf': self.csrf, 'action': 'edit', 'revision': original['revision'],
                        'remote_version': original['remote_version']} | fields
                self.assertIn('일정 수정', self.http('GET', path + '&edit=1').text)
                invalid = self.http('POST', path, data=edit | {'end': '2030-01-03T14:30'})
                self.assertEqual(invalid.status_code, 409)
                self.assertIn('value="2030-01-03T14:30"', invalid.text)
                self.assertEqual(self.calendar(schedule_id), original)

                remote_fields = {'title': '모의 원격 <변경>', 'start': '2030-01-03T16:00:00+09:00',
                                 'end': '2030-01-03T16:30:00+09:00'}
                self.remote_change(original, 'edit', **remote_fields)
                pending = self.calendar(schedule_id)
                conflict = self.http('POST', path, data=edit)
                self.assertEqual(conflict.status_code, 409)
                for text in ('일정 변경 충돌', '오후 3:00–3:30', '오후 4:00–4:30', '모의 원격 &lt;변경&gt;'):
                    self.assertIn(text, conflict.text)
                self.assertEqual(self.calendar(schedule_id), pending)
                resolve = edit | {'action': 'resolve', 'draft': 'yes', 'remote_version': pending['remote_version']}
                self.assertEqual(self.http('POST', path, data=resolve).status_code, 409)
                self.remote_change(pending, 'edit', **(remote_fields | {'end': '2030-01-03T17:00:00+09:00'}))
                latest = self.calendar(schedule_id)
                stale = self.http('POST', path, data=resolve | {'choice': choice})
                self.assertEqual(stale.status_code, 409)
                self.assertIn('오후 4:00–5:00', stale.text)
                self.assertEqual(self.calendar(schedule_id), latest)
                resolved = self.http('POST', path, data=resolve | {'choice': choice, 'remote_version': latest['remote_version']})
                self.assertEqual(resolved.status_code, 303)
                merged = self.calendar(schedule_id)
                self.assertEqual(merged['current'], merged['remote'])
                self.assertEqual(merged['status'], 'synced')
                self.assertEqual(merged['agreement'], original['agreement'])
                expected = fields['start'] + '+09:00' if choice == 'local' else latest['remote']['start_at']
                self.assertEqual(datetime.fromisoformat(merged['current']['start_at']), datetime.fromisoformat(expected))
                stale_invalid = self.http('POST', path, data=edit | {'end': '2030-01-03T10:00'})
                self.assertEqual(stale_invalid.status_code, 409)
                self.assertIn('일정이 다시 변경됐어요.', stale_invalid.text)
                self.assertNotIn('value="2030-01-03T10:00"', stale_invalid.text)

    def test_linked_web_persisted_conflict_can_be_deferred_or_import_a_deletion(self):
        schedule_id, _, review, _ = self.approved_schedule()
        path = f'/schedules/{schedule_id}/calendar'
        self.reapprove(review, 1, **LOCAL_EDIT)
        self.remote_change(self.calendar(schedule_id), 'delete')
        pending = self.calendar(schedule_id)
        page = self.http('GET', path)
        self.assertIn('충돌 해결', page.text)
        comparison = self.http('GET', path + '?compare=1')
        self.assertIn('나중에', comparison.text)
        self.assertIn('일정 삭제됨', comparison.text)
        self.assertEqual(self.calendar(schedule_id), pending)
        form = {'csrf': self.csrf, 'action': 'resolve', 'revision': pending['revision'],
                'remote_version': pending['remote_version']}
        self.assertEqual(self.http('POST', path, data=form | {'choice': 'local'}).status_code, 409)
        response = self.http('POST', path, data=form | {'choice': 'remote'})
        self.assertEqual(response.status_code, 303)
        self.assertIn('이 일정은 삭제됐어요.', self.http('GET', response.headers['location']).text)
        self.assert_deleted(self.calendar(schedule_id), pending['agreement'])

    def test_remote_edit_is_imported_without_changing_the_approved_agreement(self):
        schedule_id, _, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        evidence = self.current_schedule(schedule_id)['evidence']
        self.assertEqual(original['status'], 'synced')
        self.assertEqual(original['current'], original['remote'])
        self.assertEqual(original['agreement']['segment_ids'], [1])
        self.assertEqual(len(evidence), 1)

        self.remote_change(original, 'edit', **REMOTE_EDIT)
        pending = self.calendar(schedule_id)
        self.assertEqual(pending['status'], 'remote_changed')
        self.assertEqual(pending['revision'], original['revision'])
        self.assertEqual(pending['current'], original['current'])
        self.assertEqual(pending['remote']['title'], REMOTE_EDIT['title'])
        self.assertEqual(pending['remote_version'], original['remote_version'] + 1)
        self.assert_rejected(self.sync(original, 'pull'), 'STALE_CALENDAR_SYNC')
        self.assert_rejected(self.sync(pending, 'edit', **LOCAL_EDIT), 'CALENDAR_SYNC_CONFLICT')
        self.assert_rejected(self.sync(pending, 'delete'), 'CALENDAR_SYNC_CONFLICT')

        imported = self.applied(pending, 'pull')
        self.assertEqual(imported['status'], 'synced')
        self.assertEqual(imported['revision'], original['revision'] + 1)
        self.assertEqual(imported['current'], pending['remote'])
        self.assertEqual(imported['current'], imported['remote'])
        self.assertEqual(imported['agreement'], original['agreement'])
        self.assertEqual(datetime.fromisoformat(imported['current']['start_at']),
                         datetime.fromisoformat(REMOTE_EDIT['start']))
        self.assertEqual(self.current_schedule(schedule_id)['evidence'], evidence)
        self.assertEqual(self.applied(imported, 'pull'), imported)
        self.assert_rejected(self.sync(pending, 'pull'), 'STALE_CALENDAR_SYNC')
        page = self.http('GET', '/schedules?date=2030-01-04')
        self.assertEqual(page.status_code, 200)
        self.assertIn('모의 원격 &lt;변경&gt;', page.text)
        self.assertIn(f'href="{evidence[0]}"', page.text)
        previous_day = self.http('GET', '/schedules?date=2030-01-03')
        self.assertNotIn('모의 원격 &lt;변경&gt;', previous_day.text)

    def test_web_edit_preserves_id_original_approval_and_local_time_precision(self):
        schedule_id, _, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        changed = self.applied(original, 'edit', **LOCAL_EDIT)
        self.assertEqual(changed['status'], 'synced')
        self.assertEqual(changed['id'], schedule_id)
        self.assertEqual(changed['revision'], original['revision'] + 1)
        self.assertEqual(changed['remote_version'], original['remote_version'] + 1)
        self.assertEqual(changed['current'], changed['remote'])
        self.assertEqual(changed['current']['title'], LOCAL_EDIT['title'])
        for field in ('start', 'end'):
            with self.subTest(field=field):
                expected = datetime.fromisoformat(LOCAL_EDIT[field] + '+09:00')
                self.assertEqual(datetime.fromisoformat(changed['remote'][f'{field}_at']), expected)
        self.assertEqual(changed['agreement'], original['agreement'])
        self.assertEqual(self.current_schedule(schedule_id)['title'], LOCAL_EDIT['title'])
        self.assertEqual(self.applied(changed, 'edit', **LOCAL_EDIT), changed)
        same_remote = {'title': LOCAL_EDIT['title'],
                       'start': LOCAL_EDIT['start'] + '+09:00', 'end': LOCAL_EDIT['end'] + '+09:00'}
        self.remote_change(changed, 'edit', **same_remote)
        self.assertEqual(self.calendar(schedule_id), changed)
        with self.assertRaises(Conflict):
            self.remote_change(original, 'edit', **REMOTE_EDIT)
        self.assert_rejected(self.sync(original, 'edit', **LOCAL_EDIT), 'STALE_CALENDAR_SYNC')
        self.assertEqual(self.applied(changed, 'pull'), changed)

    def test_new_change_requires_fresh_explicit_resolution_for_either_side(self):
        schedule_id, _, review, send = self.approved_schedule()
        initial = self.calendar(schedule_id)
        for candidate_revision, chosen in ((1, 'local'), (2, 'remote')):
            with self.subTest(chosen=chosen):
                fields = LOCAL_EDIT | {'title': f'가상 재승인 {candidate_revision}'}
                self.reapprove(review, candidate_revision, **fields)
                local = self.calendar(schedule_id)
                self.assertEqual(local['status'], 'local_changed')
                self.assertEqual(local['remote'], initial['remote'])
                self.assertEqual(local['agreement'], initial['agreement'])
                self.assertEqual(self.http('POST', send, data={
                    'csrf': self.csrf, 'revision': candidate_revision + 1,
                }).status_code, 303)
                self.assertEqual(self.calendar(schedule_id), local)
                self.remote_change(local, 'edit', **(REMOTE_EDIT | {'title': f'가상 원격 {candidate_revision}'}))
                conflict = self.calendar(schedule_id)
                self.assertEqual(conflict['status'], 'conflict')
                self.assert_rejected(self.sync(conflict, 'pull'), 'CALENDAR_SYNC_CONFLICT')
                self.assert_rejected(self.sync(conflict, 'edit', **fields), 'CALENDAR_SYNC_CONFLICT')
                self.assertEqual(self.calendar(schedule_id), conflict)
                self.assert_rejected(self.sync(local, 'pull', resolve='yes'), 'STALE_CALENDAR_SYNC')

                if chosen == 'local':
                    merged = self.applied(conflict, 'edit', resolve='yes', **fields)
                else:
                    merged = self.applied(conflict, 'pull', resolve='yes')
                expected = conflict['remote'] if chosen == 'remote' else conflict['current']
                self.assertEqual((merged['status'], merged['current'], merged['remote']), ('synced', expected, expected))
                self.assertEqual(merged['agreement'], local['agreement'])
                initial = merged

    def test_remote_delete_and_late_reapproval_never_recreate_the_event(self):
        schedule_id, _, review, send = self.approved_schedule()
        original = self.calendar(schedule_id)
        self.reapprove(review, 1, **LOCAL_EDIT)
        current = self.calendar(schedule_id)
        self.remote_change(current, 'delete')
        pending = self.calendar(schedule_id)
        self.assertEqual(pending['status'], 'conflict')
        self.assertIsNone(pending['remote'])
        self.assertTrue(pending['remote_deleted'])
        self.remote_change(pending, 'delete')
        self.assertEqual(self.calendar(schedule_id), pending)
        self.assert_rejected(self.sync(pending, 'pull'), 'CALENDAR_SYNC_CONFLICT')
        self.assertEqual(self.sync(pending, 'edit', resolve='yes', **LOCAL_EDIT).status_code, 409)
        self.assertEqual(self.calendar(schedule_id), pending)

        deleted = self.applied(pending, 'pull', resolve='yes')
        self.assert_deleted(deleted, current['agreement'])
        self.assertEqual(self.applied(deleted, 'pull'), deleted)
        self.assert_rejected(self.sync(original, 'edit', **LOCAL_EDIT), 'STALE_CALENDAR_SYNC')
        with self.assertRaises(Conflict):
            self.remote_change(deleted, 'edit', **REMOTE_EDIT)
        self.reapprove(review, 2, **(LOCAL_EDIT | {'title': '삭제 뒤 재승인'}))
        self.assertEqual(self.http('POST', send, data={'csrf': self.csrf, 'revision': 3}).status_code, 409)
        self.assertEqual(self.calendar(schedule_id), deleted)

    def test_web_delete_removes_remote_event_and_is_safe_to_retry(self):
        schedule_id, _, _, send = self.approved_schedule()
        original = self.calendar(schedule_id)
        deleted = self.applied(original, 'delete')
        self.assert_deleted(deleted, original['agreement'])
        self.assertEqual(deleted['revision'], original['revision'] + 1)
        self.assertEqual(deleted['remote_version'], original['remote_version'] + 1)
        self.assert_rejected(self.sync(original, 'delete'), 'STALE_CALENDAR_SYNC')
        self.assertEqual(self.applied(deleted, 'pull'), deleted)
        self.assertEqual(self.http('POST', send, data={'csrf': self.csrf, 'revision': 1}).status_code, 409)
        self.assertEqual(self.calendar(schedule_id), deleted)

    def test_remote_delete_without_local_changes_is_imported_once(self):
        schedule_id, _, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        self.remote_change(original, 'delete')
        remote_deleted = self.calendar(schedule_id)
        self.assertEqual(remote_deleted['status'], 'remote_changed')
        self.assertTrue(remote_deleted['remote_deleted'])
        self.assertIsNone(remote_deleted['remote'])
        self.assertEqual(remote_deleted['current'], original['current'])
        self.assertEqual(self.current_schedule(schedule_id)['title'], original['current']['title'])

        deleted = self.applied(remote_deleted, 'pull')
        self.assert_deleted(deleted, original['agreement'])
        self.assertEqual(deleted['evidence'], original['evidence'])
        self.assertEqual(deleted['remote_version'], remote_deleted['remote_version'])
        self.assertEqual(self.applied(deleted, 'delete'), deleted)
        self.assertEqual(self.applied(deleted, 'pull'), deleted)

    def test_legacy_checksum_only_event_is_not_mistaken_for_remote_deletion(self):
        schedule_id, _, _, _ = self.approved_schedule()
        with self.service.transaction() as conn:
            conn.execute('UPDATE callops.mock_calendar_events SET title=NULL,start_at=NULL,end_at=NULL WHERE event_id=%s',
                         (schedule_id,))
        legacy = self.calendar(schedule_id)
        self.assertEqual(legacy['status'], 'synced')
        self.assertEqual(legacy['remote_version'], 1)
        self.assertFalse(legacy['remote_deleted'])
        self.assertIsNone(legacy['remote'])
        self.assertEqual(self.applied(legacy, 'pull'), legacy)
        self.assertEqual(self.current_schedule(schedule_id)['title'], legacy['current']['title'])

        self.remote_change(legacy, 'edit', **REMOTE_EDIT)
        changed = self.calendar(schedule_id)
        self.assertEqual(changed['status'], 'remote_changed')
        self.assertEqual(changed['remote_version'], 2)
        imported = self.applied(changed, 'pull')
        self.assertEqual((imported['status'], imported['current'], imported['remote']),
                         ('synced', changed['remote'], changed['remote']))

    def test_simultaneous_web_edit_and_delete_share_one_version(self):
        schedule_id, _, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        barrier = Barrier(2)

        def request(action, **fields):
            barrier.wait(timeout=10)
            return self.sync(original, action, **fields)

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_edit = pool.submit(request, 'edit', **LOCAL_EDIT)
            pending_delete = pool.submit(request, 'delete')
            results = [pending_edit.result(), pending_delete.result()]
        self.assertEqual(sorted(item.status_code for item in results), [200, 409])
        for item in results:
            if item.status_code == 409:
                self.assert_rejected(item, 'STALE_CALENDAR_SYNC')
        final = next(item.json() for item in results if item.status_code == 200)
        self.assertEqual(self.calendar(schedule_id), final)
        self.assertEqual(final['revision'], original['revision'] + 1)
        self.assertEqual(final['remote_version'], original['remote_version'] + 1)
        self.assertEqual(final['current'], final['remote'])
        self.assertEqual(final['agreement'], original['agreement'])

    def test_simultaneous_web_and_provider_edits_cannot_both_commit(self):
        schedule_id, _, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        barrier = Barrier(2)

        def web_edit():
            barrier.wait(timeout=10)
            return self.sync(original, 'edit', **LOCAL_EDIT)

        def provider_edit():
            barrier.wait(timeout=10)
            try:
                self.remote_change(original, 'edit', **REMOTE_EDIT)
            except Conflict as error:
                return str(error)
            return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            web = pool.submit(web_edit)
            provider = pool.submit(provider_edit)
            web_response = web.result()
            provider_error = provider.result()
        final = self.calendar(schedule_id)
        self.assertEqual(final['remote_version'], original['remote_version'] + 1)
        if provider_error is None:
            self.assert_rejected(web_response, 'STALE_CALENDAR_SYNC')
            self.assertEqual(final['status'], 'remote_changed')
            self.assertEqual(final['current'], original['current'])
            self.assertEqual(final['remote']['title'], REMOTE_EDIT['title'])
        else:
            self.assertEqual(provider_error, 'STALE_CALENDAR_SYNC')
            self.assertEqual(web_response.status_code, 200)
            self.assertEqual(final['status'], 'synced')
            self.assertEqual(final['current'], final['remote'])
            self.assertEqual(final['remote']['title'], LOCAL_EDIT['title'])

    def test_db_failure_rolls_back_both_sides_and_latest_request_can_be_retried(self):
        schedule_id, _, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        transaction = self.service.transaction

        @contextmanager
        def fail_after_update():
            with transaction() as conn:
                yield conn
                result = conn.execute('SELECT title FROM callops.schedules WHERE id=%s',
                                      (schedule_id,)).fetchone()
                if result['title'] == LOCAL_EDIT['title']:
                    raise OperationalError('synthetic transaction failure')

        with patch.object(self.service, 'transaction', side_effect=fail_after_update):
            response = self.sync(original, 'edit', **LOCAL_EDIT)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error'], 'STORAGE_UNAVAILABLE')
        self.assertEqual(self.calendar(schedule_id), original)
        retried = self.applied(original, 'edit', **LOCAL_EDIT)
        self.assertEqual(retried['current'], retried['remote'])
        self.assertEqual(retried['current']['title'], LOCAL_EDIT['title'])

    def test_unlinked_unknown_and_manual_schedules_are_out_of_scope(self):
        self.assertEqual(self.http('GET', f'/api/schedules/{uuid4()}/calendar').status_code, 401)
        schedule_id, _, _, send = self.approved_schedule(link=False)
        form = {'csrf': self.csrf, 'revision': 1, 'remote_version': 1, 'action': 'delete'}
        path = f'/api/schedules/{schedule_id}/calendar'
        self.assert_rejected(self.http('GET', path), 'CALENDAR_NOT_LINKED')
        self.assert_rejected(self.http('POST', path, data=form), 'CALENDAR_NOT_LINKED')
        with patch('callops.schedule.calendar.commit_mock_event', side_effect=TimeoutError('synthetic timeout')):
            self.assertEqual(self.http('POST', send, data=form).status_code, 303)
        self.assertEqual(self.current_schedule(schedule_id)['calendar_status'], 'UNKNOWN')
        self.assert_rejected(self.http('GET', path), 'CALENDAR_NOT_LINKED')
        self.assert_rejected(self.http('POST', path, data=form), 'CALENDAR_NOT_LINKED')

        manual = self.http('POST', '/api/schedules/manual', data={
            'csrf': self.csrf, 'request_id': str(uuid4()),
        } | LOCAL_EDIT)
        self.assertEqual(manual.status_code, 201)
        manual_id = manual.json()['id']
        try:
            path = f'/api/schedules/{manual_id}/calendar'
            self.assert_rejected(self.http('GET', path), 'CALENDAR_NOT_LINKED')
            self.assert_rejected(self.http('POST', path, data=form), 'CALENDAR_NOT_LINKED')
            self.assertEqual(self.current_schedule(manual_id)['title'], LOCAL_EDIT['title'])
        finally:
            with self.service.transaction() as conn:
                conn.execute('DELETE FROM callops.schedules WHERE id=%s', (manual_id,))

    def test_invalid_input_csrf_and_expired_or_private_source(self):
        schedule_id, recording, _, _ = self.approved_schedule()
        original = self.calendar(schedule_id)
        self.assertEqual(self.sync(original, 'delete', csrf='bad').status_code, 403)
        for invalid in (
                {'remote_version': '-1'}, {'revision': '9000000000000000000000000'},
                {'title': ' '}, {'start': '2030-01-04T23:59:59+09:00'},
                {'end': LOCAL_EDIT['start']}, {'end': ''}, {'resolve': 'unexpected'}):
            with self.subTest(invalid=invalid):
                response = self.sync(original, 'edit', **(LOCAL_EDIT | invalid))
                self.assertEqual(response.status_code // 100, 4)
        with self.assertRaises(Conflict):
            self.remote_change(original, 'edit', **(REMOTE_EDIT | {'start': '2030-01-04T16:00:00'}))
        self.assertEqual(self.calendar(schedule_id), original)

        self.advance(days=91)
        self.service.maintain()
        self.csrf = self.login()
        expired = self.calendar(schedule_id)
        self.assertIsNone(expired['agreement'])
        self.assertEqual((expired['current'], expired['remote']), (original['current'], original['remote']))
        self.assertEqual(self.current_schedule(schedule_id)['evidence'], [])

        self.service.classify(recording, 'PRIVATE_EXCLUDED')
        self.assertIsNone(self.current_schedule(schedule_id))
        path = f'/api/schedules/{schedule_id}/calendar'
        self.assert_rejected(self.http('GET', path), 'CALENDAR_NOT_LINKED')
        self.assert_rejected(self.sync(expired, 'delete'), 'CALENDAR_NOT_LINKED')
        with self.service.transaction() as conn:
            remote = conn.execute('SELECT event_id,title,start_at,end_at,deleted FROM callops.mock_calendar_events'
                                  ' WHERE event_id=%s', (schedule_id,)).fetchone()
        self.assertEqual(str(remote['event_id']), schedule_id)
        self.assertFalse(remote['deleted'])
        self.assertEqual((remote['title'], remote['start_at'], remote['end_at']), (None, None, None))
