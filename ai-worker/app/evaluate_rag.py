"""Evaluate scoped BGE-M3 retrieval and local Qwen answers on synthetic calls."""

import argparse
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from math import hypot, isfinite
from pathlib import Path
import re
import sys
import time
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from ollama import ResponseError
from pydantic import Field, model_validator

from app.analyzer import MODEL_NAME, compact_json, local_client
from app.evaluate_analysis import FIXTURE, model_identity
from app.schemas import StrictModel, Text


EMBED_MODEL = "bge-m3:567m"
EMBED_DIGEST = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"
TOP_K = 5
ANSWER_OPTIONS = {"temperature": 0, "num_ctx": 8192, "num_predict": 768}
PROMPT_VERSION = "rag-v10"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
NUMBER_QUESTION = re.compile(r"몇|얼마")
AGREEMENT_WORDS = re.compile(r"확정|합의|결정|승인|정했|정한")
UNRESOLVED_VALUE = re.compile(
    r"미정|(?:확정|합의|결정|정)하지\s*않|아직[^.!?]{0,24}모[르릅]"
    r"|확인[^.!?]{0,20}(?:뒤|후)[^.!?]{0,24}(?:정하|결정)"
)


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


def calls_in_scope(calls, filters, zone):
    lower, upper = (date.fromisoformat(filters[key]) for key in ("from_date", "to_date"))
    return {
        call["id"] for call in calls
        if call["counterparty"] == filters["counterparty"]
        and lower <= datetime.fromisoformat(call["call_started_at"]).astimezone(zone).date() <= upper
    }


def source_windows(calls, width):
    for call in calls:
        segments = call["segments"]
        for start in range(max(1, len(segments) - width + 1)):
            yield {
                "call_id": call["id"], "counterparty": call["counterparty"],
                "call_started_at": call["call_started_at"], "segments": segments[start:start + width],
            }


def unit_vector(vector):
    length = hypot(*vector)
    if not isfinite(length) or length == 0:
        raise ValueError("Invalid BGE-M3 vector")
    return [value / length for value in vector]


def rank_windows(query, windows, embeddings, scope):
    scores = [
        (sum(x * y for x, y in zip(query, vector, strict=True)), index)
        for index, (window, vector) in enumerate(zip(windows, embeddings, strict=True))
        if window["call_id"] in scope
    ]
    scores.sort(key=lambda entry: (-entry[0], entry[1]))
    return [windows[index] for _, index in scores[:TOP_K]]


def retrieved_ids(windows):
    return {segment["id"] for window in windows for segment in window["segments"]}


def request_response(client, prompt, payload, schema, *, allowed_citations=None):
    response_format = schema.model_json_schema()
    if allowed_citations is not None:
        response_format["properties"]["segment_ids"]["items"]["enum"] = sorted(allowed_citations)
    response = client.chat(
        model=MODEL_NAME, think=False, stream=False, options=ANSWER_OPTIONS,
        format=response_format,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": compact_json(payload)},
        ],
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


def answer_question(client, question, matches, prompts):
    if not matches:
        return GroundedAnswer(answer=None, reason="검색 범위에 답할 수 있는 발화가 없습니다.", segment_ids=[])
    by_source = {
        segment["id"]: {**segment, "call_id": window["call_id"], "call_started_at": window["call_started_at"]}
        for window in matches for segment in window["segments"]
    }
    evidence = sorted(by_source.values(), key=lambda row: (
        datetime.fromisoformat(row["call_started_at"]), row["call_id"], row["start"],
    ))
    query = question["text"]
    payload = {"question": query, "scope": question["filters"], "evidence": evidence}
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
    if answer.answer is not None and not citations:
        raise ValueError("Invalid answer evidence")
    return GroundedAnswer(answer=answer.answer, reason=answer.reason, segment_ids=citations)


