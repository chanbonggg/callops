"""Single-user review UI and separately authenticated worker protocol."""

import hmac
import os
import secrets
from datetime import timedelta

import psycopg
from flask import Flask, Response, abort, g, jsonify, redirect, render_template_string, request, session
from werkzeug.security import check_password_hash

from .config import ConfigurationError
from .db import readiness
from .queue import Queue
from .service import Conflict, Service, digest, utcnow


LOGIN = '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>CallOps 로그인</title>
<h1>CallOps 로그인</h1><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>사용자 <input name="username" required autocomplete="username"></label>
<label>비밀번호 <input type="password" name="password" required autocomplete="current-password"></label>
<button>로그인</button></form></html>'''

INDEX = '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>CallOps 통화 분류</title>
<h1>통화 분류</h1><p>업무 선택은 AI 처리 승인입니다. 일정 등록은 별도 승인이 필요합니다.</p>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button>로그아웃</button></form>
{% for row in rows %}<article><h2>{{ row.id }}</h2><p>{{ row.classification_status }} · {{ row.received_at }} · {{ row.duration_seconds }}초</p>
<p>삭제: {{ row.deletion_state }}</p>
{% if row.classification_status not in ('PRIVATE_EXCLUDED','EXPIRED') %}
<audio controls preload="none" src="/recordings/{{ row.id }}/audio"></audio>
<form method="post" action="/recordings/{{ row.id }}/classify">
<input type="hidden" name="csrf" value="{{ csrf }}">
<button name="decision" value="BUSINESS_APPROVED">업무 · AI 처리 승인</button>
<button name="decision" value="PRIVATE_EXCLUDED">사적 · 관리 사본 삭제</button>
{% if row.classification_status != 'BUSINESS_APPROVED' %}<button name="decision" value="DEFERRED">보류</button>{% endif %}
</form>{% endif %}</article>{% else %}<p>수집된 통화가 없습니다.</p>{% endfor %}</html>'''


