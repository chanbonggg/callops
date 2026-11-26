"""Evaluate scoped BGE-M3 retrieval and local Qwen answers on synthetic calls."""

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import httpx
from ollama import ResponseError

from app import rag
from app.analyzer import MODEL_NAME, compact_json, local_client
from app.evaluate_analysis import FIXTURE, model_identity


TOP_K = 5


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


def answer_question(client, question, matches, prompts):
    by_source = {
        segment["id"]: {**segment, "call_id": window["call_id"], "call_started_at": window["call_started_at"]}
        for window in matches for segment in window["segments"]
    }
    return rag.generate_answer(client, question["text"], question["filters"], by_source.values(), prompts)


def evaluate(data, width, port, prompts):
    calls, questions = data["calls"], data["questions"]
    zone = ZoneInfo(data["timezone"])
    windows = list(source_windows(calls, width))
    texts = [" ".join(segment["text"] for segment in window["segments"]) for window in windows]
    with local_client(port, 180) as client:
        embeddings = rag.embed_texts(client, texts + [q["text"] for q in questions])
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
        prompts = rag.load_prompts()
        identity = model_identity(args.port)
        with local_client(args.port, 5) as client:
            bge = next(model for model in client.list().models if model.model == rag.EMBED_MODEL)
        if bge.digest != rag.EMBED_DIGEST or bge.details is None:
            raise ValueError("BGE-M3 identity differs from the fixed evaluation settings")
        settings = identity | {
            "embed_model": rag.EMBED_MODEL, "embed_digest": bge.digest,
            "embed_quantization": bge.details.quantization_level,
            "chat_model": MODEL_NAME, "answer_options": rag.ANSWER_OPTIONS, "think": False,
            "answer_prompt_budget": rag.ANSWER_PROMPT_BUDGET,
            "prompt_version": rag.PROMPT_VERSION,
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "rag_runtime_sha256": hashlib.sha256(Path(rag.__file__).read_bytes()).hexdigest(),
            "answer_prompt_sha256": hashlib.sha256(prompts.answer.encode()).hexdigest(),
            "review_prompt_sha256": hashlib.sha256(prompts.review.encode()).hexdigest(),
            "evidence_prompt_sha256": hashlib.sha256(prompts.evidence.encode()).hexdigest(),
            "schema_sha256": hashlib.sha256(compact_json(rag.GroundedAnswer.model_json_schema()).encode()).hexdigest(),
            "review_schema_sha256": hashlib.sha256(compact_json(rag.AnswerReview.model_json_schema()).encode()).hexdigest(),
            "evidence_schema_sha256": hashlib.sha256(compact_json(rag.EvidenceDecision.model_json_schema()).encode()).hexdigest(),
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
