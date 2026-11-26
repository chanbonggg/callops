"""Extract and reconcile review candidates from speaker-labelled transcript segments."""

from dataclasses import dataclass
from functools import cache
import hashlib
import json
from pathlib import Path
import re
import time

import httpx
from ollama import Client, ResponseError
from pydantic import ValidationError

from app.analysis_time import DATE_PHRASES, call_clock, date_choices, event_times, revised_event, task_deadline
from app.schemas import CallAnalysis, Event, Extraction, ReviewItem, Summary, Task, Transcript


MODEL_NAME = "qwen3:8b"
MODEL_DIGEST = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
PROMPT_VERSION = "analysis-v6"
OPTIONS = {"temperature": 0, "num_ctx": 8192, "num_predict": 2048}
THINK = False
MAX_CHUNKS = 8
ANALYSIS_TIMEOUT = 30 * 60
# Qwen's byte-level tokenizer uses at most one token per UTF-8 byte.
# Reserve extra space for the Ollama chat template and special tokens.
TEMPLATE_TOKENS = 256
PROMPT_BUDGET = OPTIONS["num_ctx"] - OPTIONS["num_predict"] - TEMPLATE_TOKENS
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
ORDINAL = re.compile(r"(?:첫|두|세|네|[0-9]+)\s*번째")


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@cache
def load_system_prompt():
    filename = PROMPT_VERSION.replace("-", "_") + ".txt"
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def analysis_settings():
    schema = Extraction.model_json_schema()
    prompt = load_system_prompt()
    return {
        "model": MODEL_NAME,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "schema_version": CallAnalysis.model_fields["schema_version"].default,
        "schema_sha256": hashlib.sha256(compact_json(schema).encode()).hexdigest(),
        "date_grammar": "literal_choices_for_abbreviated_days",
        "options": OPTIONS.copy(),
        "think": THINK,
        "prompt_byte_budget": PROMPT_BUDGET,
        "max_chunks": MAX_CHUNKS,
        "timeout_seconds": ANALYSIS_TIMEOUT,
    }


@dataclass(frozen=True)
class RequestRecord:
    stage: str
    segment_ids: tuple[int, ...]
    start: float
    end: float
    prompt_bytes: int
    done_reason: str
    prompt_tokens: int | None
    output_tokens: int | None
    load_ns: int | None
    total_ns: int | None


@dataclass(frozen=True)
class AnalysisRun:
    result: CallAnalysis
    requests: tuple[RequestRecord, ...]


class AnalysisFailure(ValueError):
    def __init__(self, code, stage, segment_ids=(), requests=()):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.segment_ids = tuple(segment_ids)
        self.requests = tuple(requests)


def local_client(port, timeout):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid Ollama loopback port")
    return Client(host=f"http://127.0.0.1:{port}", timeout=timeout,
                  follow_redirects=False, trust_env=False)


def byte_size(value):
    return len(value.encode("utf-8"))


def transcript_content(segments):
    return compact_json({"segments": [segment.model_dump(mode="json") for segment in segments]})


def extraction_schema(segments):
    schema = Extraction.model_json_schema()
    choices = date_choices(segments)
    if choices:
        # When full dates and abbreviated days coexist, the grammar can
        # prevent the model from joining the year/month to a different day.
        schema["$defs"]["ExtractedEvent"]["properties"]["date_text"] = {"enum": [*choices, None]}
    return schema


def plan_chunks(segments, prompt, chunk_segments=None):
    if chunk_segments is not None and chunk_segments < 1:
        raise ValueError("Invalid chunk segment limit")
    # A comma per segment except the first, plus the wrapper and system prompt.
    capacity = PROMPT_BUDGET - byte_size(prompt) - byte_size('{"segments":[]}')
    sizes = [byte_size(compact_json(segment.model_dump(mode="json"))) + 1 for segment in segments]
    chunks = []
    start = 0
    end = 0
    while end < len(segments):
        if len(chunks) == MAX_CHUNKS:
            raise AnalysisFailure("INPUT_TOO_LONG", "planning", [segments[start].segment_id])
        end = start
        remaining = capacity + 1
        while end < len(segments) and sizes[end] <= remaining:
            if chunk_segments is not None and end - start >= chunk_segments:
                break
            remaining -= sizes[end]
            end += 1
        if end == start:
            raise AnalysisFailure("INPUT_TOO_LONG", "planning", [segments[start].segment_id])
        chunks.append(segments[start:end])
        if (end < len(segments) and end - start > 1
                and sizes[end - 1] + sizes[end] <= capacity + 1):
            start = end - 1
        else:
            start = end
    return chunks


