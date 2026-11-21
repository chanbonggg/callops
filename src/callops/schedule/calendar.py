"""Send approved schedules and simulate Calendar changes in PostgreSQL."""

import json
import re

from ..common import Conflict, digest, identifier
from .service import event_fields


SENDABLE_APPROVALS = '''SELECT c.logical_item_id, c.revision, c.start_at, c.expires_at, s.revision AS schedule_revision,
        f.id AS transcript_id, r.duration_seconds, a.snapshot, a.snapshot_sha256, d.status AS delivery_status
    FROM callops.schedule_candidates c
    JOIN callops.schedule_approvals a ON a.candidate_id=c.id AND a.revision=c.revision
    JOIN callops.schedules s ON s.id=a.schedule_id AND s.approved_candidate_id=c.id AND s.approved_revision=c.revision
    JOIN callops.recordings r ON r.id=c.recording_id
    JOIN callops.jobs j ON j.id=c.analysis_job_id AND j.recording_id=r.id
    JOIN callops.artifacts f ON f.id=j.input_artifact_id AND f.recording_id=r.id AND f.id::text=a.snapshot->>'transcript_id'
    LEFT JOIN callops.calendar_deliveries d ON d.logical_item_id=c.logical_item_id
    WHERE s.deleted_at IS NULL AND c.status='APPROVED' AND r.classification_status='BUSINESS_APPROVED' AND r.deletion_state='NONE'
        AND c.expires_at>%s AND f.expires_at>%s AND f.available'''


def require_approval(conn, candidate_id, revision, now):
    row = conn.execute(SENDABLE_APPROVALS + ' AND c.id=%s', (now, now, candidate_id)).fetchone()
    if row is None or row['revision'] != revision:
        raise Conflict('CALENDAR_APPROVAL_CHANGED')
    return row


