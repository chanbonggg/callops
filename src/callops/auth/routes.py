"""Browser sessions and worker credentials."""

import hmac
import secrets
from datetime import timedelta
from pathlib import Path

import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from werkzeug.datastructures import MIMEAccept
from werkzeug.http import parse_accept_header
from werkzeug.security import check_password_hash

from ..common import digest


SESSION_AGE = timedelta(hours=8)
FORM_MAX_SIZE = 16384
LOGIN_ASSETS = ('/task-assets/tasks.css', '/task-assets/wave.svg')
TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name('templates'))


def csrf_matches(expected, form):
    actual = form.get('csrf', '')
    return bool(expected) and hmac.compare_digest(expected.encode(), actual.encode())


def login_error(request, error, status_code):
    accept = parse_accept_header(request.headers.get('accept'), MIMEAccept)
    headers = {'Vary': 'Accept'}
    if accept.best_match(('application/json', 'text/html')) == 'text/html':
        return TEMPLATES.TemplateResponse(
            request, 'login.html', {'csrf': request.session['login_csrf'], 'error': error},
            status_code=status_code, headers=headers,
        )
    return JSONResponse({'error': error}, status_code=status_code, headers=headers)


def authenticate(app, *, service, workers):
    def load_login(token):
        with service.transaction() as conn:
            return conn.execute('SELECT * FROM callops.sessions WHERE token_hash=%s AND expires_at>%s',
                                (digest(token.encode()), service.clock())).fetchone()

    async def check_access(request):
        path = request.scope['path']
        if path.startswith('/worker/'):
            credential = request.headers.get('Authorization', '')
            matches = [name for name, token in workers.items()
                       if hmac.compare_digest(credential.encode(), ('Bearer ' + token).encode())]
            if not matches:
                return PlainTextResponse('Unauthorized', status_code=401)
            request.state.worker = matches[0]
            return None

        if request.method in ('GET', 'HEAD') and path in LOGIN_ASSETS:
            return None

        if path != '/login':
            token = request.session.get('token', '')
            if not token:
                return PlainTextResponse('Unauthorized', status_code=401)
            try:
                request.state.login = await run_in_threadpool(load_login, token)
            except (psycopg.Error, OSError):
                return JSONResponse({'error': 'STORAGE_UNAVAILABLE'}, status_code=503)
            if request.state.login is None:
                return PlainTextResponse('Unauthorized', status_code=401)

        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            values = {}
            try:
                async with request.form(max_part_size=FORM_MAX_SIZE) as form:
                    media_type = request.headers.get('content-type', '').partition(';')[0].strip().lower()
                    if media_type == 'multipart/form-data' and len(form.multi_items()) > 8:
                        return PlainTextResponse('Content Too Large', status_code=413)
                    for key, value in form.multi_items():
                        if isinstance(value, str):
                            values.setdefault(key, value)
            except HTTPException as error:
                return PlainTextResponse(error.detail, status_code=error.status_code)
            request.state.form = values
            if path != '/login' and not csrf_matches(request.state.login['csrf'], values):
                return PlainTextResponse('Forbidden', status_code=403)
        return None

    async def middleware(scope, receive, send):
        if scope['type'] == 'http':
            denied = await check_access(Request(scope, receive))
            if denied is not None:
                await denied(scope, receive, send)
                return
        await app(scope, receive, send)

    return middleware


def routes(service, owner, password_hash):
    def login(request):
        session = request.session
        if request.method in ('GET', 'HEAD'):
            if 'login_csrf' not in session:
                session['login_csrf'] = secrets.token_urlsafe(32)
            return TEMPLATES.TemplateResponse(request, 'login.html', {'csrf': session['login_csrf']})
        form = request.state.form
        if not csrf_matches(session.get('login_csrf'), form):
            raise HTTPException(403)
        error = None
        with service.transaction() as conn:
            now = service.clock()
            conn.execute('INSERT INTO callops.login_limits (id,retry_at) VALUES (1,%s) ON CONFLICT DO NOTHING', (now,))
            limit = conn.execute('SELECT * FROM callops.login_limits WHERE id=1 FOR UPDATE').fetchone()
            if limit['failures'] >= 5 and limit['retry_at'] > now:
                error = ('LOGIN_THROTTLED', 429)
            elif (not check_password_hash(password_hash, form.get('password', ''))
                  or not hmac.compare_digest(owner.encode(), form.get('username', '').encode())):
                failures = limit['failures'] + 1
                conn.execute('UPDATE callops.login_limits SET failures=%s,retry_at=%s WHERE id=1',
                             (failures, now + timedelta(minutes=5) if failures >= 5 else now))
                error = ('LOGIN_FAILED', 401)
            else:
                conn.execute('UPDATE callops.login_limits SET failures=0,retry_at=%s WHERE id=1', (now,))
                if session.get('token'):
                    conn.execute('DELETE FROM callops.sessions WHERE token_hash=%s', (digest(session['token'].encode()),))
                conn.execute('DELETE FROM callops.sessions WHERE expires_at<=%s', (now,))
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                conn.execute('INSERT INTO callops.sessions VALUES (%s,%s,%s)', (digest(token.encode()), csrf, now + SESSION_AGE))
        if error is not None:
            return login_error(request, *error)
        session.clear()
        session['token'] = token
        return RedirectResponse('/', status_code=302)

    def logout(request):
        with service.transaction() as conn:
            conn.execute('DELETE FROM callops.sessions WHERE token_hash=%s', (request.state.login['token_hash'],))
        request.session.clear()
        return RedirectResponse('/login', status_code=302)

    return [Route('/login', login, methods=['GET', 'POST']), Route('/logout', logout, methods=['POST'])]
