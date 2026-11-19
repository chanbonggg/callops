"""Manage directly entered and call-approved tasks."""

import re
from pathlib import Path
from uuid import uuid4

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .calls.service import CALL_TIMEZONE
from .common import Conflict, identifier
from .schedule.service import checked_text, event_time


TASK_FIELDS = 'id,title,due_at,status,revision,completed_at'
TASK_TRANSITIONS = {'complete': (('OPEN',), 'DONE'), 'cancel': (('OPEN',), 'CANCELLED'),
                    'reopen': (('DONE', 'CANCELLED'), 'OPEN')}
TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name('task_templates'))
WEEKDAYS = '월화수목금토일'
INVALID_FIELDS = '제목은 1~160자이며 기한은 올바른 날짜와 시간으로 입력해 주세요.'


class TaskNotFound(Exception):
    pass


def task_fields(form):
    try:
        title = checked_text(form.get('title'), 160, required=True)
        due_at = event_time(form.get('due') or None, local=True)
    except (ValueError, OverflowError):
        raise Conflict('INVALID_TASK_FIELDS') from None
    return title, due_at


def create_task(service, form):
    request_id = identifier(form.get('request_id'))
    fields = task_fields(form)
    with service.transaction() as conn:
        created = conn.execute(f'''INSERT INTO callops.tasks (id,request_id,title,due_at)
            VALUES (%s,%s,%s,%s) ON CONFLICT (request_id) DO NOTHING RETURNING {TASK_FIELDS}''',
                               (uuid4(), request_id, *fields)).fetchone()
        if created is not None:
            return created, True
        existing = conn.execute(f'''SELECT {TASK_FIELDS} FROM callops.tasks
            WHERE request_id=%s AND recording_id IS NULL AND deleted_at IS NULL AND revision=1''',
                                (request_id,)).fetchone()
        if existing is not None and (existing['title'], existing['due_at']) == fields:
            return existing, False
        raise Conflict('TASK_REQUEST_CONFLICT')


def change_task(service, task_id, form):
    action, revision = form.get('action'), form.get('revision', '')
    if action not in ('edit', 'delete', *TASK_TRANSITIONS) or not re.fullmatch(r'[1-9][0-9]{0,9}', revision):
        raise Conflict('INVALID_TASK_ACTION')
    revision = int(revision)
    fields = task_fields(form) if action == 'edit' else None
    with service.transaction() as conn:
        current = conn.execute('SELECT revision,status,deleted_at FROM callops.tasks WHERE id=%s FOR UPDATE',
                               (task_id,)).fetchone()
        if current is None or current['deleted_at'] is not None:
            if current is not None and action == 'delete' and current['revision'] == revision + 1:
                return None
            raise TaskNotFound
        if current['revision'] != revision:
            raise Conflict('STALE_TASK')
        if action == 'edit':
            row = conn.execute(f'''UPDATE callops.tasks SET title=%s,due_at=%s,revision=revision+1
                WHERE id=%s RETURNING {TASK_FIELDS}''', (*fields, task_id)).fetchone()
        elif action in TASK_TRANSITIONS:
            allowed, target = TASK_TRANSITIONS[action]
            if current['status'] not in allowed:
                raise Conflict('TASK_ALREADY_COMPLETED' if action == 'complete' and current['status'] == 'DONE'
                               else 'INVALID_TASK_TRANSITION')
            row = conn.execute(f'''UPDATE callops.tasks SET status=%s,completed_at=%s,revision=revision+1
                WHERE id=%s RETURNING {TASK_FIELDS}''',
                               (target, service.clock() if target == 'DONE' else None, task_id)).fetchone()
        else:
            # Preserve the request ID so a delayed create cannot restore the task.
            conn.execute('''UPDATE callops.tasks SET title=NULL,due_at=NULL,status=NULL,completed_at=NULL,
                revision=revision+1,deleted_at=%s WHERE id=%s''', (service.clock(), task_id))
            return None
    return row


def list_tasks(service, now):
    with service.transaction() as conn:
        return conn.execute('''SELECT t.id,t.title,t.due_at,t.status,t.revision,t.completed_at,
                t.recording_id,f.id AS transcript_id,c.segment_ids
            FROM callops.tasks t
            LEFT JOIN callops.recordings r ON r.id=t.recording_id
            LEFT JOIN callops.task_candidates c ON c.id=t.id AND c.recording_id=t.recording_id
                AND c.status='APPROVED' AND c.expires_at>%s
            LEFT JOIN callops.artifacts f ON f.id=c.transcript_id AND f.recording_id=c.recording_id
                AND f.available AND f.expires_at>%s
            WHERE t.deleted_at IS NULL AND (t.recording_id IS NULL OR
                (r.classification_status='BUSINESS_APPROVED' AND r.deletion_state='NONE'))
            ORDER BY t.status DESC,t.due_at NULLS LAST,t.id''', (now, now)).fetchall()


