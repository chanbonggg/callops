"""Envelopes for synthetic worker results."""

ENVELOPE_FIELDS = {'schema_version', 'job_id', 'attempt_id', 'input_artifact_id',
                   'input_artifact_version', 'input_sha256', 'synthetic'}


def validate_segments(segments, duration):
    if (not isinstance(segments, list) or len(segments) > 10000
            or type(duration) not in (int, float) or not 0 < duration <= 7200):
        raise ValueError('Invalid segments')
    previous_start, text_size = 0, 0
    for number, segment in enumerate(segments, start=1):
        if (not isinstance(segment, dict) or set(segment) != {'segment_id', 'start', 'end', 'text'}
                or type(segment['segment_id']) is not int or segment['segment_id'] != number
                or not isinstance(segment['text'], str)):
            raise ValueError('Invalid segment')
        segment['text'].encode('utf-8')
        start, end = segment['start'], segment['end']
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not previous_start <= start <= end <= duration):
            raise ValueError('Invalid timestamps')
        text_size += len(segment['text'])
        if text_size > 100000:
            raise ValueError('Transcript too large')
        previous_start = start
    return segments


def validate_result(payload, stage, duration):
    if (not isinstance(payload, dict) or payload.get('synthetic') is not True
            or type(payload.get('schema_version')) is not int):
        raise ValueError('Invalid result')
    if payload['schema_version'] == 1:
        if (set(payload) != ENVELOPE_FIELDS | {'text'}
                or not isinstance(payload['text'], str) or len(payload['text']) > 100000):
            raise ValueError('Invalid legacy result')
        payload['text'].encode('utf-8')
    elif payload['schema_version'] == 2 and stage == 'STT':
        if set(payload) != ENVELOPE_FIELDS | {'segments'}:
            raise ValueError('Invalid timestamped result')
        validate_segments(payload['segments'], duration)
    elif payload['schema_version'] == 2 and stage == 'ANALYSIS':
        fields = ENVELOPE_FIELDS | {'events'}
        if set(payload) not in (fields, fields | {'summary'}):
            raise ValueError('Invalid synthetic analysis result')
        if 'summary' in payload:
            summary = payload['summary']
            if not isinstance(summary, dict) or set(summary) != {'text', 'segment_ids'}:
                raise ValueError('Invalid summary')
            text, ids = summary['text'], summary['segment_ids']
            if (not isinstance(text, str) or not text.strip() or len(text) > 4000
                    or not isinstance(ids, list) or not 1 <= len(ids) <= 10
                    or any(type(segment_id) is not int or segment_id < 1 for segment_id in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError('Invalid summary')
            text.encode('utf-8')
    else:
        raise ValueError('Unsupported result schema')
    return payload