def create_app(settings, *, secret=None, owner=None, password_hash=None, workers=None, clock=utcnow):
    secret = secret or os.environ.get('CALLOPS_SESSION_SECRET', '')
    owner = owner or os.environ.get('CALLOPS_OWNER', '')
    password_hash = password_hash or os.environ.get('CALLOPS_PASSWORD_HASH', '')
    workers = workers if workers is not None else {'desktop': os.environ.get('CALLOPS_WORKER_TOKEN', '')}
    if (len(secret) < 32 or secret.startswith('REPLACE_') or not owner or not password_hash.startswith('scrypt:')
            or not workers or any(not name or len(token) < 32 or token.startswith('REPLACE_') for name, token in workers.items())
            or len(set(workers.values())) != len(workers)):
        raise ConfigurationError('Explicit session secret, owner, scrypt password hash and worker tokens required')
    try:
        check_password_hash(password_hash, 'configuration-validation-only')
    except (ValueError, TypeError):
        raise ConfigurationError('Invalid password hash') from None
    readiness(settings)
    service = Service(settings, clock)
    service.recover()
    queue = Queue(service)
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      MAX_CONTENT_LENGTH=Queue.MAX_RESULT, MAX_FORM_MEMORY_SIZE=16384, MAX_FORM_PARTS=8)
    app.config['TRUSTED_HOSTS'] = ['localhost', '127.0.0.1']
    app.extensions.update(callops=service, queue=queue)

    @app.errorhandler(Conflict)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(psycopg.Error)
    @app.errorhandler(OSError)
    def unavailable(error):
        return jsonify(error='STORAGE_UNAVAILABLE'), 503

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
                                 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer'})
        return response

    def csrf_matches(expected):
        actual = request.form.get('csrf', '')
        return bool(expected) and hmac.compare_digest(expected.encode(), actual.encode())

    @app.before_request
    def authentication():
        if request.path.startswith('/worker/'):
            credential = request.headers.get('Authorization', '')
            matches = [name for name, token in workers.items() if hmac.compare_digest(credential.encode(), ('Bearer ' + token).encode())]
            if not matches:
                abort(401)
            g.worker = matches[0]
            return
        if request.path == '/login':
            return
        token = session.get('token', '')
        if not token:
            abort(401)
        with service.transaction() as conn:
            g.login = conn.execute('SELECT * FROM callops.sessions WHERE token_hash=%s AND expires_at>%s',
                                   (digest(token.encode()), clock())).fetchone()
        if g.login is None:
            abort(401)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not csrf_matches(g.login['csrf']):
            abort(403)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET':
            if 'login_csrf' not in session:
                session['login_csrf'] = secrets.token_urlsafe(32)
            return render_template_string(LOGIN, csrf=session['login_csrf'])
        if not csrf_matches(session.get('login_csrf')):
            abort(403)
        with service.transaction() as conn:
            now = clock()
            conn.execute('INSERT INTO callops.login_limits (id,retry_at) VALUES (1,%s) ON CONFLICT DO NOTHING', (now,))
            limit = conn.execute('SELECT * FROM callops.login_limits WHERE id=1 FOR UPDATE').fetchone()
            if limit['failures'] >= 5 and limit['retry_at'] > now:
                return jsonify(error='LOGIN_THROTTLED'), 429
            correct_password = check_password_hash(password_hash, request.form.get('password', ''))
            if not correct_password or not hmac.compare_digest(owner.encode(), request.form.get('username', '').encode()):
                failures = limit['failures'] + 1
                conn.execute('UPDATE callops.login_limits SET failures=%s,retry_at=%s WHERE id=1',
                             (failures, now + timedelta(minutes=5) if failures >= 5 else now))
                return jsonify(error='LOGIN_FAILED'), 401
            conn.execute('UPDATE callops.login_limits SET failures=0,retry_at=%s WHERE id=1', (now,))
            if session.get('token'):
                conn.execute('DELETE FROM callops.sessions WHERE token_hash=%s', (digest(session['token'].encode()),))
            conn.execute('DELETE FROM callops.sessions WHERE expires_at<=%s', (now,))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            conn.execute('INSERT INTO callops.sessions VALUES (%s,%s,%s)', (digest(token.encode()), csrf, now + timedelta(hours=8)))
        session.clear()
        session.permanent = True
        session['token'] = token
        return redirect('/')

    @app.post('/logout')
    def logout():
        with service.transaction() as conn:
            conn.execute('DELETE FROM callops.sessions WHERE token_hash=%s', (g.login['token_hash'],))
        session.clear()
        return redirect('/login')

    @app.get('/')
    def index():
        with service.transaction() as conn:
            rows = conn.execute('SELECT * FROM callops.recordings ORDER BY received_at DESC LIMIT 200').fetchall()
        return render_template_string(INDEX, rows=rows, csrf=g.login['csrf'])

    @app.get('/recordings/<uuid:recording_id>/audio')
    def audio(recording_id):
        with service.transaction() as conn:
            row = service.recording(conn, recording_id)
            if (row['classification_status'] in ('PRIVATE_EXCLUDED', 'EXPIRED') or row['deletion_state'] != 'NONE'
                    or (row['classification_status'] != 'BUSINESS_APPROVED' and row['expires_at'] <= clock())
                    or row['received_at'] + timedelta(days=30) <= clock()):
                abort(410)
            # Keep the lock until bytes are read: cleanup cannot delete mid-read.
            data = service.audio_path(row).read_bytes()
        if digest(data) != row['sha256']:
            raise Conflict('INPUT_CORRUPT')
        return Response(data, mimetype='audio/wav')

    @app.post('/recordings/<uuid:recording_id>/classify')
    def classify(recording_id):
        service.classify(recording_id, request.form.get('decision'))
        return redirect('/')

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400)
        return value

    @app.post('/worker/claim')
    def claim():
        return jsonify(queue.claim(g.worker))

    @app.get('/worker/jobs/<uuid:job_id>/input')
    def worker_input(job_id):
        return Response(queue.input(g.worker, job_id, request.headers.get('X-Lease-Token')), mimetype='application/octet-stream')

    @app.post('/worker/jobs/<uuid:job_id>/<action>')
    def report(job_id, action):
        token = request.headers.get('X-Lease-Token')
        if action == 'result':
            return jsonify(queue.upload(g.worker, job_id, token, request.headers.get('X-Result-Request-ID'), request.get_data()))
        return jsonify(queue.report(g.worker, job_id, token, action, body()))

    @app.post('/worker/cleanup')
    def cleanup():
        data = body()
        return jsonify(queue.cleanup(g.worker, data or None))

    return app
