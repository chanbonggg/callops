"""Authenticated browser and API routes for schedules."""

from calendar import Calendar, SUNDAY, monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..calls.service import CALL_TIMEZONE
from ..common import Conflict
from .calendar import (change_linked_calendar, linked_calendar, linked_calendar_json, send_schedule,
                       sync_fields, sync_status)
from .manual import change_manual_schedule, create_manual_schedule, get_manual_schedule
from .service import change_candidate, list_schedules, review_candidates


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name('templates'))
FORM_ERRORS = {
    'INVALID_SCHEDULE_FIELDS': '제목과 시각을 확인하세요. 종료는 시작보다 늦어야 합니다.',
    'STALE_SCHEDULE_CANDIDATE': '다른 화면에서 후보가 바뀌었습니다. 아래 최신 내용을 확인하세요.',
    'SCHEDULE_ALREADY_DECIDED': '이미 결정된 후보입니다. 내용을 바꾸려면 수정한 뒤 다시 검토하세요.',
    'SCHEDULE_TIMES_REQUIRED': '승인하려면 시작과 종료 시각을 모두 저장하세요.',
    'PAST_SCHEDULE_CONFIRMATION': '이미 시작 시각이 지났습니다. 아래에서 확인한 뒤 승인하세요.',
    'INPUT_CORRUPT': '전사 근거를 확인할 수 없어 승인하지 않았습니다.',
    'INPUT_MISSING': '전사 근거를 확인할 수 없어 승인하지 않았습니다.',
}
CALENDAR_ERRORS = {
    'CALENDAR_APPROVAL_CHANGED': '현재 승인 상태가 바뀌었습니다. 목록을 다시 확인하세요.',
    'CALENDAR_START_PASSED': '시작 시각이 지나 새로 등록할 수 없습니다.',
    'CALENDAR_EVENT_MISMATCH': '기존 모의 일정의 내용이 일치하지 않습니다. 등록 결과를 확인하세요.',
    'INPUT_MISSING': '전사 근거를 확인할 수 없어 전송하지 않았습니다.',
    'INPUT_CORRUPT': '전사 근거를 확인할 수 없어 전송하지 않았습니다.',
}
MANUAL_CONFLICTS = {
    'MANUAL_SCHEDULE_REQUEST_CONFLICT': '이미 사용된 등록 요청입니다. 목록에서 등록 여부를 확인해 주세요.',
    'STALE_MANUAL_SCHEDULE': '다른 탭에서 이 일정을 수정하거나 삭제했습니다.',
    'MANUAL_SCHEDULE_NOT_FOUND': '이 일정을 찾을 수 없습니다. 목록에서 확인해 주세요.',
    'INVALID_SCHEDULE_ACTION': '요청을 처리할 수 없습니다. 목록에서 일정을 다시 열어 주세요.',
}
WEEKDAYS = '월화수목금토일'
LINKED_ERRORS = {
    'STALE_CALENDAR_SYNC': '일정이 다시 변경됐어요. 최신 값을 확인해 주세요.',
    'CALENDAR_SYNC_CONFLICT': 'Calendar 변경 사항을 확인해 주세요.',
    'CALENDAR_EVENT_DELETED': 'Calendar에서 삭제된 일정은 다시 등록할 수 없어요.',
    'INVALID_CALENDAR_SYNC': '적용할 일정을 선택해 주세요.',
    'INVALID_SCHEDULE_FIELDS': FORM_ERRORS['INVALID_SCHEDULE_FIELDS'],
}


def selected_date(request, now):
    value = request.query_params.get('date', '')
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except ValueError:
        pass
    return now.astimezone(CALL_TIMEZONE).date()


def month_view(rows, selected):
    year, month = selected.year, selected.month
    first, last = selected.replace(day=1), selected.replace(day=monthrange(year, month)[1])
    by_day = defaultdict(list)
    for row in rows:
        start = row['start_at'].astimezone(CALL_TIMEZONE)
        end = row['end_at'].astimezone(CALL_TIMEZONE)
        # An event ending at midnight does not occupy the new day.
        last_day = (end - timedelta(microseconds=1)).date()
        event = row | {'start': start, 'end': end}
        for ordinal in range(max(first, start.date()).toordinal(), min(last, last_day).toordinal() + 1):
            by_day[date.fromordinal(ordinal)].append(event)

    cells = []
    for y, m, d, weekday in Calendar(SUNDAY).itermonthdays4(year, month):
        if not date.min.year <= y <= date.max.year:
            cells.append(None)
            continue
        day = date(y, m, d)
        cells.append({'day': day, 'weekday': WEEKDAYS[weekday], 'outside': day.month != month,
                      'count': len(by_day.get(day, ()))})
    return {
        'cells': cells,
        'previous': (first - timedelta(days=1)).replace(day=1) if first > date.min else None,
        'next': last + timedelta(days=1) if last < date.max else None,
        'weekday': WEEKDAYS[selected.weekday()],
        'events': by_day[selected],
    }


