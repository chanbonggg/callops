"""Browser views for reviewing calls."""

from datetime import timedelta
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..common import Conflict, digest


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name('templates'))


def routes(service):
    def index(request):
        with service.transaction() as conn:
            rows = conn.execute('SELECT * FROM callops.recordings ORDER BY received_at DESC LIMIT 200').fetchall()
        return TEMPLATES.TemplateResponse(request, 'index.html', {
            'rows': rows, 'csrf': request.state.login['csrf'],
        })

    def audio(request):
        with service.transaction() as conn:
            row = service.recording(conn, request.path_params['recording_id'])
            if (row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED') or row['deletion_state'] != 'NONE'
                    or (row['classification_status'] != 'BUSINESS_APPROVED' and row['expires_at'] <= service.clock())
                    or row['received_at'] + timedelta(days=30) <= service.clock()):
                raise HTTPException(410)
            # Keep the lock until bytes are read: cleanup cannot delete mid-read.
            data = service.audio_path(row).read_bytes()
        if digest(data) != row['sha256']:
            raise Conflict('INPUT_CORRUPT')
        return Response(data, media_type='audio/wav')

    def classify(request):
        service.classify(request.path_params['recording_id'], request.state.form.get('decision'))
        return RedirectResponse('/', status_code=302)

    return [
        Route('/', index),
        Route('/recordings/{recording_id:uuid}/audio', audio),
        Route('/recordings/{recording_id:uuid}/classify', classify, methods=['POST']),
    ]
