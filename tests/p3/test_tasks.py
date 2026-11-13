import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from tests.support import ProductCase


class TaskTests(ProductCase):
    def form(self):
        request_id = uuid4()
        self.addCleanup(self.remove_task, request_id)
        return {'request_id': str(request_id), 'title': '가상 견적서 전달'}

    def remove_task(self, request_id):
        with self.service.transaction() as conn:
            conn.execute('DELETE FROM callops.tasks WHERE request_id=%s', (request_id,))

    def test_web_task_lifecycle_and_stale_edit(self):
        self.assertEqual(self.http('GET', '/tasks').status_code, 401)
        csrf = self.login()
        self.assertIn('href="/tasks"', self.http('GET', '/').text)
        page = self.http('GET', '/tasks')
        request_id = re.search(r'name="request_id" value="([^"]+)"', page.text)[1]
        self.addCleanup(self.remove_task, request_id)
        form = {'request_id': request_id, 'title': '<가상 견적서>', 'due': ''}
        self.assertEqual(self.http('POST', '/tasks', data=form).status_code, 403)
        form['csrf'] = csrf
        invalid = self.http('POST', '/tasks', data=form | {'due': '2030-02-29T12:00'})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn('value="2030-02-29T12:00"', invalid.text)
        self.assertIn('value="&lt;가상 견적서&gt;"', invalid.text)
        self.assertIn(f'value="{request_id}"', invalid.text)
        for _ in range(2):
            response = self.http('POST', '/tasks', data=form)
            self.assertEqual((response.status_code, response.headers['location']), (303, '/tasks'))
        [task] = self.http('GET', '/api/tasks').json()['tasks']
        self.assertIsNone(task['due_at'])
        path = f"/tasks/{task['id']}"
        first_tab, second_tab = (self.http('GET', path) for _ in range(2))
        first_revision, second_revision = (
            re.search(r'name="revision" value="([^"]+)"', tab.text)[1] for tab in (first_tab, second_tab)
        )
        edit = {'csrf': csrf, 'action': 'edit', 'revision': first_revision,
                'title': '새 제목', 'due': '2030-01-03T00:30:15'}
        self.assertEqual(self.http('POST', path, data=edit).status_code, 303)
        self.assertIn('1월 3일 목 · 00:30:15', self.http('GET', '/tasks').text)
        stale = self.http('POST', path, data=edit | {'revision': second_revision, 'title': '이전 탭'})
        self.assertEqual(stale.status_code, 409)
        self.assertIn('최신 목록 다시 불러오기', stale.text)
        self.assertIn('이전 탭', stale.text)
        self.assertNotIn('name="revision"', stale.text)
        self.assertIn('새 제목', self.http('GET', path).text)
        invalid = self.http('POST', path, data=edit | {'revision': second_revision, 'title': ' '})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn(f'name="revision" value="{second_revision}"', invalid.text)
        self.assertEqual(self.http('POST', path, data=edit | {'revision': 2, 'due': ''}).status_code, 303)
        self.assertEqual(self.http('POST', path, data=edit | {'revision': 3, 'action': 'complete'}).status_code, 303)
        self.assertNotIn('value="complete"', self.http('GET', '/tasks').text)
        confirm = self.http('GET', path + '?delete=1')
        self.assertIn('새 제목', confirm.text)
        self.assertIn('value="delete"', confirm.text)
        self.assertEqual(self.http('POST', path, data=edit | {'revision': 4, 'action': 'delete'}).status_code, 303)
        self.assertEqual(self.http('GET', '/api/tasks').json()['tasks'], [])
        self.assertEqual(self.http('GET', path).status_code, 404)

    def test_manual_task_lifecycle_and_stale_changes(self):
        csrf = self.login()
        form = self.form() | {'csrf': csrf}
        response = self.http('POST', '/api/tasks', data=form)
        self.assertEqual(response.status_code, 201)
        task = response.json()
        self.assertEqual((task['title'], task['due_at'], task['status'], task['revision'], task['completed_at']),
                         (form['title'], None, 'OPEN', 1, None))
        self.assertEqual(self.http('GET', '/api/tasks').json(), {'csrf': csrf, 'tasks': [task]})
        path = f"/api/tasks/{task['id']}"
        edit = {'csrf': csrf, 'action': 'edit', 'revision': 1,
                'title': '수정된 가상 할 일', 'due': '2030-01-03T00:30'}
        response = self.http('POST', path, data=edit)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()['title'], response.json()['due_at'], response.json()['revision']),
                         (edit['title'], '2030-01-02T15:30:00+00:00', 2))
        self.assertEqual(self.http('POST', path, data=edit).json(), {'error': 'STALE_TASK'})

        complete = {'csrf': csrf, 'action': 'complete', 'revision': 2}
        self.assertEqual(self.http('POST', path, data=complete | {'revision': 1}).json(), {'error': 'STALE_TASK'})
        response = self.http('POST', path, data=complete)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()['status'], response.json()['revision'], response.json()['completed_at']),
                         ('DONE', 3, self.now.isoformat()))
        self.assertEqual(self.http('POST', path, data=complete).status_code, 409)
        self.assertEqual(self.http('POST', path, data=complete | {'revision': 3}).json(),
                         {'error': 'TASK_ALREADY_COMPLETED'})

        response = self.http('POST', path, data=edit | {'revision': 3, 'due': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()['due_at'], response.json()['status'], response.json()['completed_at']),
                         (None, 'DONE', self.now.isoformat()))
        delete = {'csrf': csrf, 'action': 'delete', 'revision': 4}
        self.assertEqual(self.http('POST', path, data=delete | {'revision': 3}).status_code, 409)
        self.assertEqual(self.http('POST', path, data=delete).status_code, 204)
        self.assertEqual(self.http('POST', path, data=delete).status_code, 204)
        self.assertEqual(self.http('POST', '/api/tasks', data=form).status_code, 409)
        self.assertEqual(self.http('GET', '/api/tasks').json()['tasks'], [])
        with self.service.transaction() as conn:
            removed = conn.execute('SELECT title,due_at,status,completed_at FROM callops.tasks WHERE id=%s',
                                   (task['id'],)).fetchone()
        self.assertEqual(tuple(removed.values()), (None, None, None, None))

    def test_manual_task_requires_session_csrf_and_valid_fields(self):
        form = {'request_id': str(uuid4()), 'title': '가상 할 일'}
        path = f'/api/tasks/{uuid4()}'
        self.assertEqual(self.http('GET', '/api/tasks').status_code, 401)
        self.assertEqual(self.http('POST', '/api/tasks', data=form).status_code, 401)
        self.assertEqual(self.http('POST', path, data={'action': 'delete', 'revision': 1}).status_code, 401)
        self.assertEqual(self.http('POST', '/api/tasks', data=form, headers={
            'Authorization': 'Bearer ' + self.tokens['one'],
        }).status_code, 401)
        csrf = self.login()
        self.assertEqual(self.http('POST', '/api/tasks', data=form).status_code, 403)
        self.assertEqual(self.http('POST', path, data={'action': 'delete', 'revision': 1}).status_code, 403)
        for invalid in ({'title': ' '}, {'title': 'a' * 161}, {'title': '\x00'}, {'request_id': 'bad'},
                        {'due': '2030-02-29T12:00'}, {'due': '0001-01-01T00:00'}, {'due': '2030-01-03T12:00Z'}):
            with self.subTest(invalid=invalid):
                response = self.http('POST', '/api/tasks', data=form | invalid | {'csrf': csrf})
                self.assertEqual(response.status_code, 409)
        self.assertEqual(self.http('POST', path, data={'csrf': csrf, 'action': 'complete', 'revision': 1}).status_code, 404)
        self.assertEqual(self.http('GET', '/api/tasks').json()['tasks'], [])

    def test_concurrent_create_reuses_one_task_and_rejects_changed_payload(self):
        csrf = self.login()
        form = self.form() | {'csrf': csrf}
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=10)
            return self.http('POST', '/api/tasks', data=form)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [pool.submit(submit) for _ in range(2)]
            first, second = [result.result() for result in responses]
        self.assertEqual(sorted([first.status_code, second.status_code]), [200, 201])
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.http('POST', '/api/tasks', data=form | {'due': '2030-01-03T12:00'}).status_code, 409)
        self.assertEqual(self.http('GET', '/api/tasks').json()['tasks'], [first.json()])
