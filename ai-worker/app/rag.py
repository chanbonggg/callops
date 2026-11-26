"""Local BGE-M3 embeddings and Qwen answers over caller-selected evidence."""

from dataclasses import dataclass
from datetime import date, datetime
from math import hypot, isfinite
from pathlib import Path
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.analyzer import MODEL_NAME, TEMPLATE_TOKENS, byte_size, compact_json
from app.schemas import Speaker, StrictModel, Text


EMBED_MODEL = "bge-m3:567m"
EMBED_DIGEST = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"
EMBED_DIMENSIONS = 1024
MAX_EMBED_BYTES = 4096
ANSWER_OPTIONS = {"temperature": 0, "num_ctx": 8192, "num_predict": 768}
ANSWER_PROMPT_BUDGET = ANSWER_OPTIONS["num_ctx"] - ANSWER_OPTIONS["num_predict"] - TEMPLATE_TOKENS
PROMPT_VERSION = "rag-v10"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
CALL_TIMEZONE = ZoneInfo("Asia/Seoul")
NUMBER_QUESTION = re.compile(r"몇|얼마")
AGREEMENT_WORDS = re.compile(r"확정|합의|결정|승인|정했|정한")
UNRESOLVED_VALUE = re.compile(
    r"미정|(?:확정|합의|결정|정)하지\s*않|아직[^.!?]{0,24}모[르릅]"
    r"|확인[^.!?]{0,20}(?:뒤|후)[^.!?]{0,24}(?:정하|결정)"
)

SourceID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_:-]{1,80}$")]


class RagRequest(StrictModel):
    schema_version: int = Field(ge=1, le=1)


class EmbeddingRequest(RagRequest):
    texts: list[Text] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def check_text_bytes(self):
        if any(byte_size(text) > MAX_EMBED_BYTES for text in self.texts):
            raise ValueError("Embedding text exceeds its byte limit")
        return self


class AnswerScope(StrictModel):
    counterparty: Annotated[Text, Field(max_length=100)] | None = None
    from_date: date | None = None
    to_date: date | None = None

    @model_validator(mode="after")
    def check_dates(self):
        if self.from_date is not None and self.to_date is not None and self.from_date > self.to_date:
            raise ValueError("Invalid date range")
        return self


class AnswerEvidence(StrictModel):
    id: SourceID
    call_id: SourceID
    call_started_at: AwareDatetime | None = None
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: Speaker | None = None
    text: Text

    @model_validator(mode="after")
    def check_times(self):
        if self.end < self.start:
            raise ValueError("Invalid evidence time range")
        return self

    def prompt_row(self):
        call_time = self.call_started_at.astimezone(CALL_TIMEZONE).isoformat() if self.call_started_at else None
        return self.model_dump(mode="json") | {"call_started_at": call_time}


class AnswerRequest(RagRequest):
    question: Annotated[Text, Field(max_length=200)]
    scope: AnswerScope = Field(default_factory=AnswerScope)
    evidence: list[AnswerEvidence] = Field(max_length=10)

    @model_validator(mode="after")
    def check_source_ids(self):
        if len({row.id for row in self.evidence}) != len(self.evidence):
            raise ValueError("Repeated evidence ID")
        return self


class ContextTooLong(ValueError):
    pass


@dataclass(frozen=True)
class RagPrompts:
    answer: str
    review: str
    evidence: str


def load_prompts():
    return RagPrompts(
        answer=(PROMPTS_DIR / "rag_v2.txt").read_text(encoding="utf-8").strip(),
        review=(PROMPTS_DIR / "rag_review_v7.txt").read_text(encoding="utf-8").strip(),
        evidence=(PROMPTS_DIR / "rag_evidence_v8.txt").read_text(encoding="utf-8").strip(),
    )


class GroundedAnswer(StrictModel):
    answer: Text | None
    reason: Text | None
    segment_ids: list[Text] = Field(max_length=10)

    @model_validator(mode="after")
    def check_abstention_reason(self):
        if (self.answer is None) == (self.reason is None):
            raise ValueError("Expected an answer or an abstention reason")
        return self


class AnswerReview(StrictModel):
    additional_segment_ids: list[Text] = Field(max_length=10)
    correction: GroundedAnswer | None


