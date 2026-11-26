"""Validate the shared artifact format on upload and on every authorized read."""

import re

ENVELOPE_FIELDS = {'schema_version', 'job_id', 'attempt_id', 'input_artifact_id',
                   'input_artifact_version', 'input_sha256', 'synthetic'}
TRANSCRIPT_WARNINGS = {
    'NO_SPEECH': '음성 발화를 찾지 못했습니다.',
    'LOW_AUDIO_CONTRAST': '잡음과 발화가 구분되는지 원음을 확인해 주세요.',
    'NARROWBAND_AMR': '협대역 녹음의 전사 내용을 확인해 주세요.',
    'SPEECH_OVERLAP': '일부 발화가 겹쳐 있습니다.',
    'SPEAKER_UNCERTAIN': '일부 구간의 화자를 확인할 수 없습니다.',
}


def validate_segments(segments, duration, *, with_speaker=False):
    if (not isinstance(segments, list) or len(segments) > 10000
            or type(duration) not in (int, float) or not 0 < duration <= 7200):
        raise ValueError('Invalid segments')
    previous_start, text_size = 0, 0
    fields = {'segment_id', 'start', 'end', 'text'}
    if with_speaker:
        fields.add('speaker')
    for number, segment in enumerate(segments, start=1):
        if (not isinstance(segment, dict) or set(segment) != fields
                or type(segment['segment_id']) is not int or segment['segment_id'] != number
                or not isinstance(segment['text'], str)):
            raise ValueError('Invalid segment')
        if with_speaker and (segment['speaker'] not in (None, 'A', 'B') or not segment['text'].strip()):
            raise ValueError('Invalid speaker or speech')
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


def validate_evidence(item, text_field, limit):
    if not isinstance(item, dict) or set(item) != {text_field, 'segment_ids'}:
        raise ValueError('Invalid evidence item')
    text, ids = item[text_field], item['segment_ids']
    if (not isinstance(text, str) or not text.strip() or len(text) > limit
            or not isinstance(ids, list) or not 1 <= len(ids) <= 10
            or any(type(segment_id) is not int or segment_id < 1 for segment_id in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Invalid evidence text or IDs')
    text.encode('utf-8')


def validate_model_metadata(payload):
    name, seconds = payload.get('model_name'), payload.get('processing_seconds')
    if (not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', name) is None
            or type(seconds) not in (int, float) or not 0 <= seconds <= 3600):
        raise ValueError('Invalid model metadata')


def validate_result(payload, stage, duration):
    if (not isinstance(payload, dict) or type(payload.get('synthetic')) is not bool
            or type(payload.get('schema_version')) is not int):
        raise ValueError('Invalid result')
    real = not payload['synthetic']
    fields = ENVELOPE_FIELDS
    if real:
        validate_model_metadata(payload)
        fields = fields | {'model_name', 'processing_seconds'}
    if payload['schema_version'] == 1:
        if (real or set(payload) != fields | {'text'}
                or not isinstance(payload['text'], str) or len(payload['text']) > 100000):
            raise ValueError('Invalid legacy result')
        payload['text'].encode('utf-8')
    elif payload['schema_version'] == 2 and stage == 'STT':
        fields = fields | {'segments'}
        if real:
            fields |= {'warnings', 'language', 'duration_seconds'}
        if set(payload) != fields:
            raise ValueError('Invalid timestamped result')
        if real:
            warnings, decoded = payload['warnings'], payload['duration_seconds']
            if (payload['language'] != 'ko' or type(decoded) not in (int, float) or not 0 < decoded <= 7200
                    or not isinstance(warnings, list) or len(warnings) > len(TRANSCRIPT_WARNINGS)
                    or any(not isinstance(code, str) or code not in TRANSCRIPT_WARNINGS for code in warnings)
                    or len(set(warnings)) != len(warnings)
                    or ('NO_SPEECH' in warnings) != (payload['segments'] == [])):
                raise ValueError('Invalid transcription metadata')
            if not 0 < duration <= 7200:
                raise ValueError('Invalid original audio duration')
            duration = min(duration, decoded)
        validate_segments(payload['segments'], duration, with_speaker=real)
    elif payload['schema_version'] == 2 and stage == 'ANALYSIS':
        fields = fields | {'events'}
        if real:
            fields |= {'summary', 'tasks', 'prompt_version', 'needs_review', 'contract_mentions'}
            version = payload.get('prompt_version')
            if set(payload) != fields or not isinstance(version, str) or re.fullmatch(r'analysis-v[0-9]{1,4}', version) is None:
                raise ValueError('Invalid model analysis result')
            for name, field in (('needs_review', 'reason'), ('contract_mentions', 'text')):
                items = payload[name]
                if not isinstance(items, list) or len(items) > 100:
                    raise ValueError('Invalid review items')
                for item in items:
                    validate_evidence(item, field, 600)
        elif not fields <= payload.keys() <= fields | {'summary', 'tasks'}:
            raise ValueError('Invalid synthetic analysis result')
        if 'summary' in payload:
            validate_evidence(payload['summary'], 'text', 4000)
    else:
        raise ValueError('Unsupported result schema')
    return payload
