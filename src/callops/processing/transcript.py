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
        if set(payload) != ENVELOPE_FIELDS | {'events'}:
            raise ValueError('Invalid synthetic analysis result')
    else:
        raise ValueError('Unsupported result schema')
    return payload
