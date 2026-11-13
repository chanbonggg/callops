"""Store directly entered schedules independently of call analysis."""

import re
from uuid import uuid4

from ..common import Conflict, identifier
from .service import event_fields


MANUAL_FIELDS = 'id, title, start_at, end_at, revision, source'


def manual_fields(form):
    try:
        title, start, end = event_fields(form.get('title'), form.get('start') or None,
                                         form.get('end') or None, local=True)
        if start is None or end is None:
            raise ValueError('Both schedule times are required')
    except (ValueError, OverflowError):
        raise Conflict('INVALID_SCHEDULE_FIELDS') from None
    return title, start, end


def create_manual_schedule(service, form):
    request_id = identifier(form.get('request_id'))
    fields = manual_fields(form)
    with service.transaction() as conn:
        created = conn.execute(f'''INSERT INTO callops.schedules (id,request_id,title,start_at,end_at)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (request_id) DO NOTHING RETURNING {MANUAL_FIELDS}''',
                               (uuid4(), request_id, *fields)).fetchone()
        if created is not None:
            return created, True
        existing = conn.execute(f'''SELECT {MANUAL_FIELDS} FROM callops.schedules
            WHERE source='manual' AND request_id=%s AND deleted_at IS NULL AND revision=1''', (request_id,)).fetchone()
        if existing is not None and (existing['title'], existing['start_at'], existing['end_at']) == fields:
            return existing, False
        raise Conflict('MANUAL_SCHEDULE_REQUEST_CONFLICT')


def change_manual_schedule(service, schedule_id, form):
    action, revision = form.get('action'), form.get('revision', '')
    if action not in ('edit', 'delete') or not re.fullmatch(r'[1-9][0-9]{0,9}', revision):
        raise Conflict('INVALID_SCHEDULE_ACTION')
    revision = int(revision)
    fields = manual_fields(form) if action == 'edit' else None
    with service.transaction() as conn:
        current = conn.execute("SELECT revision, deleted_at FROM callops.schedules WHERE id=%s AND source='manual' FOR UPDATE",
                               (schedule_id,)).fetchone()
        if current is None:
            raise Conflict('MANUAL_SCHEDULE_NOT_FOUND')
        if current['deleted_at'] is not None:
            if action == 'delete' and current['revision'] == revision + 1:
                return None
            raise Conflict('MANUAL_SCHEDULE_NOT_FOUND')
        if current['revision'] != revision:
            raise Conflict('STALE_MANUAL_SCHEDULE')
        if action == 'edit':
            return conn.execute(f'''UPDATE callops.schedules SET title=%s,start_at=%s,end_at=%s,
                revision=revision+1 WHERE id=%s RETURNING {MANUAL_FIELDS}''', (*fields, schedule_id)).fetchone()
        # Keep the request ID so a delayed create cannot restore deleted content.
        conn.execute('''UPDATE callops.schedules SET title=NULL,start_at=NULL,end_at=NULL,
            revision=revision+1,deleted_at=%s WHERE id=%s''', (service.clock(), schedule_id))
        return None
