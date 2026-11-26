"""Prepare, rank and validate RAG work under the CallOps storage lock."""

import json
import re
from datetime import timedelta
from itertools import islice
from math import hypot, isclose
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..calls.service import audio_expired, counterparty_name, recording_filters
from ..common import Conflict
from ..search.service import MAX_QUERY_LENGTH, MAX_RESULTS, transcript_query


RAG_STAGES = ('RAG_EMBED', 'RAG_ANSWER')
CONTRACT_VERSION = 1
EMBED_MODEL = 'bge-m3:567m'
EMBED_DIGEST = '7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab'
ANSWER_MODEL = 'qwen3:8b'
ANSWER_DIGEST = '500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41'
DIMENSIONS = 1024
BATCH_SIZE = 16
MAX_EMBED_BYTES = 4096
MAX_INPUT_BYTES = 80 * 1024
MAX_RESULT_BYTES = 1024 * 1024
QUESTION_TTL = timedelta(minutes=15)
RESULT_TTL = timedelta(hours=24)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def excerpt_windows(segments):
    """Use the fixed D4 window and keep the original utterance IDs."""
    utterances = [(segment['segment_id'], segment['text'].strip())
                  for segment in segments if segment['text'].strip()]
    if len(utterances) == 1:
        yield utterances
    else:
        yield from zip(utterances, utterances[1:])


def vector_text(vector):
    if (not isinstance(vector, list) or len(vector) != DIMENSIONS
            or any(type(value) not in (float, int) for value in vector)
            or not isclose(hypot(*vector), 1, rel_tol=0, abs_tol=1e-5)):
        raise ValueError('Invalid unit vector')
    return '[' + ','.join(str(value) for value in vector) + ']'


def scope(request):
    return recording_filters(request['counterparty'], request['from_date'], request['to_date'])


def prompt_scope(request):
    return {name: request[name] or None for name in ('counterparty', 'from_date', 'to_date')}


def current_artifacts(conn, now, ids, request=None, pins=None):
    if not ids:
        return {}
    filters, values = scope(request) if request else ([], [])
    query = transcript_query([*filters, 'artifact.id=ANY(%s)'])
    rows = {row['id']: row for row in conn.execute(query, [now, *values, list(ids)])}
    if rows.keys() != set(ids) or (pins is not None and any(
            [row['version'], row['sha256']] != pins.get(str(key)) for key, row in rows.items())):
        raise Conflict('APPROVAL_REVOKED')
    return rows


def evidence_id(reference):
    return f'{reference[0]}:{reference[1]}'


def read_evidence(service, conn, now, request, refs, pins=None):
    if not refs:
        return []
    ids = {UUID(ref[0]) for ref in refs}
    artifacts = current_artifacts(conn, now, ids, request, pins)
    transcripts = {
        key: service.read_transcript_artifact(row, row['duration_seconds'])['segments']
        for key, row in artifacts.items()
    }
    sources = []
    for ref in refs:
        artifact_id, number = ref
        key = UUID(artifact_id)
        artifact = artifacts[key]
        segment = transcripts[key][number - 1]
        sources.append((artifact, segment, {
            'id': evidence_id(ref), 'call_id': str(artifact['recording_id']),
            'call_started_at': (artifact['call_started_at'].isoformat()
                                if artifact['call_started_at'] and artifact['call_time_trusted'] else None),
            'start': segment['start'], 'end': segment['end'], 'speaker': segment.get('speaker'),
            'text': segment['text'].strip(),
        }))
    return sources