class EvidenceDecision(StrictModel):
    status: Literal["supported", "unavailable"]
    reason: Text | None
    segment_ids: list[Text] = Field(max_length=10)

    @model_validator(mode="after")
    def check_evidence(self):
        if self.status == "supported" and (self.reason is not None or not self.segment_ids):
            raise ValueError("An answerable question needs evidence")
        if self.status == "unavailable" and self.reason is None:
            raise ValueError("An unavailable answer needs a grounded reason")
        return self


def embed_texts(client, texts, *, keep_alive=0):
    vectors = client.embed(model=EMBED_MODEL, input=texts, truncate=False, keep_alive=keep_alive).embeddings
    if len(vectors) != len(texts):
        raise ValueError("Unexpected number of embeddings")
    embeddings = []
    for vector in vectors:
        if len(vector) != EMBED_DIMENSIONS:
            raise ValueError("Unexpected embedding dimension")
        length = hypot(*vector)
        if not isfinite(length) or length == 0:
            raise ValueError("Invalid embedding norm")
        embeddings.append([value / length for value in vector])
    return embeddings


def request_response(client, prompt, payload, schema, *, allowed_citations=None):
    content = compact_json(payload)
    if byte_size(prompt) + byte_size(content) > ANSWER_PROMPT_BUDGET:
        raise ContextTooLong("Question context exceeds its byte budget")
    response_format = schema.model_json_schema()
    if allowed_citations is not None:
        response_format["properties"]["segment_ids"]["items"]["enum"] = sorted(allowed_citations)
    response = client.chat(
        model=MODEL_NAME, think=False, stream=False, options=ANSWER_OPTIONS,
        format=response_format,
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": content}],
    )
    if not response.done or response.done_reason != "stop" or response.message.tool_calls:
        raise ValueError("Incomplete response")
    return schema.model_validate_json(response.message.content)


def validate_citations(segment_ids, sources):
    ids = set(segment_ids)
    if len(ids) != len(segment_ids) or not ids.issubset(sources):
        raise ValueError("Invalid answer evidence")


def draft_answer(client, prompt, payload, sources):
    draft = request_response(client, prompt, payload, GroundedAnswer)
    try:
        validate_citations(draft.segment_ids, sources)
    except ValueError:
        draft = request_response(client, prompt, payload, GroundedAnswer, allowed_citations=sources)
        validate_citations(draft.segment_ids, sources)
    if draft.answer is not None and not draft.segment_ids:
        raise ValueError("Invalid answer evidence")
    return draft


def no_evidence_answer():
    return GroundedAnswer(answer=None, reason="검색 범위에 답할 수 있는 발화가 없습니다.", segment_ids=[])


def source_order(row):
    when = row["call_started_at"]
    return (when is None, datetime.fromisoformat(when) if when else None, row["call_id"], row["start"])


def generate_answer(client, query, scope, evidence, prompts):
    if not evidence:
        return no_evidence_answer()
    by_source = {row["id"]: row for row in evidence}
    evidence = sorted(by_source.values(), key=source_order)
    payload = {"question": query, "scope": scope, "evidence": evidence}
    unresolved_ids = {row["id"] for row in evidence if UNRESOLVED_VALUE.search(row["text"])}
    if unresolved_ids and NUMBER_QUESTION.search(query) and AGREEMENT_WORDS.search(query):
        decision = request_response(client, prompts.evidence, payload, EvidenceDecision)
        validate_citations(decision.segment_ids, by_source)
        if decision.status == "unavailable" and unresolved_ids.intersection(decision.segment_ids):
            quotes = " ".join(f'“{by_source[segment_id]["text"]}”' for segment_id in decision.segment_ids)
            return GroundedAnswer(
                answer=None, reason=f"통화에서 확정된 수치를 확인할 수 없습니다. {quotes}",
                segment_ids=decision.segment_ids,
            )

    draft = draft_answer(client, prompts.answer, payload, by_source)
    review = request_response(client, prompts.review, payload | {"draft": draft.model_dump()}, AnswerReview)
    answer = review.correction if review.correction is not None else draft
    validate_citations(answer.segment_ids, by_source)
    validate_citations(review.additional_segment_ids, by_source)
    citations = list(dict.fromkeys([*answer.segment_ids, *review.additional_segment_ids]))
    if not citations:
        if answer.answer is not None:
            raise ValueError("Invalid answer evidence")
        return no_evidence_answer()
    return GroundedAnswer(answer=answer.answer, reason=answer.reason, segment_ids=citations)
