"""Read and match the latest eligible transcripts."""

from unicodedata import normalize

from ..calls.service import recording_filters
from ..common import Conflict


MAX_QUERY_LENGTH = 200
MAX_CALLS = 50
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_RESULTS = 5


def transcript_candidates(conn, now, filters, values):
    query = '''
        SELECT artifact.*, r.counterparty, r.call_started_at, r.duration_seconds
        FROM callops.recordings r
        JOIN LATERAL (
            SELECT f.* FROM callops.jobs j
            JOIN callops.attempts a ON a.job_id=j.id
            JOIN callops.artifacts f ON f.id=a.result_artifact_id
            WHERE j.recording_id=r.id AND f.recording_id=r.id AND j.stage='STT'
            ORDER BY j.pipeline_version DESC
            LIMIT 1
        ) artifact ON true
        WHERE r.classification_status='BUSINESS_APPROVED' AND r.deletion_state='NONE'
          AND artifact.available AND artifact.expires_at>%s
    '''
    if filters:
        query += ' AND ' + ' AND '.join(filters)
    query += ' ORDER BY r.received_at DESC, r.id DESC LIMIT %s'
    return conn.execute(query, [now, *values, MAX_CALLS + 1]).fetchall()


def matching_segments(service, q, counterparty='', from_date='', to_date=''):
    if len(q) > MAX_QUERY_LENGTH:
        raise Conflict('INVALID_SEARCH_QUERY')
    terms = normalize('NFC', q).casefold().split()
    if not terms:
        raise Conflict('INVALID_SEARCH_QUERY')
    filters, values = recording_filters(counterparty, from_date, to_date)
    matches, unavailable = [], False
    remaining_bytes = MAX_SCAN_BYTES
    # shortcut: scan recent files under the shared lock, index segments when this
    # bound regularly excludes relevant calls.
    with service.transaction() as conn:
        candidates = transcript_candidates(conn, service.clock(), filters, values)
        limited = len(candidates) > MAX_CALLS
        for artifact in candidates[:MAX_CALLS]:
            if artifact['size_bytes'] > remaining_bytes:
                limited = True
                break
            remaining_bytes -= max(0, artifact['size_bytes'])
            try:
                result = service.read_transcript_artifact(artifact, artifact['duration_seconds'])
            except (Conflict, OSError):
                unavailable = True
                continue
            for segment in result.get('segments', []):
                text = normalize('NFC', segment['text']).casefold()
                if all(term in text for term in terms):
                    matches.append({
                        'recording_id': artifact['recording_id'], 'artifact_id': artifact['id'],
                        'counterparty': artifact['counterparty'], 'call_started_at': artifact['call_started_at'],
                        'expires_at': artifact['expires_at'], 'segment': segment,
                    })
                    if len(matches) == MAX_RESULTS:
                        break
            if len(matches) == MAX_RESULTS:
                break
        now = service.clock()
        matches = [match for match in matches if match['expires_at'] > now]
    return matches, limited, unavailable
