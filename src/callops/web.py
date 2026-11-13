"""Compose the feature modules into one Starlette web application."""

import os

import psycopg
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from werkzeug.security import check_password_hash

from .auth import routes as auth
from .calls.routes import routes as call_routes
from .calls.service import Service
from .common import Conflict, utcnow
from .config import ConfigurationError
from .db import readiness
from .processing.queue import Queue
from .processing.routes import routes as worker_routes
from .schedule.routes import routes as schedule_routes
from .search.routes import routes as search_routes
from .tasks import routes as task_routes


class BodyLimits:
    def __init__(self, app):
        self.browser = RequestBodyLimitMiddleware(app, max_body_size=auth.FORM_MAX_SIZE)
        self.worker = RequestBodyLimitMiddleware(app, max_body_size=Queue.MAX_RESULT)

    async def __call__(self, scope, receive, send):
        handler = self.worker if scope.get('path', '').startswith('/worker/') else self.browser
        await handler(scope, receive, send)


def security_headers(app):
    async def middleware(scope, receive, send):
        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                MutableHeaders(scope=message).update({
                    'Cache-Control': 'no-store',
                    'X-Content-Type-Options': 'nosniff',
                    'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
                    'X-Frame-Options': 'DENY',
                    'Referrer-Policy': 'no-referrer',
                })
            await send(message)

        await app(scope, receive, send_with_headers)

    return middleware


def conflict(request, error):
    return JSONResponse({'error': str(error)}, status_code=409)


def unavailable(request, error):
    return JSONResponse({'error': 'STORAGE_UNAVAILABLE'}, status_code=503)


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
    app = Starlette(
        routes=auth.routes(service, owner, password_hash) + call_routes(service)
        + worker_routes(queue) + search_routes(service) + schedule_routes(service, owner) + task_routes(service),
        middleware=[
            Middleware(security_headers),
            Middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1']),
            Middleware(BodyLimits),
            Middleware(SessionMiddleware, secret_key=secret, max_age=int(auth.SESSION_AGE.total_seconds()),
                       https_only=True, same_site='strict'),
            Middleware(auth.authenticate, service=service, workers=workers),
        ],
        exception_handlers={Conflict: conflict, psycopg.Error: unavailable, OSError: unavailable},
    )
    app.router.redirect_slashes = False
    app.state.callops = service
    app.state.queue = queue
    return app