def schedule_json(row):
    from_call = row['source'] == 'call'
    agreement = row.get('agreement')
    evidence = [f"/recordings/{row['recording_id']}/transcripts/{row['transcript_id']}#segment-{segment}"
                for segment in agreement['segment_ids']] if agreement else []
    status = (row['delivery_status'] or 'NOT_SENT') if from_call else None
    return {'id': str(row['id']), 'source': row['source'], 'title': row['title'], 'revision': row['revision'],
            'start_at': row['start_at'].isoformat(), 'end_at': row['end_at'].isoformat(), 'evidence': evidence,
            'candidate_id': str(row['candidate_id']) if row.get('candidate_id') else None,
            'approval_revision': row.get('approved_revision'), 'sendable': row.get('sendable', False),
            'agreement': {field: agreement[field] for field in ('title', 'start_at', 'end_at')} if agreement else None,
            'calendar_status': status, 'calendar_revision': row.get('delivery_revision'),
            'calendar_event_id': str(row['id']) if status == 'SUCCEEDED' else None}


def schedule_form(row):
    form = {'title': row['title'], 'revision': row['revision']}
    for field in ('start', 'end'):
        local = row[f'{field}_at'].astimezone(CALL_TIMEZONE)
        form[field] = local.replace(tzinfo=None).isoformat(timespec='seconds' if local.second else 'minutes')
    return form


def local_slot(title, start, end):
    if title is None:
        return None
    start, end = (value.astimezone(CALL_TIMEZONE) for value in (start, end))

    def clock(value):
        fmt = ':%M:%S' if value.second else ':%M'
        return f'{value.hour % 12 or 12}{value.strftime(fmt)}'

    start_period, end_period = ('오전' if value.hour < 12 else '오후' for value in (start, end))
    end_label = clock(end)
    if start.date() != end.date():
        end_label = f'{end.year}년 {end.month}월 {end.day}일 {end_period} {end_label}'
    elif start_period != end_period:
        end_label = f'{end_period} {end_label}'
    return {'title': title, 'day': start.date(),
            'date_label': f'{start.year}년 {start.month}월 {start.day}일 {WEEKDAYS[start.weekday()]}요일',
            'time_label': f'{start_period} {clock(start)}–{end_label}',
            'start': start.isoformat()}


