"""Browser views for reviewing calls and transcript evidence."""

from pathlib import Path

from jinja2 import ChoiceLoader, FileSystemLoader
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..common import Conflict, digest
from ..schedule.service import review_candidates
from ..task_candidates import decide_task_candidate, review_task_candidates
from .audio import PCM_PREVIEW_TYPES, media_type, pcm_clip, pcm_preview
from .service import CALL_TIMEZONE, audio_expired


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name('templates'))
TEMPLATES.env.loader = ChoiceLoader([
    TEMPLATES.env.loader,
    FileSystemLoader(Path(__file__).parents[1] / 'schedule' / 'templates'),
])
METADATA_ERRORS = {
    'INVALID_COUNTERPARTY': '통화 상대는 줄바꿈 없이 100자 이내로 입력하세요.',
    'INVALID_CALL_TIME': '실제 통화 시각을 확인해 주세요.',
}


def routes(service):
    def index(request):
        filters = {name: request.query_params.get(name, '') for name in ('counterparty', 'from_date', 'to_date')}
        error = None
        try:
            rows = service.list_recordings(**filters)
        except Conflict as exc:
            error = METADATA_ERRORS.get(str(exc))
            if str(exc) == 'INVALID_CALL_FILTER':
                error = '통화 시작일과 종료일을 확인해 주세요.'
            if error is None:
                raise
            rows = []
        return TEMPLATES.TemplateResponse(request, 'index.html', {
            'rows': rows, 'csrf': request.state.login['csrf'], 'filters': filters,
            'now': service.clock(), 'call_timezone': CALL_TIMEZONE, 'error': error, 'active_nav': 'calls',
        }, status_code=409 if error else 200)

    def metadata(request):
        form = request.state.form
        recording_id = request.path_params['recording_id']
        try:
            service.update_metadata(recording_id, form.get('counterparty', ''), form.get('call_started_at', ''))
        except Conflict as exc:
            error = METADATA_ERRORS.get(str(exc))
            if error is None:
                raise
            return detail(request, form=form, error=error)
        return RedirectResponse(f'/recordings/{recording_id}', status_code=302)

    def detail(request, *, form=None, error=None):
        recording_id, pinned = request.path_params['recording_id'], request.path_params.get('artifact_id')
        row, artifact, result = service.transcript(recording_id, pinned, include_summary=not pinned)
        audio_available = not audio_expired(row, service.clock()) and service.audio_path(row).is_file()
        candidates = []
        if result and not pinned:
            _, candidates = review_candidates(service, recording_id)
            candidates = [candidate for candidate in candidates if candidate['transcript_id'] == artifact['id']]
        return TEMPLATES.TemplateResponse(request, 'detail.html', {
            'row': row, 'artifact': artifact, 'result': result, 'audio_available': audio_available,
            'call_timezone': CALL_TIMEZONE, 'csrf': request.state.login['csrf'], 'active_nav': 'calls',
            'pinned': pinned, 'form': form, 'error': error, 'candidates': candidates,
        }, status_code=409 if error else 200)

    def audio(request):
        preview = request.query_params.get('play') == 'pcm'
        segment = None
        source = None
        if 'segment_id' in request.path_params:
            _, source, result = service.transcript(request.path_params['recording_id'],
                                                   request.path_params['artifact_id'])
            position = request.path_params['segment_id'] - 1
            if result['schema_version'] != 2 or not 0 <= position < len(result['segments']):
                raise HTTPException(404)
            segment = result['segments'][position]
            if segment['start'] == segment['end']:
                raise HTTPException(404)
        with service.transaction() as conn:
            row = service.recording(conn, request.path_params['recording_id'])
            now = service.clock()
            if (row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED') or row['deletion_state'] != 'NONE'
                    or ((preview or segment) and row['classification_status'] != 'BUSINESS_APPROVED')
                    or (row['classification_status'] != 'BUSINESS_APPROVED' and row['expires_at'] <= now)
                    or audio_expired(row, now) or (source is not None and source['expires_at'] <= now)):
                raise HTTPException(410)
            # Keep the lock until bytes are read: cleanup cannot delete mid-read.
            data = service.audio_path(row).read_bytes()
        if digest(data) != row['sha256']:
            raise Conflict('INPUT_CORRUPT')
        if audio_expired(row, service.clock()):
            raise HTTPException(410)
        kind = media_type(data[:12])
        if segment or (preview and kind in PCM_PREVIEW_TYPES):
            converted = (pcm_clip(data, kind, segment['start'], segment['end']) if segment
                         else pcm_preview(data, kind))
            with service.transaction() as conn:
                row = service.recording(conn, row['id'])
                now = service.clock()
                if (row['classification_status'] != 'BUSINESS_APPROVED' or row['deletion_state'] != 'NONE'
                        or audio_expired(row, now) or (source is not None and source['expires_at'] <= now)):
                    raise HTTPException(410)
            return Response(converted, media_type='audio/wav')
        return Response(data, media_type=kind)

    def classify(request):
        recording_id, decision = request.path_params['recording_id'], request.state.form.get('decision')
        service.classify(recording_id, decision)
        location = f'/recordings/{recording_id}' if decision == 'BUSINESS_APPROVED' else '/'
        return RedirectResponse(location, status_code=302)

    def task_candidates(request):
        recording_id = request.path_params['recording_id']
        path = f'/recordings/{recording_id}/task-candidates'
        error = None
        if request.method == 'POST':
            try:
                task_id = decide_task_candidate(service, recording_id, request.state.form)
            except Conflict as exc:
                error = str(exc)
            else:
                return RedirectResponse(f'/tasks/{task_id}' if task_id else path, status_code=303)
        try:
            recording, candidates, tasks = review_task_candidates(service, recording_id)
        except Conflict as exc:
            recording, candidates, tasks = None, [], []
            error = error or str(exc)
        return TEMPLATES.TemplateResponse(request, 'task_candidates.html', {
            'recording': recording, 'recording_id': recording_id, 'candidates': candidates,
            'tasks': tasks, 'csrf': request.state.login['csrf'], 'call_timezone': CALL_TIMEZONE,
            'error': error, 'active_nav': 'calls',
        }, status_code=409 if error else 200)

    return [
        Route('/', index),
        Route('/recordings/{recording_id:uuid}/metadata', metadata, methods=['POST']),
        Route('/recordings/{recording_id:uuid}', detail),
        Route('/recordings/{recording_id:uuid}/task-candidates', task_candidates, methods=['GET', 'POST']),
        Route('/recordings/{recording_id:uuid}/transcripts/{artifact_id:uuid}', detail, name='transcript'),
        Route('/recordings/{recording_id:uuid}/audio', audio),
        Route('/recordings/{recording_id:uuid}/transcripts/{artifact_id:uuid}/audio/{segment_id:int}', audio),
        Route('/recordings/{recording_id:uuid}/classify', classify, methods=['POST']),
    ]
