"""Send the current approved revision to a durable local Calendar simulation."""

import json
import re

from ..common import Conflict, digest


SENDABLE_APPROVALS = '''SELECT c.logical_item_id, c.revision, c.start_at, c.expires_at,
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
        conn.execute('''UPDATE callops.calendar_deliveries SET candidate_id=%s,revision=%s,payload_sha256=%s
            WHERE logical_item_id=%s''', (candidate_id, revision, checksum, event_id))
        conn.execute('INSERT INTO callops.mock_calendar_events (event_id,payload_sha256) VALUES (%s,%s)',
                     (event_id, checksum))
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