def routes(service, owner):
    def page(request, error=None):
        recording, candidates = review_candidates(service, request.path_params['recording_id'])
        return TEMPLATES.TemplateResponse(request, 'review.html', {
            'recording': recording, 'candidates': candidates, 'csrf': request.state.login['csrf'],
            'now': service.clock(), 'call_timezone': CALL_TIMEZONE, 'error': error,
        }, status_code=409 if error else 200)

    def submit(request):
        recording_id, candidate_id = request.path_params['recording_id'], request.path_params['candidate_id']
        try:
            change_candidate(service, recording_id, candidate_id, owner, request.state.form)
        except Conflict as error:
            message = FORM_ERRORS.get(str(error))
            if message is None:
                raise
            return page(request, message)
        return RedirectResponse(f'/recordings/{recording_id}/schedule#candidate-{candidate_id}', status_code=303)

    def schedules(request, error=None):
        now = service.clock()
        selected = selected_date(request, now)
        return TEMPLATES.TemplateResponse(request, 'schedules.html', {
            **month_view(list_schedules(service), selected),
            'selected': selected, 'today': now.astimezone(CALL_TIMEZONE).date(),
            'csrf': request.state.login['csrf'], 'now': now, 'error': error,
        }, status_code=409 if error else 200)

    def send(request):
        try:
            send_schedule(service, request.path_params['candidate_id'], request.state.form.get('revision', ''))
        except Conflict as error:
            message = CALENDAR_ERRORS.get(str(error))
            if message is None:
                raise
            return schedules(request, message)
        destination = '/schedules'
        if 'date' in request.query_params:
            destination += f'?date={selected_date(request, service.clock())}'
        return RedirectResponse(destination, status_code=303)

    def schedule_api(request):
        return JSONResponse({'csrf': request.state.login['csrf'],
                             'schedules': [schedule_json(row) for row in list_schedules(service)]})

    def linked_calendar_api(request):
        schedule_id = request.path_params['schedule_id']
        if request.method == 'POST':
            row = change_linked_calendar(service, schedule_id, request.state.form)
        else:
            row = linked_calendar(service, schedule_id)
        return JSONResponse(linked_calendar_json(row))

    def linked_page(request):
        schedule_id = request.path_params['schedule_id']
        path = f'/schedules/{schedule_id}/calendar'
        day = selected_date(request, service.clock())
        form = request.state.form if request.method == 'POST' else {}
        action = form.get('action')
        choice = form.get('choice')
        error = None
        draft = None
        try:
            row = linked_calendar(service, schedule_id)
            if request.method == 'POST':
                try:
                    if action == 'resolve' and choice in ('local', 'remote'):
                        if choice == 'local' and form.get('draft') != 'yes':
                            fields = schedule_form(row) if row['deleted_at'] is None else {}
                            submission = form | {key: fields.get(key, '') for key in ('title', 'start', 'end')}
                        else:
                            submission = form
                        submission = submission | {'action': 'edit' if choice == 'local' else 'pull', 'resolve': 'yes'}
                    elif action in ('edit', 'pull'):
                        submission = form | {'resolve': ''}
                    else:
                        raise Conflict('INVALID_CALENDAR_SYNC')
                    result = change_linked_calendar(service, schedule_id, submission)
                except Conflict as exc:
                    code = str(exc)
                    error = LINKED_ERRORS.get(code)
                    if error is None:
                        raise
                    row = linked_calendar(service, schedule_id)
                    same_revision = form.get('revision') == str(row['revision'])
                    if code == 'INVALID_SCHEDULE_FIELDS' and (
                            not same_revision or form.get('remote_version') != str(row['remote_version'])):
                        error = LINKED_ERRORS['STALE_CALENDAR_SYNC']
                    if (code in ('STALE_CALENDAR_SYNC', 'CALENDAR_SYNC_CONFLICT')
                            and same_revision and not row['remote_deleted']
                            and sync_status(row) in ('remote_changed', 'conflict')
                            and (action == 'edit' or form.get('draft') == 'yes')):
                        try:
                            draft = local_slot(*sync_fields(form, local=True))
                        except Conflict:
                            pass  # A stale remote selection may also contain an obsolete edit draft.
                        else:
                            if action == 'edit' and code == 'STALE_CALENDAR_SYNC':
                                error = None
                else:
                    if result['start_at'] is not None:
                        day = result['start_at'].astimezone(CALL_TIMEZONE).date()
                    updated = '&updated=remote' if submission['action'] == 'pull' else ''
                    return RedirectResponse(f'{path}?date={day}{updated}', status_code=303)
        except Conflict as exc:
            if str(exc) != 'CALENDAR_NOT_LINKED':
                raise
            row = None

        mode = 'missing'
        context = {}
        if row:
            status = sync_status(row)
            current = local_slot(row['title'], row['start_at'], row['end_at'])
            remote = local_slot(row['remote_title'], row['remote_start_at'], row['remote_end_at'])
            agreement = row['agreement']
            agreement_slot = local_slot(agreement['title'], datetime.fromisoformat(agreement['start_at']),
                                        datetime.fromisoformat(agreement['end_at'])) if agreement else None
            mode = 'detail'
            if draft or (status == 'conflict' and (request.method == 'POST' or request.query_params.get('compare') == '1')):
                mode = 'conflict'
            elif (current and status in ('synced', 'local_changed')
                    and (request.query_params.get('edit') == '1' or action == 'edit')):
                mode = 'edit'
            context = {'row': row, 'status': status, 'current': current, 'remote': remote,
                       'local_option': draft or current, 'agreement': agreement_slot}
            if mode == 'edit':
                invalid_fields = error == FORM_ERRORS['INVALID_SCHEDULE_FIELDS']
                context.update(fields=form if invalid_fields else schedule_form(row), invalid_fields=invalid_fields)
            elif mode == 'detail' and agreement:
                try:
                    recording, _, transcript = service.transcript(row['recording_id'], agreement['transcript_id'])
                except Conflict as exc:
                    if str(exc) in ('RECORDING_UNAVAILABLE', 'APPROVAL_REVOKED', 'NOT_FOUND'):
                        context = {}
                        mode = 'missing'
                    else:
                        context['evidence'] = []
                else:
                    context['counterparty'] = recording['counterparty']
                    context['evidence'] = linked_calendar_json(row)['evidence']
                    if transcript['schema_version'] == 2 and agreement['segment_ids']:
                        index = agreement['segment_ids'][0] - 1
                        if 0 <= index < len(transcript['segments']):
                            context['quote'] = transcript['segments'][index]['text']

        return TEMPLATES.TemplateResponse(request, 'linked.html', {
            **context, 'mode': mode, 'detail_url': f'{path}?date={day}', 'calendar_url': f'/schedules?date={day}',
            'error': error, 'draft': form if draft else None, 'csrf': request.state.login['csrf'],
            'updated': request.query_params.get('updated'),
        }, status_code=404 if mode == 'missing' else 409 if error or draft else 200)

    def manual_create(request):
        row, created = create_manual_schedule(service, request.state.form)
        return JSONResponse(schedule_json(row), status_code=201 if created else 200)

    def manual_change(request):
        try:
            row = change_manual_schedule(service, request.path_params['schedule_id'], request.state.form)
        except Conflict as error:
            if str(error) != 'MANUAL_SCHEDULE_NOT_FOUND':
                raise
            return JSONResponse({'error': str(error)}, status_code=404)
        return Response(status_code=204) if row is None else JSONResponse(schedule_json(row))

    def manual_form(request, mode, form=None, error=None, status_code=200):
        return TEMPLATES.TemplateResponse(request, 'manual.html', {
            'mode': mode, 'form': form or {}, 'error': error,
            'schedule_id': request.path_params.get('schedule_id'), 'csrf': request.state.login['csrf'],
        }, status_code=status_code)

    def manual_new_page(request):
        if request.method in ('GET', 'HEAD'):
            return manual_form(request, 'create', {'request_id': uuid4()})
        form = request.state.form
        try:
            row, _ = create_manual_schedule(service, form)
        except Conflict as error:
            if str(error) == 'INVALID_ID':
                return manual_form(request, 'create', form | {'request_id': uuid4()},
                                   '등록 요청을 다시 확인해 주세요.', 409)
            return manual_form_error(request, 'create', form, error)
        return RedirectResponse(f"/schedules?date={row['start_at'].astimezone(CALL_TIMEZONE).date()}", status_code=303)

    def manual_form_error(request, mode, form, error):
        if str(error) == 'INVALID_SCHEDULE_FIELDS':
            return manual_form(request, mode, form, FORM_ERRORS[str(error)], 409)
        message = MANUAL_CONFLICTS.get(str(error))
        if message is None:
            raise error
        return manual_form(request, 'conflict', error=message, status_code=409)

    def manual_item_page(request):
        schedule_id = request.path_params['schedule_id']
        mode = 'delete' if request.scope['route'].name == 'manual_schedule_delete' else 'edit'
        if request.method in ('GET', 'HEAD'):
            row = get_manual_schedule(service, schedule_id)
            if row is None:
                return manual_form(request, 'missing', error=MANUAL_CONFLICTS['MANUAL_SCHEDULE_NOT_FOUND'],
                                   status_code=404)
            return manual_form(request, mode, schedule_form(row))
        form = request.state.form
        try:
            row = change_manual_schedule(service, schedule_id, form | {'action': mode})
        except Conflict as error:
            return manual_form_error(request, mode, form, error)
        destination = f"/schedules?date={row['start_at'].astimezone(CALL_TIMEZONE).date()}" if row else '/schedules'
        return RedirectResponse(destination, status_code=303)

    return [Route('/recordings/{recording_id:uuid}/schedule', page),
            Route('/recordings/{recording_id:uuid}/schedule/{candidate_id:uuid}', submit, methods=['POST']),
            Route('/schedules', schedules), Route('/schedules/{candidate_id:uuid}/send', send, methods=['POST']),
            Route('/schedules/{schedule_id:uuid}/calendar', linked_page, methods=['GET', 'POST']),
            Route('/schedules/manual/new', manual_new_page, methods=['GET', 'POST']),
            Route('/schedules/manual/{schedule_id:uuid}', manual_item_page, methods=['GET', 'POST']),
            Route('/schedules/manual/{schedule_id:uuid}/delete', manual_item_page, methods=['GET', 'POST'],
                  name='manual_schedule_delete'),
            Route('/api/schedules', schedule_api), Route('/api/schedules/manual', manual_create, methods=['POST']),
            Route('/api/schedules/{schedule_id:uuid}/calendar', linked_calendar_api, methods=['GET', 'POST']),
            Route('/api/schedules/manual/{schedule_id:uuid}', manual_change, methods=['POST'])]
