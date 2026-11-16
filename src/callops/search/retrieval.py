"""Rank transcript windows and keep citations tied to the source artifact."""

from math import hypot, isfinite

from ..calls.service import recording_filters
from ..common import Conflict
from .service import MAX_CALLS, MAX_QUERY_LENGTH, MAX_RESULTS, MAX_SCAN_BYTES, transcript_candidates


WINDOW_CHARS = 600
WINDOW_SECONDS = 45


def transcript_windows(segments):
    window, chars, end = [], 0, 0
    for segment in segments:
        if not segment['text'].strip():
            continue
        length = len(segment['text'])
        if window and (chars + 1 + length > WINDOW_CHARS
                       or max(end, segment['end']) - window[0]['start'] > WINDOW_SECONDS):
            yield window, end
            previous = window[-1]
            # Keep the boundary utterance as context when both utterances fit.
            if (len(window) > 1 and len(previous['text']) + 1 + length <= WINDOW_CHARS
                    and max(previous['end'], segment['end']) - previous['start'] <= WINDOW_SECONDS):
                window, chars, end = [previous], len(previous['text']), previous['end']
            else:
                window, chars, end = [], 0, 0
        # shortcut: keep an oversized utterance intact, split if the real model's input limit requires it.
        chars += length + bool(window)
        end = max(end, segment['end'])
        window.append(segment)
    if window:
        yield window, end


def unit_vector(embedding):
    values = tuple(embedding)
    length = hypot(*values)
    if not values or not isfinite(length):
        raise ValueError('Invalid embedding')
    return tuple(value / length for value in values) if length else values


def retrieve_evidence(service, q, *, embed, counterparty='', from_date='', to_date=''):
    """Return up to five ranked windows, scan limit and unreadable-source flags.

    The caller supplies a trusted local embedder for both query and transcript.
    """
    if not isinstance(q, str) or len(q) > MAX_QUERY_LENGTH or not q.strip():
        raise Conflict('INVALID_SEARCH_QUERY')
    filters, values = recording_filters(counterparty, from_date, to_date)
    sources, unavailable = [], False
    remaining_bytes = MAX_SCAN_BYTES
    with service.transaction() as conn:
        candidates = transcript_candidates(conn, service.clock(), filters, values)
        limited = len(candidates) > MAX_CALLS
        for artifact in candidates[:MAX_CALLS]:
            size = artifact['size_bytes']
            if size > remaining_bytes:
                limited = True
                break
            remaining_bytes -= max(0, size)
            try:
                result = service.read_transcript_artifact(artifact, artifact['duration_seconds'])
            except (Conflict, OSError):
                unavailable = True
                continue
            if result.get('segments'):
                sources.append((artifact, result['segments']))

    if not sources:
        return [], limited, unavailable
    question = unit_vector(embed(q.strip()))
    if not any(question):
        return [], limited, unavailable
    ranked = []
    # Model calls can be slow. Recheck eligibility after ranking outside the DB lock.
    for artifact, segments in sources:
        for window, end in transcript_windows(segments):
            vector = unit_vector(embed(' '.join(segment['text'] for segment in window)))
            score = sum(a * b for a, b in zip(question, vector, strict=True))
            if any(vector):
                ranked.append((score, artifact, window, end))
    ranked.sort(key=lambda match: match[0], reverse=True)

    matches = []
    with service.transaction() as conn:
        current = transcript_candidates(conn, service.clock(), filters, values)
        current_ids = {artifact['id'] for artifact in current[:MAX_CALLS]}
        for _, artifact, window, end in ranked:
            if artifact['id'] not in current_ids or artifact['expires_at'] <= service.clock():
                continue
            source = f"/recordings/{artifact['recording_id']}/transcripts/{artifact['id']}"
            matches.append({
                'recording_id': artifact['recording_id'], 'artifact_id': artifact['id'],
                'counterparty': artifact['counterparty'], 'call_started_at': artifact['call_started_at'],
                'start': window[0]['start'], 'end': end,
                'segments': [dict(segment, source_url=f"{source}#segment-{segment['segment_id']}")
                             for segment in window],
            })
            if len(matches) == MAX_RESULTS:
                break
    return matches, limited, unavailable