def payload_checksum(row):
    snapshot = row['snapshot']
    if digest(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode('utf-8')) != row['snapshot_sha256']:
        raise Conflict('CALENDAR_APPROVAL_CHANGED')
    payload = {field: snapshot[field] for field in ('title', 'start_at', 'end_at')}
    return digest(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def commit_mock_event(service, candidate_id, revision, event_id):
    # The mock provider commits separately from the delivery acknowledgement.
    with service.transaction() as conn:
        delivery = conn.execute('SELECT * FROM callops.calendar_deliveries WHERE logical_item_id=%s',
                                (event_id,)).fetchone()
        if delivery['status'] == 'SUCCEEDED':
            return delivery['payload_sha256']
        event = conn.execute('SELECT payload_sha256 FROM callops.mock_calendar_events WHERE event_id=%s',
                             (event_id,)).fetchone()
        if event is not None:
            if event['payload_sha256'] != delivery['payload_sha256']:
                raise Conflict('CALENDAR_EVENT_MISMATCH')
            return event['payload_sha256']

        now = service.clock()
        row = require_approval(conn, candidate_id, revision, now)
        artifact = conn.execute('SELECT * FROM callops.artifacts WHERE id=%s', (row['transcript_id'],)).fetchone()
        service.read_transcript_artifact(artifact, row['duration_seconds'])
        now = service.clock()
        if row['start_at'] <= now:
            raise Conflict('CALENDAR_START_PASSED')
        if min(row['expires_at'], artifact['expires_at']) <= now:
            raise Conflict('CALENDAR_APPROVAL_CHANGED')
        checksum = payload_checksum(row)
        conn.execute('''UPDATE callops.calendar_deliveries SET candidate_id=%s,revision=%s,payload_sha256=%s,
            synced_schedule_revision=%s WHERE logical_item_id=%s''',
                     (candidate_id, revision, checksum, row['schedule_revision'], event_id))
        fields = tuple(row['snapshot'][field] for field in ('title', 'start_at', 'end_at'))
        conn.execute('''INSERT INTO callops.mock_calendar_events (event_id,payload_sha256,title,start_at,end_at)
            VALUES (%s,%s,%s,%s,%s)''', (event_id, checksum, *fields))
        return checksum


def send_schedule(service, candidate_id, revision):
    if not re.fullmatch(r'[1-9][0-9]{0,9}', revision):
        raise Conflict('CALENDAR_APPROVAL_CHANGED')
    revision = int(revision)
    with service.transaction() as conn:
        now = service.clock()
        row = require_approval(conn, candidate_id, revision, now)
        event_id = row['logical_item_id']
        if row['delivery_status'] == 'SUCCEEDED':
            return
        if row['delivery_status'] is None:
            if row['start_at'] <= now:
                raise Conflict('CALENDAR_START_PASSED')
            conn.execute('''INSERT INTO callops.calendar_deliveries (logical_item_id,candidate_id,revision,payload_sha256,status)
                VALUES (%s,%s,%s,%s,'UNKNOWN')''', (event_id, candidate_id, revision, payload_checksum(row)))

    try:
        checksum = commit_mock_event(service, candidate_id, revision, event_id)
    except TimeoutError:
        return
    with service.transaction() as conn:
        conn.execute('''UPDATE callops.calendar_deliveries SET status='SUCCEEDED'
            WHERE logical_item_id=%s AND payload_sha256=%s''', (event_id, checksum))


LINKED_SCHEDULE = '''SELECT s.id,s.recording_id,s.title,s.start_at,s.end_at,s.revision,s.deleted_at,
        d.synced_schedule_revision,d.synced_event_version,e.version AS remote_version,e.deleted AS remote_deleted,
        e.title AS remote_title,e.start_at AS remote_start_at,e.end_at AS remote_end_at,
        CASE WHEN f.id IS NOT NULL THEN a.snapshot END AS agreement
    FROM callops.schedules s
    JOIN callops.recordings r ON r.id=s.recording_id
    JOIN callops.calendar_deliveries d ON d.logical_item_id=s.id AND d.status='SUCCEEDED'
    JOIN callops.mock_calendar_events e ON e.event_id=s.id
    LEFT JOIN callops.schedule_approvals a ON a.schedule_id=s.id AND a.candidate_id=d.candidate_id
        AND a.revision=d.revision
    LEFT JOIN callops.schedule_candidates c ON c.id=a.candidate_id AND c.expires_at>%s
    LEFT JOIN callops.jobs j ON j.id=c.analysis_job_id
    LEFT JOIN callops.artifacts f ON f.id=j.input_artifact_id AND f.id::text=a.snapshot->>'transcript_id'
        AND f.available AND f.expires_at>%s
    WHERE s.id=%s AND r.classification_status='BUSINESS_APPROVED' AND r.deletion_state='NONE' '''


def linked_schedule(conn, service, schedule_id):
    now = service.clock()
    row = conn.execute(LINKED_SCHEDULE, (now, now, identifier(schedule_id))).fetchone()
    if row is None:
        raise Conflict('CALENDAR_NOT_LINKED')
    return row


def linked_calendar(service, schedule_id):
    with service.transaction() as conn:
        return linked_schedule(conn, service, schedule_id)


def sync_status(row):
    if row['deleted_at'] is not None:
        return 'deleted'
    remote_changed = row['remote_version'] != row['synced_event_version']
    local_changed = row['revision'] != row['synced_schedule_revision']
    if remote_changed:
        return 'conflict' if local_changed else 'remote_changed'
    return 'local_changed' if local_changed else 'synced'


def event_payload(title, start, end):
    return {'title': title, 'start_at': start.isoformat(), 'end_at': end.isoformat()} if title is not None else None


def linked_calendar_json(row):
    agreement = row['agreement']
    return {'id': str(row['id']), 'revision': row['revision'], 'remote_version': row['remote_version'],
            'status': sync_status(row), 'remote_deleted': row['remote_deleted'],
            'current': event_payload(row['title'], row['start_at'], row['end_at']),
            'remote': event_payload(row['remote_title'], row['remote_start_at'], row['remote_end_at']),
            'agreement': agreement,
            'evidence': [f"/recordings/{row['recording_id']}/transcripts/{agreement['transcript_id']}#segment-{segment}"
                         for segment in agreement['segment_ids']] if agreement else []}


def sync_version(form, name):
    value = form.get(name, '')
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise Conflict('INVALID_CALENDAR_SYNC')
    return int(value)


def sync_fields(form, *, local):
    try:
        fields = event_fields(form.get('title'), form.get('start') or None, form.get('end') or None, local=local)
        if None in fields:
            raise ValueError('Both times are required')
    except (ValueError, OverflowError):
        raise Conflict('INVALID_SCHEDULE_FIELDS') from None
    return fields


def write_mock_event(conn, schedule_id, fields):
    values = fields or (None, None, None)
    payload = event_payload(*values)
    checksum = digest(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    return conn.execute('''UPDATE callops.mock_calendar_events SET title=%s,start_at=%s,end_at=%s,
        deleted=%s,version=version+1,payload_sha256=%s WHERE event_id=%s RETURNING version''',
                        (*values, fields is None, checksum, schedule_id)).fetchone()['version']


def change_mock_event(service, schedule_id, form):
    """Simulate an independent edit or deletion at the provider."""
    action = form.get('action')
    if action not in ('edit', 'delete'):
        raise Conflict('INVALID_CALENDAR_SYNC')
    version = sync_version(form, 'remote_version')
    fields = sync_fields(form, local=False) if action == 'edit' else None
    with service.transaction() as conn:
        row = linked_schedule(conn, service, schedule_id)
        if row['remote_version'] != version:
            raise Conflict('STALE_CALENDAR_SYNC')
        if row['remote_deleted']:
            if action == 'delete':
                return version
            raise Conflict('CALENDAR_EVENT_DELETED')
        if fields == (row['remote_title'], row['remote_start_at'], row['remote_end_at']):
            return version
        return write_mock_event(conn, schedule_id, fields)


def change_linked_calendar(service, schedule_id, form):
    action = form.get('action')
    if action not in ('edit', 'delete', 'pull') or form.get('resolve', '') not in ('', 'yes'):
        raise Conflict('INVALID_CALENDAR_SYNC')
    revision = sync_version(form, 'revision')
    version = sync_version(form, 'remote_version')
    fields = sync_fields(form, local=True) if action == 'edit' else None
    with service.transaction() as conn:
        row = linked_schedule(conn, service, schedule_id)
        if (revision, version) != (row['revision'], row['remote_version']):
            raise Conflict('STALE_CALENDAR_SYNC')
        status = sync_status(row)
        if status == 'deleted':
            if action == 'edit':
                raise Conflict('CALENDAR_EVENT_DELETED')
            return row

        remote_changed = version != row['synced_event_version']
        if action == 'pull' and not remote_changed:
            return row
        if action != 'pull' and row['remote_deleted']:
            raise Conflict('CALENDAR_EVENT_DELETED')
        if remote_changed and (status == 'conflict' or action != 'pull') and form.get('resolve') != 'yes':
            raise Conflict('CALENDAR_SYNC_CONFLICT')
        if status == 'synced' and fields == (row['title'], row['start_at'], row['end_at']):
            return row

        if action == 'pull':
            if not row['remote_deleted']:
                fields = (row['remote_title'], row['remote_start_at'], row['remote_end_at'])
        else:
            version = write_mock_event(conn, schedule_id, fields)
        if fields is None:
            conn.execute('''UPDATE callops.schedules SET title=NULL,start_at=NULL,end_at=NULL,
                approved_candidate_id=NULL,approved_revision=NULL,deleted_at=%s,revision=revision+1 WHERE id=%s''',
                         (service.clock(), schedule_id))
        else:
            conn.execute('''UPDATE callops.schedules SET title=%s,start_at=%s,end_at=%s,revision=revision+1
                WHERE id=%s''', (*fields, schedule_id))
        conn.execute('''UPDATE callops.calendar_deliveries SET synced_event_version=%s,synced_schedule_revision=%s
            WHERE logical_item_id=%s''', (version, revision + 1, schedule_id))
        return linked_schedule(conn, service, schedule_id)