def evidence_texts(item, segments):
    ids = set(item.segment_ids)
    return [segment.text for segment_id, segment in segments.items() if segment_id in ids]


def valid_evidence_ids(extraction, segments):
    for item in (extraction.summary, *(item for group in extraction.items() for item in group)):
        ids = set(item.segment_ids)
        if len(ids) != len(item.segment_ids) or not ids.issubset(segments):
            return False
    return all(task.assignee_segment_id is None or task.assignee_segment_id in task.segment_ids
               for task in extraction.tasks)


def resolve_cancellation(extraction, segments):
    if len(extraction.events) != 2:
        return extraction
    original, cancelled = sorted(extraction.events, key=lambda event: event.status == "cancelled")
    if original.status == "cancelled" or cancelled.status != "cancelled" or original.title != cancelled.title:
        return extraction
    if (cancelled.date_text not in {None, original.date_text}
            or cancelled.time_text not in {None, original.time_text}):
        return extraction
    cue = re.compile(
        rf"방금\s*(?:정한|합의한|잡은)\s+{re.escape(original.title)}(?:은|는|을|를)\s*"
        r"취소(?:합니다|하겠습니다|하죠|했습니다)(?=[\s.!?]|$)"
    )
    original_ids = set(original.segment_ids)
    cancelled_ids = set(cancelled.segment_ids)
    earlier = min(segments[segment_id].start for segment_id in original_ids)
    if not any(segments[segment_id].start > earlier and cue.search(segments[segment_id].text)
               for segment_id in cancelled_ids):
        return extraction
    ids = [i for i in segments if i in original_ids or i in cancelled_ids]
    if len(ids) > 10:
        return extraction
    event = cancelled.model_copy(update={
        "date_text": original.date_text, "time_text": original.time_text, "segment_ids": ids,
    })
    return extraction.model_copy(update={"events": [event]})


def grounded_event(event, segments):
    texts = evidence_texts(event, segments)
    return all(not value or any(value in text for text in texts)
               for value in (event.date_text, event.time_text))


def separate_ordinal_evidence(extraction, segments):
    if len(extraction.events) < 2:
        return extraction
    # A turn explicitly naming just one numbered appointment and its date
    # and time can only support the matching appointment.
    owners = {}
    for segment_id, segment in segments.items():
        ordinal = ORDINAL.match(segment.text)
        if not ordinal or len(ORDINAL.findall(segment.text)) != 1:
            continue
        dates = [match[0] for match in DATE_PHRASES.finditer(segment.text)]
        if len(dates) != 1:
            continue
        matches = [
            index for index, event in enumerate(extraction.events)
            if ordinal[0] in event.title and event.date_text == dates[0]
            and event.time_text and event.time_text in segment.text
        ]
        if len(matches) == 1:
            owners[segment_id] = matches[0]
    if not owners:
        return extraction
    events = []
    for index, event in enumerate(extraction.events):
        ids = [segment_id for segment_id in event.segment_ids if owners.get(segment_id, index) == index]
        pruned = event.model_copy(update={"segment_ids": ids})
        events.append(pruned if ids and grounded_event(pruned, segments) else event)
    return extraction.model_copy(update={"events": events})


def grounded_values(extraction, segments):
    if any(not grounded_event(event, segments) for event in extraction.events):
        return False
    for task in extraction.tasks:
        if task.deadline_text and not any(task.deadline_text in text for text in evidence_texts(task, segments)):
            return False
    for mention in extraction.contract_mentions:
        if not any(mention.text in text for text in evidence_texts(mention, segments)):
            return False
    return True


