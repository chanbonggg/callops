"""Validate synthetic task proposals and record the owner's review decisions."""

import re
from uuid import uuid4

from .common import Conflict, identifier
from .schedule.service import checked_text, event_time
from .tasks import task_fields


def validated_tasks(tasks, transcript):
    if transcript['schema_version'] != 2 or not isinstance(tasks, list) or len(tasks) > 10:
        raise ValueError('Invalid task result')
    candidates = {}
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {'title', 'due_at', 'segment_ids'}:
            raise ValueError('Invalid task candidate')
        title = checked_text(task['title'], 160, required=True)
        due = event_time(task['due_at'])
        ids = task['segment_ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 10
                or any(type(value) is not int or not 1 <= value <= len(transcript['segments']) for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Invalid task evidence IDs')
        candidates[title, due, tuple(sorted(ids))] = (title, due, ids)
    return list(candidates.values())


def store_task_candidates(conn, job, expires_at, candidates):
    for position, (title, due, ids) in enumerate(candidates, 1):
        conn.execute('''INSERT INTO callops.task_candidates
            (id,recording_id,analysis_job_id,transcript_id,position,title,due_at,segment_ids,expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (uuid4(), job['recording_id'], job['id'], job['input_artifact_id'], position, title, due, ids, expires_at))


ACTIVE_CANDIDATES = '''SELECT c.*,j.pipeline_version FROM callops.task_candidates c
    JOIN callops.jobs j ON j.id=c.analysis_job_id
    JOIN callops.artifacts f ON f.id=c.transcript_id
    WHERE c.recording_id=%s AND c.expires_at>%s AND f.expires_at>%s AND f.available'''


def require_task_review(recording):
    if recording['classification_status'] != 'BUSINESS_APPROVED' or recording['deletion_state'] != 'NONE':
        raise Conflict('TASK_CANDIDATE_UNAVAILABLE')


def read_evidence(service, conn, recording, transcript_id):
    artifact = conn.execute('SELECT * FROM callops.artifacts WHERE id=%s AND recording_id=%s',
                            (transcript_id, recording['id'])).fetchone()
    if artifact is None:
        raise Conflict('TASK_CANDIDATE_UNAVAILABLE')
    service.read_transcript_artifact(artifact, recording['duration_seconds'])


def related_tasks(conn, recording_id):
    return conn.execute('''SELECT id,title,status,deleted_at FROM callops.tasks WHERE recording_id=%s
        ORDER BY deleted_at NULLS FIRST,id''', (recording_id,)).fetchall()


def review_task_candidates(service, recording_id):
    with service.transaction() as conn:
        recording = service.recording(conn, recording_id)
        require_task_review(recording)
        now = service.clock()
        rows = conn.execute(ACTIVE_CANDIDATES + ' ORDER BY j.pipeline_version DESC,c.position',
                            (recording_id, now, now)).fetchall()
        available = {}
        for row in rows:
            source = row['transcript_id']
            if source not in available:
                try:
                    read_evidence(service, conn, recording, source)
                except Conflict:
                    available[source] = False
                else:
                    available[source] = True
        return recording, [row for row in rows if available[row['transcript_id']]], related_tasks(conn, recording_id)


def decide_task_candidate(service, recording_id, form):
    candidate_id = identifier(form.get('candidate_id'))
    revision, action = form.get('revision', ''), form.get('action')
    targets = {'approve': 'APPROVED', 'reject': 'REJECTED', 'link': 'LINKED'}
    if action not in targets or not re.fullmatch(r'[1-9][0-9]{0,9}', revision):
        raise Conflict('INVALID_TASK_ACTION')
    target = targets[action]
    revision = int(revision)
    fields = task_fields(form) if action == 'approve' else None
    linked_id = identifier(form.get('task_id')) if action == 'link' else None
    with service.transaction() as conn:
        recording = service.recording(conn, recording_id)
        require_task_review(recording)
        now = service.clock()
        candidate = conn.execute(ACTIVE_CANDIDATES + ' AND c.id=%s FOR UPDATE OF c',
                                 (recording_id, now, now, candidate_id)).fetchone()
        if candidate is None:
            raise Conflict('TASK_CANDIDATE_UNAVAILABLE')
        existing_fields = (candidate['title'], candidate['due_at'])
        task_id = candidate['task_id']
        if candidate['status'] == target and candidate['revision'] == revision + 1:
            if ((fields is not None and fields != existing_fields) or (linked_id is not None and linked_id != task_id)
                    or (task_id is not None and not conn.execute('''SELECT 1 FROM callops.tasks
                        WHERE id=%s AND recording_id=%s AND deleted_at IS NULL''', (task_id, recording_id)).fetchone())):
                raise Conflict('STALE_TASK_CANDIDATE')
            return task_id
        if candidate['revision'] != revision or candidate['status'] != 'PENDING':
            raise Conflict('STALE_TASK_CANDIDATE')

        compare = candidate['pipeline_version'] > 1 and related_tasks(conn, recording_id)
        output_version = form.get('output_version')
        if (compare or output_version is not None) and output_version != str(recording['output_decision_version']):
            raise Conflict('STALE_TASK_CANDIDATE')
        if action in ('approve', 'link'):
            read_evidence(service, conn, recording, candidate['transcript_id'])
            now = service.clock()
            if candidate['expires_at'] <= now:
                raise Conflict('TASK_CANDIDATE_UNAVAILABLE')
        if action == 'approve':
            task_id = candidate_id
            conn.execute('''INSERT INTO callops.tasks (id,request_id,recording_id,title,due_at)
                VALUES (%s,%s,%s,%s,%s)''', (task_id, uuid4(), recording_id, *fields))
        elif action == 'link':
            task = conn.execute('''SELECT 1 FROM callops.tasks WHERE id=%s AND recording_id=%s
                AND deleted_at IS NULL FOR UPDATE''', (linked_id, recording_id)).fetchone()
            if task is None or not compare:
                raise Conflict('TASK_LINK_UNAVAILABLE')
            task_id = linked_id
        conn.execute('''UPDATE callops.task_candidates SET status=%s,revision=revision+1,decided_at=%s,
            task_id=%s,title=%s,due_at=%s WHERE id=%s''',
            (target, now, task_id, *(fields or existing_fields), candidate_id))
        if task_id is not None:
            conn.execute('UPDATE callops.recordings SET output_decision_version=output_decision_version+1 WHERE id=%s',
                         (recording_id,))
        return task_id
