"""Persist analysis candidates with verified evidence and review a specific revision."""

import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

from ..calls.service import CALL_TIMEZONE
from ..common import Conflict, digest


LOCAL_TIME = r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?'
OFFSET = r'(?:Z|[+-][0-9]{2}:[0-9]{2})'


def checked_text(value, maximum, *, required=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid schedule text')
    value.encode('utf-8')
    value = value.strip()
    if required and not value:
        raise ValueError('Missing title')
    return value


def event_time(value, *, local=False):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(LOCAL_TIME if local else LOCAL_TIME + OFFSET, value):
        raise ValueError('Invalid schedule time')
    if not local and value[-1] != 'Z' and int(value[-2:]) >= 60:
        raise ValueError('Invalid schedule offset')
    parsed = datetime.fromisoformat(value)
    if local:
        parsed = parsed.replace(tzinfo=CALL_TIMEZONE)
    result = parsed.astimezone(timezone.utc)
    result.astimezone(CALL_TIMEZONE)
    return result


def event_fields(title, start, end, *, local=False):
    title = checked_text(title, 160, required=True)
    start = event_time(start, local=local)
    end = event_time(end, local=local)
    if start is not None and end is not None and end <= start:
        raise ValueError('End must follow start')
    return title, start, end


def review_status(start, end):
    return 'AWAITING_APPROVAL' if start is not None and end is not None else 'NEEDS_CONFIRMATION'


def validated_events(events, transcript):
    if (transcript['schema_version'] != 2 or not isinstance(events, list) or len(events) > 10):
        raise ValueError('Invalid schedule result')
    candidates = []
    seen = set()
    for event in events:
        if not isinstance(event, dict) or set(event) != {'title', 'start', 'end', 'time_hint', 'segment_ids'}:
            raise ValueError('Invalid schedule candidate')
        ids = event['segment_ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 10
                or any(type(value) is not int or not 1 <= value <= len(transcript['segments']) for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Invalid evidence IDs')
        title, start, end = event_fields(event['title'], event['start'], event['end'])
        hint = checked_text(event['time_hint'], 240)
        key = (title, start, end, hint, tuple(sorted(ids)))
        if key in seen:
            continue
        seen.add(key)
        candidates.append((title, start, end, hint, ids, review_status(start, end)))
    return candidates


def store_candidates(conn, job, expires_at, candidates):
    for position, (title, start, end, hint, ids, status) in enumerate(candidates, 1):
        conn.execute('''INSERT INTO callops.schedule_candidates
            (id,recording_id,analysis_job_id,position,title,start_at,end_at,time_hint,segment_ids,status,expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (uuid4(), job['recording_id'], job['id'], position, title, start, end, hint, ids, status, expires_at))


ACTIVE_CANDIDATES = '''SELECT c.*, f.id AS transcript_id FROM callops.schedule_candidates c
    JOIN callops.jobs j ON j.id=c.analysis_job_id AND j.recording_id=c.recording_id
    JOIN callops.artifacts f ON f.id=j.input_artifact_id AND f.recording_id=c.recording_id
    WHERE c.recording_id=%s AND c.expires_at>%s AND f.expires_at>%s AND f.available'''


def require_reviewable(recording):
    if recording['classification_status'] != 'BUSINESS_APPROVED' or recording['deletion_state'] != 'NONE':
        raise Conflict('SCHEDULE_UNAVAILABLE')


def review_candidates(service, recording_id):
    with service.transaction() as conn:
        recording = service.recording(conn, recording_id)
        require_reviewable(recording)
        now = service.clock()
        rows = conn.execute(ACTIVE_CANDIDATES + ' ORDER BY j.pipeline_version DESC,c.position',
                            (recording_id, now, now)).fetchall()
    return recording, rows


def list_schedules(service):
    with service.transaction() as conn:
        now = service.clock()
        return conn.execute('''SELECT s.id,s.source,s.title,s.start_at,s.end_at,s.revision,s.recording_id,r.counterparty,
                s.approved_revision,d.status AS delivery_status,d.revision AS delivery_revision,
                d.candidate_id AS delivery_candidate_id,f.id AS transcript_id,
                CASE WHEN f.id IS NOT NULL THEN a.candidate_id END AS candidate_id,
                CASE WHEN f.id IS NOT NULL THEN a.snapshot END AS agreement,
                COALESCE(f.id IS NOT NULL AND c.status='APPROVED' AND c.revision=s.approved_revision,false) AS sendable
            FROM callops.schedules s
            LEFT JOIN callops.recordings r ON r.id=s.recording_id
            LEFT JOIN callops.schedule_approvals a ON a.schedule_id=s.id
                AND a.candidate_id=s.approved_candidate_id AND a.revision=s.approved_revision
            LEFT JOIN callops.schedule_candidates c ON c.id=a.candidate_id
            LEFT JOIN callops.jobs j ON j.id=c.analysis_job_id
            LEFT JOIN callops.artifacts f ON f.id=j.input_artifact_id AND f.id::text=a.snapshot->>'transcript_id'
                AND f.available AND f.expires_at>%s AND c.expires_at>%s
            LEFT JOIN callops.calendar_deliveries d ON d.logical_item_id=s.id
            WHERE s.deleted_at IS NULL AND (s.source='manual' OR
                (r.classification_status='BUSINESS_APPROVED' AND r.deletion_state='NONE'))
            ORDER BY s.start_at,s.id''', (now, now)).fetchall()


def store_approval(conn, candidate, actor, now):
    logical_id = candidate['logical_item_id']
    candidate_id, revision = candidate['id'], candidate['revision']
    fields = (candidate['title'], candidate['start_at'], candidate['end_at'])
    new_schedule = logical_id is None
    if new_schedule:
        logical_id = uuid4()
        conn.execute('''INSERT INTO callops.schedules (id,source,recording_id,title,start_at,end_at)
            VALUES (%s,'call',%s,%s,%s,%s)''', (logical_id, candidate['recording_id'], *fields))
        conn.execute('UPDATE callops.schedule_candidates SET logical_item_id=%s WHERE id=%s', (logical_id, candidate_id))
        conn.execute('UPDATE callops.recordings SET output_decision_version=output_decision_version+1 WHERE id=%s',
                     (candidate['recording_id'],))
    snapshot = {'title': fields[0], 'start_at': fields[1].isoformat(), 'end_at': fields[2].isoformat(),
                'transcript_id': str(candidate['transcript_id']), 'segment_ids': candidate['segment_ids']}
    checksum = digest(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    conn.execute('''INSERT INTO callops.schedule_approvals
        (candidate_id,revision,schedule_id,actor,approved_at,snapshot,snapshot_sha256)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''', (candidate_id, revision, logical_id, actor, now, Jsonb(snapshot), checksum))
    if new_schedule:
        conn.execute('UPDATE callops.schedules SET approved_candidate_id=%s,approved_revision=%s WHERE id=%s',
                     (candidate_id, revision, logical_id))
    else:
        # A later analysis may link to this schedule without adopting its proposal.
        conn.execute('''UPDATE callops.schedules SET title=%s,start_at=%s,end_at=%s,
            approved_revision=%s,revision=revision+1 WHERE id=%s AND approved_candidate_id=%s AND deleted_at IS NULL''',
                     (*fields, revision, logical_id, candidate_id))
    return logical_id


def change_candidate(service, recording_id, candidate_id, actor, form):
    action = form.get('action')
    revision = form.get('revision', '')
    if action not in ('edit', 'approve', 'reject') or not re.fullmatch(r'[1-9][0-9]{0,9}', revision):
        raise Conflict('INVALID_SCHEDULE_ACTION')
    if action == 'edit':
        try:
            title, start, end = event_fields(form.get('title'), form.get('start') or None,
                                             form.get('end') or None, local=True)
        except (ValueError, OverflowError):
            raise Conflict('INVALID_SCHEDULE_FIELDS') from None
    with service.transaction() as conn:
        recording = service.recording(conn, recording_id)
        require_reviewable(recording)
        now = service.clock()
        candidate = conn.execute(ACTIVE_CANDIDATES + ' AND c.id=%s FOR UPDATE OF c',
                                 (recording_id, now, now, candidate_id)).fetchone()
        if candidate is None:
            raise Conflict('SCHEDULE_UNAVAILABLE')
        if candidate['revision'] != int(revision):
            raise Conflict('STALE_SCHEDULE_CANDIDATE')
        status = candidate['status']
        if action == 'edit':
            if status != form.get('status'):
                raise Conflict('STALE_SCHEDULE_CANDIDATE')
            if (title, start, end) != (candidate['title'], candidate['start_at'], candidate['end_at']):
                conn.execute('''UPDATE callops.schedule_candidates SET title=%s,start_at=%s,end_at=%s,status=%s,
                    revision=revision+1,decided_at=NULL,decided_by=NULL WHERE id=%s''',
                    (title, start, end, review_status(start, end), candidate_id))
            return

        target = 'APPROVED' if action == 'approve' else 'REJECTED'
        if status == target:
            return
        if status in ('APPROVED', 'REJECTED'):
            raise Conflict('SCHEDULE_ALREADY_DECIDED')
        logical_id = candidate['logical_item_id']
        if target == 'APPROVED':
            if status != 'AWAITING_APPROVAL':
                raise Conflict('SCHEDULE_TIMES_REQUIRED')
            if candidate['start_at'] <= now and form.get('confirm_past') != 'yes':
                raise Conflict('PAST_SCHEDULE_CONFIRMATION')
            artifact = conn.execute('SELECT * FROM callops.artifacts WHERE id=%s', (candidate['transcript_id'],)).fetchone()
            service.read_transcript_artifact(artifact, recording['duration_seconds'])
            logical_id = store_approval(conn, candidate, actor, now)
        conn.execute('''UPDATE callops.schedule_candidates SET status=%s,logical_item_id=%s,decided_by=%s,decided_at=%s
            WHERE id=%s''', (target, logical_id, actor, now, candidate_id))