def merge_duplicates(items):
    merged = []
    for item in items:
        value = item.model_dump(exclude={"segment_ids"})
        for index, existing in enumerate(merged):
            ids = sorted(set(existing.segment_ids) | set(item.segment_ids))
            if (set(existing.segment_ids).intersection(item.segment_ids) and len(ids) <= 10
                    and value == existing.model_dump(exclude={"segment_ids"})):
                merged[index] = existing.model_copy(update={"segment_ids": ids})
                break
        else:
            merged.append(item)
    return merged


def merge_preserves_evidence(extractions, final):
    # An unresolved event or task can move to human review. Verbatim contract
    # observations must survive as observations, including conflicting values.
    destinations = (
        final.events + final.needs_review, final.tasks + final.needs_review,
        final.contract_mentions, final.needs_review,
    )
    for extraction in extractions:
        for group, target in zip(extraction.items(), destinations, strict=True):
            for item in group:
                if not any(set(item.segment_ids).issubset(other.segment_ids)
                           and getattr(item, "text", None) == getattr(other, "text", None)
                           for other in target):
                    return False
    return True


def retain_chunk_reviews(extractions, final):
    reviews = list(final.needs_review)
    for extraction in extractions:
        for review in extraction.needs_review:
            if not any(set(review.segment_ids).issubset(item.segment_ids) for item in reviews):
                reviews.append(review)
    return final.model_copy(update={"needs_review": reviews})


def merge_evidence(extractions, segments):
    candidate_ids = {
        segment_id
        for part in extractions for group in (part.events, part.tasks, part.contract_mentions) for item in group
        for segment_id in item.segment_ids
    }
    context_ids = {
        segment_id
        for part in extractions for item in (part.summary, *part.needs_review)
        for segment_id in item.segment_ids
    }
    seen = set()
    evidence = []
    for segment in segments:
        value = (segment.speaker, segment.text)
        # Keep every candidate reference. Identical summary/review context
        # can share one turn in the prompt; interim reviews keep their IDs.
        if segment.segment_id in candidate_ids or (segment.segment_id in context_ids and value not in seen):
            evidence.append(segment)
            seen.add(value)
    return evidence


def normalize(extraction, transcript):
    segments = {segment.segment_id: segment for segment in transcript.segments}
    zone, local_date = call_clock(transcript)
    events = []
    tasks = []
    reviews = list(extraction.needs_review)
    summary = extraction.summary
    unique_events = merge_duplicates(extraction.events)
    for event in unique_events:
        evidence = evidence_texts(event, segments)
        if len(unique_events) == 1:
            event, changed = revised_event(event, local_date, evidence)
            if changed and event.status == "confirmed" and event.date_text not in summary.text:
                text = f"{summary.text} {event.title} 일정은 {event.date_text}로 변경되었습니다."
                ids = sorted(set(summary.segment_ids) | set(event.segment_ids))
                if len(ids) <= 10 and len(text) <= 4000:
                    summary = Summary(text=text, segment_ids=ids)
        start_at, end_at = event_times(event, zone, local_date, evidence)
        if event.status != "cancelled" and (event.status != "confirmed" or end_at is None):
            reviews.append(ReviewItem(reason="일정의 합의 여부 또는 시작·종료 시각 확인", segment_ids=event.segment_ids))
        events.append(Event(**event.model_dump(), start_at=start_at, end_at=end_at))
    for task in merge_duplicates(extraction.tasks):
        owner_id = task.assignee_segment_id
        assignee = segments[owner_id].speaker if owner_id is not None else None
        due_at = task_deadline(task.deadline_text, zone, local_date, evidence_texts(task, segments))
        if task.status != "cancelled" and (
            task.status != "confirmed" or assignee is None or (task.deadline_text and due_at is None)
        ):
            reviews.append(ReviewItem(reason="할 일의 합의 여부, 담당자 또는 기한 확인", segment_ids=task.segment_ids))
        tasks.append(Task(**task.model_dump(), assignee=assignee, due_at=due_at))
    return CallAnalysis(summary=summary, events=events, tasks=tasks,
                        contract_mentions=merge_duplicates(extraction.contract_mentions),
                        needs_review=merge_duplicates(reviews))


