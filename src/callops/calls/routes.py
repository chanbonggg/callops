"""Browser views for reviewing calls and transcript evidence."""

from datetime import timedelta
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..common import Conflict, digest
from .audio import PCM_PREVIEW_TYPES, media_type, pcm_clip, pcm_preview
from .service import CALL_TIMEZONE


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name('templates'))


def routes(service):
    def index(request):
        filters = {name: request.query_params.get(name, '') for name in ('counterparty', 'from_date', 'to_date')}
        rows = service.list_recordings(**filters)
        return TEMPLATES.TemplateResponse(request, 'index.html', {
            'rows': rows, 'csrf': request.state.login['csrf'], 'filters': filters,
            'now': service.clock(), 'call_timezone': CALL_TIMEZONE,
        })

    def metadata(request):
        form = request.state.form
        service.update_metadata(request.path_params['recording_id'], form.get('counterparty', ''),
                                form.get('call_started_at', ''))
        return RedirectResponse('/', status_code=302)

    def detail(request):
        row, artifact, result = service.transcript(request.path_params['recording_id'],
                                                   request.path_params.get('artifact_id'))
        audio_available = (row['received_at'] + timedelta(days=30) > service.clock()
                           and service.audio_path(row).is_file())
        return TEMPLATES.TemplateResponse(request, 'detail.html', {
            'row': row, 'artifact': artifact, 'result': result, 'audio_available': audio_available,
            'call_timezone': CALL_TIMEZONE,
        })

    def audio(request):
        preview = request.query_params.get('play') == 'pcm'
        segment = None
        if 'segment_id' in request.path_params:
            _, _, result = service.transcript(request.path_params['recording_id'],
                                              request.path_params['artifact_id'])
            position = request.path_params['segment_id'] - 1
            if result['schema_version'] != 2 or not 0 <= position < len(result['segments']):
                raise HTTPException(404)
            segment = result['segments'][position]
            if segment['start'] == segment['end']:
                raise HTTPException(404)
        with service.transaction() as conn:
            row = service.recording(conn, request.path_params['recording_id'])
            if (row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED') or row['deletion_state'] != 'NONE'
                    or ((preview or segment) and row['classification_status'] != 'BUSINESS_APPROVED')
                    or (row['classification_status'] != 'BUSINESS_APPROVED' and row['expires_at'] <= service.clock())
                    or row['received_at'] + timedelta(days=30) <= service.clock()):
                raise HTTPException(410)
            # Keep the lock until bytes are read: cleanup cannot delete mid-read.
            data = service.audio_path(row).read_bytes()
        if digest(data) != row['sha256']:
            raise Conflict('INPUT_CORRUPT')
        kind = media_type(data[:12])
        if segment or (preview and kind in PCM_PREVIEW_TYPES):
            converted = (pcm_clip(data, kind, segment['start'], segment['end']) if segment
                         else pcm_preview(data, kind))
            with service.transaction() as conn:
                row = service.recording(conn, row['id'])
                if (row['classification_status'] != 'BUSINESS_APPROVED' or row['deletion_state'] != 'NONE'
                        or row['received_at'] + timedelta(days=30) <= service.clock()):
                    raise HTTPException(410)
            return Response(converted, media_type='audio/wav')
        return Response(data, media_type=kind)

    def classify(request):
        service.classify(request.path_params['recording_id'], request.state.form.get('decision'))
        return RedirectResponse('/', status_code=302)

    return [
        Route('/', index),
        Route('/recordings/{recording_id:uuid}/metadata', metadata, methods=['POST']),
        Route('/recordings/{recording_id:uuid}', detail),
        Route('/recordings/{recording_id:uuid}/transcripts/{artifact_id:uuid}', detail, name='transcript'),
        Route('/recordings/{recording_id:uuid}/audio', audio),
        Route('/recordings/{recording_id:uuid}/transcripts/{artifact_id:uuid}/audio/{segment_id:int}', audio),
        Route('/recordings/{recording_id:uuid}/classify', classify, methods=['POST']),
    ]
