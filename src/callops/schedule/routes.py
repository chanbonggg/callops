"""Authenticated browser review of schedule candidates."""

from pathlib import Path

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..calls.service import CALL_TIMEZONE
from ..common import Conflict
from .calendar import send_schedule
from .manual import change_manual_schedule, create_manual_schedule
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
        return TEMPLATES.TemplateResponse(request, 'schedules.html', {
            'rows': list_schedules(service), 'csrf': request.state.login['csrf'],
            'now': service.clock(), 'call_timezone': CALL_TIMEZONE, 'error': error,
        }, status_code=409 if error else 200)

    def send(request):
        try:
            send_schedule(service, request.path_params['candidate_id'], request.state.form.get('revision', ''))
        except Conflict as error:
            message = CALENDAR_ERRORS.get(str(error))
            if message is None:
                raise
            return schedules(request, message)
        return RedirectResponse('/schedules', status_code=303)

    def schedule_api(request):
        return JSONResponse({'csrf': request.state.login['csrf'],
                             'schedules': [schedule_json(row) for row in list_schedules(service)]})

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

    return [Route('/recordings/{recording_id:uuid}/schedule', page),
            Route('/recordings/{recording_id:uuid}/schedule/{candidate_id:uuid}', submit, methods=['POST']),
            Route('/schedules', schedules), Route('/schedules/{candidate_id:uuid}/send', send, methods=['POST']),
            Route('/api/schedules', schedule_api), Route('/api/schedules/manual', manual_create, methods=['POST']),
            Route('/api/schedules/manual/{schedule_id:uuid}', manual_change, methods=['POST'])]
