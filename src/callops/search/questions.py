"""Extractive question preview over approved transcripts."""

import re
from unicodedata import normalize

from ..calls.service import audio_expired
from ..common import Conflict
from .retrieval import retrieve_evidence


MEETING = re.compile(r'미팅|회의|면담|약속|방문|뵙|만나')
DATE = re.compile(r'\d{1,2}\s*월\s*\d{1,2}\s*일|(?:다음\s*주\s*)?[월화수목금토일]요일')
UNCERTAIN = re.compile(r'취소|정정|변경|옮기|미정|어렵|아직|보류|안\s*될')
PARTY = re.compile(r'^(.{1,100}?)(?:이랑|랑|하고|와|과)\s+(?:잡은\s+)?(.*?)(?:미팅|회의|면담|약속|방문)')
WORD = re.compile(r'\w+')


def answer_question(service, q, counterparty='', from_date='', to_date=''):
    question = normalize('NFC', q).strip().casefold()
    meeting = bool(MEETING.search(question) and '언제' in question)
    party = PARTY.match(question) if meeting and not counterparty else None
    if party:
        counterparty = re.sub(r'^(?:내가|저희가|제가)\s+', '', party[1])
    topic = WORD.findall(party[2]) if party else []
    words = sorted(set(WORD.findall(question)) - {'언제', '언제였지', '뭐였지', '어떻게', '알려줘'})

    # shortcut: lexical evidence preview, replace with model retrieval and grounded answers after GPU integration.
    def embed(text):
        if text == q.strip():
            return (1,) if meeting else tuple(1 for _ in words) or (0,)
        text = normalize('NFC', text).casefold()
        if meeting:
            return (int(bool(MEETING.search(text) and all(word in text for word in topic))),)
        return tuple(int(word in text) for word in words) or (0,)

    matches, limited, unavailable = retrieve_evidence(
        service, q, embed=embed, counterparty=counterparty, from_date=from_date, to_date=to_date)
    evidence = {}
    for match in matches:
        uncertain = any(UNCERTAIN.search(s['text']) for s in match['segments'])
        for segment in match['segments']:
            if meeting and not (DATE.search(segment['text']) or UNCERTAIN.search(segment['text'])):
                continue
            if not meeting and not any(word in normalize('NFC', segment['text']).casefold() for word in words):
                continue
            evidence.setdefault(segment['source_url'], (match, segment, uncertain))
    choices = list(evidence.values())
    ambiguous = len(choices) > 1
    checked = []
    for match, segment, uncertain in choices[:3]:
        try:
            row, _, _ = service.transcript(match['recording_id'], match['artifact_id'])
            if (segment['end'] > segment['start']
                    and not audio_expired(row, service.clock())
                    and service.audio_path(row).is_file()):
                transcript_url = segment['source_url'].split('#', 1)[0]
                segment['audio_url'] = f"{transcript_url}/audio/{segment['segment_id']}"
        except (Conflict, OSError):
            unavailable = True
            continue
        checked.append((match, segment, uncertain))
    choices = checked
    if not choices:
        return 'missing', [], limited, unavailable
    if meeting and ambiguous:
        return 'clarify', choices, limited, unavailable
    if meeting and (limited or unavailable or choices[0][2] or not DATE.search(choices[0][1]['text'])):
        return 'uncertain', choices, limited, unavailable
    return 'evidence', choices, limited, unavailable