def evaluate(data, width, port, prompts):
    calls, questions = data["calls"], data["questions"]
    zone = ZoneInfo(data["timezone"])
    windows = list(source_windows(calls, width))
    texts = [" ".join(segment["text"] for segment in window["segments"]) for window in windows]
    with local_client(port, 180) as client:
        vectors = client.embed(
            model=EMBED_MODEL, input=texts + [q["text"] for q in questions], truncate=False, keep_alive=0,
        ).embeddings
    if len(vectors) != len(texts) + len(questions):
        raise ValueError("Missing embedding")
    embeddings = [unit_vector(vector) for vector in vectors]
    document_vectors = embeddings[:len(texts)]
    query_vectors = embeddings[len(texts):]
    with local_client(port, 180) as client:
        for question, query_vector in zip(questions, query_vectors, strict=True):
            started = time.monotonic()
            scope = calls_in_scope(calls, question["filters"], zone)
            matches = rank_windows(query_vector, windows, document_vectors, scope)
            required = set(question["required_evidence_ids"])
            row = {
                "question_id": question["id"], "question": question["text"],
                "answerable": question["answerable"], "expected_facts": question["expected_facts"],
                "abstention_reason": question["abstention_reason"],
                "required_evidence_ids": question["required_evidence_ids"],
                "top5": [[segment["id"] for segment in window["segments"]] for window in matches],
                "retrieval_hit": required.issubset(retrieved_ids(matches)) if question["answerable"] else None,
            }
            try:
                answer = answer_question(client, question, matches, prompts)
            except (ConnectionError, ResponseError, httpx.HTTPError, ValueError) as error:
                row.update(answer_error=type(error).__name__, citation_and_status_match=False)
            else:
                row.update(answer.model_dump())
                row["citation_and_status_match"] = (
                    (answer.answer is not None) == question["answerable"]
                    and required.issubset(answer.segment_ids)
                )
            row["seconds"] = round(time.monotonic() - started, 3)
            yield row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("split", choices=("development", "final"))
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--window-segments", type=int, choices=(1, 2), default=1)
    parser.add_argument("--development-report", type=Path, help="Completed JSONL report to verify final settings")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("invalid loopback port")
    if args.split == "final" and args.development_report is None:
        parser.error("final evaluation requires the completed development report")
    raw = FIXTURE.with_name(f"rag-{args.split}.json").read_bytes()
    data = json.loads(raw)
    if data.get("synthetic") is not True or data.get("split") != args.split:
        parser.error("expected the selected synthetic fixture")
    try:
        prompts = load_prompts()
        identity = model_identity(args.port)
        with local_client(args.port, 5) as client:
            bge = next(model for model in client.list().models if model.model == EMBED_MODEL)
        if bge.digest != EMBED_DIGEST or bge.details is None:
            raise ValueError("BGE-M3 identity differs from the fixed evaluation settings")
        settings = identity | {
            "embed_model": EMBED_MODEL, "embed_digest": bge.digest,
            "embed_quantization": bge.details.quantization_level,
            "chat_model": MODEL_NAME, "answer_options": ANSWER_OPTIONS, "think": False,
            "prompt_version": PROMPT_VERSION,
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "answer_prompt_sha256": hashlib.sha256(prompts.answer.encode()).hexdigest(),
            "review_prompt_sha256": hashlib.sha256(prompts.review.encode()).hexdigest(),
            "evidence_prompt_sha256": hashlib.sha256(prompts.evidence.encode()).hexdigest(),
            "schema_sha256": hashlib.sha256(compact_json(GroundedAnswer.model_json_schema()).encode()).hexdigest(),
            "review_schema_sha256": hashlib.sha256(compact_json(AnswerReview.model_json_schema()).encode()).hexdigest(),
            "evidence_schema_sha256": hashlib.sha256(compact_json(EvidenceDecision.model_json_schema()).encode()).hexdigest(),
            "window_segments": args.window_segments, "top_k": TOP_K,
        }
        if args.split == "final":
            rows = [json.loads(line) for line in args.development_report.read_text(encoding="utf-8").splitlines()]
            if (len(rows) != 22 or rows[0].get("split") != "development"
                    or rows[0].get("settings") != settings or "retrieval_hits" not in rows[-1]):
                raise ValueError("Final settings differ from the completed development report")
        print(json.dumps({
            "split": args.split,
            "fixture_sha256": hashlib.sha256(raw).hexdigest(),
            "settings": settings,
        }, ensure_ascii=False), flush=True)
        hits = checked = answered = 0
        for row in evaluate(data, args.window_segments, args.port, prompts):
            print(json.dumps(row, ensure_ascii=False), flush=True)
            hits += row["retrieval_hit"] is True
            answered += row["answerable"]
            checked += row["citation_and_status_match"]
        print(json.dumps({"retrieval_hits": hits, "answerable": answered, "citation_and_status_matches": checked}))
    except (OSError, ResponseError, httpx.HTTPError, ValueError, StopIteration) as error:
        print(f"FAIL: RAG evaluation ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