class Rag:
    def __init__(self, service, owner):
        self.service = service
        self.owner = owner

    def submit(self, fields):
        query = fields['q'].strip()
        if not query or len(query) > MAX_QUERY_LENGTH or '\x00' in query:
            raise Conflict('INVALID_SEARCH_QUERY')
        request = fields | {'q': query, 'counterparty': counterparty_name(fields['counterparty']) or '',
                            'from_date': fields['from_date'].strip(), 'to_date': fields['to_date'].strip()}
        if '\x00' in request['counterparty']:
            raise Conflict('INVALID_COUNTERPARTY')
        if any(request[key] and not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', request[key])
               for key in ('from_date', 'to_date')):
            raise Conflict('INVALID_CALL_FILTER')
        scope(request)
        with self.service.transaction() as conn:
            now = self.service.clock()
            count = conn.execute('''SELECT count(*) AS n FROM callops.rag_questions
                WHERE owner=%s AND request IS NOT NULL AND result IS NULL AND error_code IS NULL
                    AND expires_at>%s''', (self.owner, now)).fetchone()['n']
            if count >= 8:
                raise Conflict('TOO_MANY_QUESTIONS')
            key = uuid4()
            conn.execute('''INSERT INTO callops.rag_questions (id,owner,request,expires_at)
                VALUES (%s,%s,%s,%s)''', (key, self.owner, Jsonb(request), now + QUESTION_TTL))
            self.prepare_question(conn, key, request, now)
        return key

    def enqueue_question(self, conn, key, stage, now):
        conn.execute('''INSERT INTO callops.jobs (id,rag_question_id,stage,retry_at)
            VALUES (%s,%s,%s,%s)''', (uuid4(), key, stage, now))

    def enqueue_index(self, conn, now, preferred):
        query = transcript_query([
            'NOT artifact.rag_indexed',
            '''NOT EXISTS (SELECT 1 FROM callops.jobs j
                WHERE j.input_artifact_id=artifact.id AND j.rag_offset=0)''',
        ]) + ' ORDER BY (artifact.id=ANY(%s)) DESC, r.received_at DESC, r.id DESC LIMIT 1'
        artifact = conn.execute(query, (now, list(preferred))).fetchone()
        if artifact is None:
            return None
        return self.enqueue_batch(conn, artifact['recording_id'], artifact['id'], artifact['version'],
                                  artifact['sha256'], 0, now)

    def enqueue_batch(self, conn, recording_id, artifact_id, version, sha, offset, now):
        conn.execute('''INSERT INTO callops.jobs
            (id,recording_id,stage,input_artifact_id,pipeline_version,rag_source_hash,rag_offset,retry_at)
            VALUES (%s,%s,'RAG_EMBED',%s,%s,%s,%s,%s)''',
            (uuid4(), recording_id, artifact_id, version, sha, offset, now))

    def index_state(self, conn, request, now):
        filters, values = scope(request)
        query = transcript_query(filters)
        return conn.execute(f'''WITH eligible AS ({query})
            SELECT f.id,f.rag_indexed,EXISTS (SELECT 1 FROM callops.jobs j
                WHERE j.input_artifact_id=f.id AND j.rag_offset IS NOT NULL
                    AND j.status IN ('FAILED_MANUAL','CANCELLED')) AS failed
            FROM eligible f''', [now, *values]).fetchall()

    def prepare_question(self, conn, key, request, now):
        rows = self.index_state(conn, request, now)
        if any(row['failed'] for row in rows):
            conn.execute("UPDATE callops.rag_questions SET error_code='RAG_INDEX_FAILED' WHERE id=%s", (key,))
            return set()
        pending = {row['id'] for row in rows if not row['rag_indexed']}
        if pending:
            return pending
        if rows and conn.execute('SELECT 1 FROM callops.rag_chunks WHERE artifact_id=ANY(%s) LIMIT 1',
                                 ([row['id'] for row in rows],)).fetchone():
            self.enqueue_question(conn, key, 'RAG_EMBED', now)
        else:
            self.save_result(conn, key, None, '검색 범위에서 답할 근거를 찾지 못했어요.', [], now)
        return set()

    def prepare_questions(self, conn, now):
        waiting = conn.execute('''SELECT id,request FROM callops.rag_questions q
            WHERE q.owner=%s AND q.request IS NOT NULL AND q.result IS NULL AND q.error_code IS NULL AND q.expires_at>%s
                AND NOT EXISTS (SELECT 1 FROM callops.jobs j WHERE j.rag_question_id=q.id)
            ORDER BY q.expires_at,q.id''', (self.owner, now)).fetchall()
        preferred = set()
        for row in waiting:
            preferred.update(self.prepare_question(conn, row['id'], row['request'], now))
        return preferred

    def question_row(self, conn, question_id, now):
        row = conn.execute('SELECT * FROM callops.rag_questions WHERE id=%s AND owner=%s',
                           (question_id, self.owner)).fetchone()
        if row is None or row['request'] is None or row['expires_at'] <= now:
            raise Conflict('INPUT_EXPIRED')
        return row

    def input_info(self, conn, job, now):
        source = job['input_artifact_id']
        if source:
            artifact = current_artifacts(conn, now, [source])[source]
            if (artifact['version'], artifact['sha256']) != (job['pipeline_version'], job['rag_source_hash']):
                raise Conflict('INPUT_CORRUPT')
            return None, job['rag_input_hash'], artifact['expires_at'], job['id'], 1
        question = self.question_row(conn, job['rag_question_id'], now)
        expires = question['expires_at']
        if question['source_artifacts']:
            rows = current_artifacts(conn, now, question['source_artifacts'], question['request'], question['source_pins'])
            expires = min(expires, *(row['expires_at'] for row in rows.values()))
        return None, job['rag_input_hash'], expires, job['id'], 1

    def document_batch(self, conn, job, now):
        key, offset = job['input_artifact_id'], job['rag_offset']
        row = current_artifacts(conn, now, [key])[key]
        segments = self.service.read_transcript_artifact(row, row['duration_seconds']).get('segments', [])
        candidates = list(islice(excerpt_windows(segments), offset, offset + BATCH_SIZE + 1))
        payload = {'schema_version': 1, 'texts': []}
        for window in candidates[:BATCH_SIZE]:
            text = ' '.join(part[1] for part in window)
            if len(text.encode('utf-8')) > MAX_EMBED_BYTES:
                raise Conflict('RAG_INDEX_TOO_LONG')
            payload['texts'].append(text)
            if len(json_bytes(payload)) > MAX_INPUT_BYTES:
                payload['texts'].pop()
                break
        count = len(payload['texts'])
        return candidates[:count], payload, count < len(candidates)

    def payload(self, conn, job, now):
        if job['rag_offset'] is not None:
            _, payload, _ = self.document_batch(conn, job, now)
            return payload
        question = self.question_row(conn, job['rag_question_id'], now)
        request = question['request']
        if job['stage'] == 'RAG_EMBED':
            return {'schema_version': 1, 'texts': [request['q']]}
        sources = read_evidence(self.service, conn, now, request, question['evidence'], question['source_pins'])
        return {'schema_version': 1, 'question': request['q'], 'scope': prompt_scope(request),
                'evidence': [source[2] for source in sources]}

    def finish(self, conn, job, output, now):
        if not isinstance(output, dict) or type(output.get('schema_version')) is not int or output['schema_version'] != 1:
            raise ValueError('Invalid RAG result version')
        if job['stage'] == 'RAG_EMBED':
            vectors = output.get('embeddings')
            if (set(output) != {'schema_version', 'model_name', 'model_digest', 'dimensions', 'embeddings'}
                    or output['model_name'] != EMBED_MODEL or output['model_digest'] != EMBED_DIGEST
                    or type(output['dimensions']) is not int or output['dimensions'] != DIMENSIONS
                    or not isinstance(vectors, list)
                    or not 1 <= len(vectors) <= BATCH_SIZE):
                raise ValueError('Invalid embedding result')
            vectors = [vector_text(vector) for vector in vectors]
            if job['rag_offset'] is not None:
                self.store_batch(conn, job, vectors, now)
            elif len(vectors) == 1:
                self.rank(conn, job['rag_question_id'], vectors[0], now)
            else:
                raise ValueError('Expected one question embedding')
            return
        self.store_answer(conn, job['rag_question_id'], output, now)

    def store_batch(self, conn, job, vectors, now):
        rows, _, more = self.document_batch(conn, job, now)
        if len(rows) != len(vectors):
            raise ValueError('Unexpected embedding count')
        key, offset = job['input_artifact_id'], job['rag_offset']
        conn.cursor().executemany('''INSERT INTO callops.rag_chunks (artifact_id,position,excerpts,model_digest,embedding)
            VALUES (%s,%s,%s,%s,%s::callops.vector)''',
            [(key, offset + index, Jsonb([[str(key), part[0]] for part in window]), EMBED_DIGEST, vector)
             for index, (window, vector) in enumerate(zip(rows, vectors, strict=True))])
        if more:
            self.enqueue_batch(conn, job['recording_id'], key, job['pipeline_version'],
                               job['rag_source_hash'], offset + len(rows), now)
        else:
            conn.execute('UPDATE callops.artifacts SET rag_indexed=true WHERE id=%s', (key,))

    def rank(self, conn, question_id, vector, now):
        question = self.question_row(conn, question_id, now)
        request = question['request']
        if any(row['failed'] or not row['rag_indexed'] for row in self.index_state(conn, request, now)):
            conn.execute("UPDATE callops.rag_questions SET error_code='RAG_INDEX_FAILED' WHERE id=%s", (question_id,))
            return
        filters, values = scope(request)
        query = transcript_query([*filters, 'artifact.rag_indexed'])
        rows = conn.execute(f'''WITH eligible AS ({query})
            SELECT c.excerpts FROM callops.rag_chunks c JOIN eligible f ON f.id=c.artifact_id
            WHERE c.chunk_version=1 AND c.model_digest=%s
            ORDER BY c.embedding OPERATOR(callops.<=>) %s::callops.vector, c.artifact_id, c.position
            LIMIT %s''', [now, *values, EMBED_DIGEST, vector, MAX_RESULTS]).fetchall()
        refs = {}
        for row in rows:
            for ref in row['excerpts']:
                refs.setdefault(evidence_id(ref), ref)
        ranked = list(refs.values())
        if not ranked:
            self.save_result(conn, question_id, None, '검색 범위에서 답할 근거를 찾지 못했어요.', [], now)
            return
        sources = read_evidence(self.service, conn, now, request, ranked)
        pins = {str(row['id']): [row['version'], row['sha256']] for row, _, _ in sources}
        conn.execute('''UPDATE callops.rag_questions SET evidence=%s,source_artifacts=%s,source_pins=%s WHERE id=%s''',
                     (Jsonb(ranked), [UUID(key) for key in pins], Jsonb(pins), question_id))
        self.enqueue_question(conn, question_id, 'RAG_ANSWER', now)

    def store_answer(self, conn, question_id, output, now):
        question = self.question_row(conn, question_id, now)
        answer, reason = output.get('answer'), output.get('reason')
        citations = output.get('segment_ids')
        if (set(output) != {'schema_version', 'model_name', 'model_digest', 'prompt_version',
                            'answer', 'reason', 'segment_ids'}
                or output['model_name'] != ANSWER_MODEL or output['model_digest'] != ANSWER_DIGEST
                or output['prompt_version'] != 'rag-v10'
                or (answer is None) == (reason is None)
                or not isinstance(answer if answer is not None else reason, str)
                or not (answer if answer is not None else reason).strip()
                or '\x00' in (answer if answer is not None else reason)
                or len((answer if answer is not None else reason).encode('utf-8')) > 8192
                or not isinstance(citations, list) or len(citations) > 10
                or any(not isinstance(citation, str) for citation in citations)
                or len(set(citations)) != len(citations)
                or not set(citations).issubset({evidence_id(ref) for ref in question['evidence']})
                or (answer is not None and not citations)):
            raise ValueError('Invalid answer or citations')
        self.save_result(conn, question_id, answer, reason, citations, now)

    def save_result(self, conn, question_id, answer, reason, citations, now):
        conn.execute('UPDATE callops.rag_questions SET result=%s,display_until=%s WHERE id=%s',
                     (Jsonb({'answer': answer, 'reason': reason, 'segment_ids': citations}),
                      now + RESULT_TTL, question_id))

    def status(self, question_id):
        with self.service.transaction() as conn:
            now = self.service.clock()
            row = conn.execute('SELECT * FROM callops.rag_questions WHERE id=%s AND owner=%s',
                               (question_id, self.owner)).fetchone()
            if row is None:
                raise Conflict('QUESTION_NOT_FOUND')
            result = row['result']
            fields = row['request']
            if fields is None:
                return {'state': self.terminal_state(row['error_code'])}
            if result is not None and row['display_until'] <= now:
                return {'state': 'rag_expired'}
            if result is None:
                job = conn.execute('''SELECT status,error_code,rag_tries FROM callops.jobs WHERE rag_question_id=%s
                    ORDER BY (stage='RAG_ANSWER') DESC LIMIT 1''', (question_id,)).fetchone()
                if row['expires_at'] <= now:
                    return {'state': 'rag_timeout_running' if job and job['rag_tries'] else 'rag_timeout_waiting'}
                if row['error_code']:
                    return {'state': self.terminal_state(row['error_code']), 'fields': fields}
                if job and job['status'] in ('FAILED_MANUAL', 'CANCELLED'):
                    return {'state': self.terminal_state(job['error_code']), 'fields': fields}
                if job and job['status'] in ('PREPARING', 'RUNNING', 'UPLOADING'):
                    return {'state': 'rag_running', 'fields': fields}
                state = 'rag_waiting' if job else 'rag_indexing'
                return {'state': state, 'fields': fields}
            try:
                citations = result['segment_ids']
                sources = read_evidence(self.service, conn, now, fields, row['evidence'], row['source_pins'])
                by_id = {item[2]['id']: item for item in sources}
                sources = [by_id[key] for key in citations]
            except (Conflict, OSError):
                return {'state': 'rag_revoked'}
            displayed = []
            for artifact, segment, _ in sources:
                base = f"/recordings/{artifact['recording_id']}/transcripts/{artifact['id']}"
                number = segment['segment_id']
                detail = dict(segment, source_url=f'{base}#segment-{number}')
                if (segment['end'] > segment['start'] and not audio_expired(artifact, now)
                        and self.service.audio_path(artifact).is_file()):
                    detail['audio_url'] = f'{base}/audio/{number}'
                displayed.append((artifact, detail, False))
            return {'state': 'rag_answer' if result['answer'] else 'rag_abstain', 'fields': fields,
                    'answer': result['answer'], 'reason': result['reason'], 'sources': displayed}

    @staticmethod
    def terminal_state(code):
        return {
            'RAG_INDEX_FAILED': 'rag_index_failed',
            'RAG_CONTEXT_TOO_LONG': 'rag_context',
            'RAG_SOURCE_UNAVAILABLE': 'rag_revoked',
            'APPROVAL_REVOKED': 'rag_revoked',
            'INPUT_EXPIRED': 'rag_revoked',
            'RAG_DEADLINE_RUNNING': 'rag_timeout_running',
            'RAG_DEADLINE_WAITING': 'rag_timeout_waiting',
            'RAG_RESULT_EXPIRED': 'rag_expired',
        }.get(code, 'rag_failed')