def analyze_transcript(transcript: Transcript, *, port=11434, chunk_segments=None, observe=None) -> AnalysisRun:
    if not isinstance(transcript, Transcript) or not transcript.segments:
        raise ValueError("Expected a nonempty structured transcript")
    deadline = time.monotonic() + ANALYSIS_TIMEOUT
    prompt = load_system_prompt()
    chunks = plan_chunks(transcript.segments, prompt, chunk_segments)
    requests = []
    def extract(stage, system, content, segments):
        scope = {segment.segment_id: segment for segment in segments}
        size = byte_size(system) + byte_size(content)
        if size > PROMPT_BUDGET:
            raise AnalysisFailure("PROMPT_TOO_LONG", stage, scope, requests)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AnalysisFailure("TIMEOUT", stage, scope, requests)
        try:
            with local_client(port, remaining) as client:
                response = client.chat(
                    model=MODEL_NAME, format=extraction_schema(segments), options=OPTIONS, think=THINK, stream=False,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                )
        except (TimeoutError, httpx.TimeoutException):
            raise AnalysisFailure("TIMEOUT", stage, scope, requests) from None
        except KeyboardInterrupt:
            raise AnalysisFailure("CANCELLED", stage, scope, requests) from None
        except ResponseError as error:
            message = str(error).lower()
            code = "OOM" if "out of memory" in message or "unable to allocate" in message else "MODEL_ERROR"
            raise AnalysisFailure(code, stage, scope, requests) from None
        except (ConnectionError, httpx.HTTPError, ValidationError):
            raise AnalysisFailure("MODEL_ERROR", stage, scope, requests) from None
        requests.append(RequestRecord(
            stage, tuple(scope), segments[0].start, max(segment.end for segment in segments),
            size, response.done_reason, response.prompt_eval_count, response.eval_count,
            response.load_duration, response.total_duration,
        ))
        if observe is not None:
            observe(requests[-1], response.message.content)
        if time.monotonic() >= deadline:
            raise AnalysisFailure("TIMEOUT", stage, scope, requests)
        if not response.done or response.done_reason != "stop" or getattr(response.message, "tool_calls", None):
            raise AnalysisFailure("INCOMPLETE_RESPONSE", stage, scope, requests)
        if (response.prompt_eval_count is not None
                and response.prompt_eval_count > size + TEMPLATE_TOKENS):
            raise AnalysisFailure("PROMPT_BUDGET_MISMATCH", stage, scope, requests)
        try:
            result = Extraction.model_validate_json(response.message.content)
        except ValidationError:
            raise AnalysisFailure("INVALID_JSON", stage, scope, requests) from None
        if not valid_evidence_ids(result, scope):
            raise AnalysisFailure("INVALID_EVIDENCE", stage, scope, requests)
        result = resolve_cancellation(result, scope)
        result = separate_ordinal_evidence(result, scope)
        if not grounded_values(result, scope):
            raise AnalysisFailure("UNGROUNDED_VALUE", stage, scope, requests)
        return result

    extractions = []
    for number, chunk in enumerate(chunks, 1):
        extractions.append(extract(f"chunk:{number}", prompt, transcript_content(chunk), chunk))
    final = extractions[0]
    if len(extractions) > 1:
        evidence = merge_evidence(extractions, transcript.segments)
        # Re-read the original evidence in call order. Repeating provisional
        # JSON in the prompt can make the model copy superseded candidates.
        final = extract("merge", prompt, transcript_content(evidence), evidence)
        final = retain_chunk_reviews(extractions, final)
        if not merge_preserves_evidence(extractions, final):
            raise AnalysisFailure("INCOMPLETE_MERGE", "merge", [s.segment_id for s in evidence], requests)
    result = normalize(final, transcript)
    if time.monotonic() >= deadline:
        raise AnalysisFailure("TIMEOUT", "finalize", requests=requests)
    return AnalysisRun(result, tuple(requests))


if __name__ == "__main__":
    import sys

    from app.evaluate_analysis import main

    raise SystemExit(main(["T01", *sys.argv[1:]]))