def due_label(value):
    if value is None:
        return '기한 없음'
    local = value.astimezone(CALL_TIMEZONE)
    clock = local.strftime('%H:%M:%S' if local.second else '%H:%M')
    return f'{local.month}월 {local.day}일 {WEEKDAYS[local.weekday()]} · {clock}'


def task_json(row):
    return {key: row[key] for key in TASK_FIELDS.split(',')} | {
        'id': str(row['id']), 'due_at': row['due_at'].isoformat() if row['due_at'] else None,
        'completed_at': row['completed_at'].isoformat() if row['completed_at'] else None,
    }


def routes(service):
    def list_api(request):
        return JSONResponse({'csrf': request.state.login['csrf'],
                             'tasks': [task_json(row) for row in list_tasks(service, service.clock())]})

    def create(request):
        row, created = create_task(service, request.state.form)
        return JSONResponse(task_json(row), status_code=201 if created else 200)

    def change(request):
        try:
            row = change_task(service, request.path_params['task_id'], request.state.form)
        except TaskNotFound:
            return JSONResponse({'error': 'TASK_NOT_FOUND'}, status_code=404)
        return Response(status_code=204) if row is None else JSONResponse(task_json(row))

    def render(request, mode, status_code=200, **context):
        return TEMPLATES.TemplateResponse(request, 'tasks.html', {
            'mode': mode, 'csrf': request.state.login['csrf'], 'due_label': due_label, **context,
        }, status_code=status_code)

    def render_list(request, form=None, error=None, status_code=200):
        now = service.clock()
        overdue_only = request.query_params.get('overdue') == '1'
        rows = list_tasks(service, now)
        for row in rows:
            row['overdue'] = row['status'] == 'OPEN' and row['due_at'] is not None and row['due_at'] < now
        if overdue_only:
            rows = [row for row in rows if row['overdue']]
        if form is None:
            form = {'request_id': uuid4()}
        return render(request, 'list', status_code, form=form, error=error,
                      overdue_only=overdue_only,
                      groups=[(label, [r for r in rows if r['status'] == status])
                              for label, status in [('미완료', 'OPEN'), ('완료', 'DONE'), ('취소', 'CANCELLED')]
                              if not overdue_only or status == 'OPEN'])

    def task_list(request):
        if request.method in ('GET', 'HEAD'):
            return render_list(request)
        form = request.state.form
        try:
            create_task(service, form)
        except Conflict as error:
            if str(error) == 'INVALID_TASK_FIELDS':
                return render_list(request, form, INVALID_FIELDS, 422)
            if str(error) == 'INVALID_ID':
                return render_list(request, form | {'request_id': uuid4()}, '등록 요청을 다시 확인해 주세요.', 422)
            return render(request, 'notice', 409, message='이 등록 요청은 이미 사용됐어요.',
                          detail='최신 목록에서 등록 여부를 먼저 확인해 주세요.', form=form)
        return RedirectResponse('/tasks', status_code=303)

    def task_item(request):
        task_id = request.path_params['task_id']
        if request.method in ('GET', 'HEAD'):
            with service.transaction() as conn:
                row = conn.execute(f'''SELECT {TASK_FIELDS} FROM callops.tasks
                    WHERE id=%s AND deleted_at IS NULL''', (task_id,)).fetchone()
            if row is None:
                return render(request, 'notice', 404, message='이 할 일을 찾을 수 없어요.')
            due = row['due_at']
            form = {'title': row['title'], 'revision': row['revision'],
                    'due': due.astimezone(CALL_TIMEZONE).replace(tzinfo=None).isoformat(timespec='seconds') if due else ''}
            mode = 'delete' if request.query_params.get('delete') == '1' else 'edit'
            return render(request, mode, task_id=task_id, form=form, task_status=row['status'])

        form = request.state.form
        try:
            change_task(service, task_id, form)
        except TaskNotFound:
            return render(request, 'notice', 404, message='이 할 일을 찾을 수 없어요.', form=form)
        except Conflict as error:
            if str(error) == 'INVALID_TASK_FIELDS':
                return render(request, 'edit', 422, task_id=task_id, form=form,
                              error=INVALID_FIELDS)
            message = {'STALE_TASK': '다른 탭에서 먼저 수정했어요.',
                       'TASK_ALREADY_COMPLETED': '이미 완료한 할 일이에요.'}.get(str(error), '요청을 처리할 수 없어요.')
            return render(request, 'notice', 409, message=message,
                          detail='입력한 내용은 저장되지 않았습니다. 최신 목록에서 할 일을 다시 열어주세요.', form=form)
        return RedirectResponse('/tasks', status_code=303)

    return [Route('/api/tasks', list_api), Route('/api/tasks', create, methods=['POST']),
            Route('/api/tasks/{task_id:uuid}', change, methods=['POST']),
            Route('/tasks', task_list, methods=['GET', 'POST']),
            Route('/tasks/{task_id:uuid}', task_item, methods=['GET', 'POST']),
            Mount('/task-assets', app=StaticFiles(directory=Path(__file__).with_name('task_assets')))]
