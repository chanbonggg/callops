"""Compare local Qwen output with the fixed synthetic CallOps extraction cases."""

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import time

import httpx

from app.analyzer import MODEL_DIGEST, MODEL_NAME, AnalysisFailure, analysis_settings, analyze_transcript, local_client
from app.schemas import Transcript


FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "scenarios.json"
EDGES = FIXTURE.with_name("d4-analysis-edges.json")
OLLAMA_VERSION = "0.34.2"
QUANTIZATION = "Q4_K_M"
FIRST_CASES = ("T01", "T06", "T09")
SPLIT_CASES = {"T06": 1, "T08": 1, "T09": 1, "T10": 2}
LONG_CASES = {"T06", "T09"}
LONG_FILLER = (
    "테스트 환경의 상태는 정상입니다. 앞에서 만든 결과 파일은 지금 열려 있습니다.",
    "표의 머리글과 설명 문구를 확인했습니다. 그림의 배경은 회색입니다.",
    "공유한 문서의 첫 화면입니다. 지난 작업의 로그와 사용 안내가 나와 있습니다.",
    "본문의 개행과 문장부호를 확인했습니다. 현재 자료는 읽기 전용 화면에 표시됩니다.",
)
LONG_TURNS = 105


def load_cases():
    cases = {}
    for path in (FIXTURE, EDGES):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("synthetic") is not True:
            raise ValueError("Expected synthetic evaluation fixture")
        for case in data["cases"]:
            if "d4_speakers" in case:
                cases[case["id"]] = {
                    "call_started_at": data["call_started_at"], "timezone": data["timezone"], **case,
                }
    return cases


def make_transcript(case, *, long=False):
    script = re.sub(r"\[[^\]]+\]\s*", "", case["input"])
    if case.get("d4_prefix"):
        script = f'{case["d4_prefix"]} {script}'
    turns = list(zip(case["d4_speakers"], re.split(r"(?<=[.!?])\s+", script), strict=True))
    if long:
        filler = [("B", LONG_FILLER[i % len(LONG_FILLER)]) for i in range(LONG_TURNS)]
        turns = turns[:1] + filler + turns[1:]
    segments = [
        {"segment_id": index, "start": (index - 1) * 5, "end": index * 5, "speaker": speaker, "text": text}
        for index, (speaker, text) in enumerate(turns, 1)
    ]
    return Transcript.model_validate_json(json.dumps({
        "call_started_at": case["call_started_at"], "timezone": case["timezone"], "segments": segments,
    }))


def time_pair(event):
    return [value.isoformat() if value else None for value in (event.start_at, event.end_at)]


def score(case, result, *, long=False):
    case_id = case["id"]
    expected = case["expected"]
    issues = []
    wanted_count = expected.get("schedule_count", 1 if case_id == "T09" else 0)
    counts = {0, 1} if case_id == "T02" else {wanted_count}
    if len(result.events) not in counts:
        issues.append("WRONG_EVENT_COUNT")
    if case_id == "T02" and not result.events and "다음 주쯤" not in result.summary.text:
        if not any("다음 주쯤" in item.reason for item in result.needs_review):
            issues.append("MISSING_ORIGINAL_DATE")
    if case_id == "T06":
        day = datetime.fromisoformat(expected["start_at"]).date()
        if f"{day.day}일" not in result.summary.text and day.isoformat() not in result.summary.text:
            issues.append("SUMMARY_MISSING_CHANGED_DATE")
    if case_id == "T10":
        if sorted(time_pair(event) for event in result.events if event.start_at and event.end_at) != expected["time_pairs"]:
            issues.append("WRONG_EVENT_TIME")
    elif case_id == "T09" and any(event.status != "cancelled" for event in result.events):
        issues.append("ACTIVE_EVENT_AFTER_CANCELLATION")
    for event in result.events:
        if case_id == "T02":
            if event.status not in {"proposed", "uncertain"} or event.date_text != "다음 주쯤":
                issues.append("PROPOSAL_MUST_REMAIN_UNCERTAIN")
            if 1 not in event.segment_ids:
                issues.append("MISSING_EVENT_EVIDENCE")
        elif wanted_count == 1:
            if event.status != ("cancelled" if case_id == "T09" else "confirmed"):
                issues.append("WRONG_EVENT_STATUS")
            if "회의" not in event.title:
                issues.append("WRONG_EVENT_SUBJECT")
            ids = {1, 2 + LONG_TURNS if long else 2}
            if case_id == "T06":
                ids.add(3 + LONG_TURNS if long else 3)
            if not ids.issubset(event.segment_ids):
                issues.append("MISSING_EVENT_EVIDENCE")
        elif case_id == "T10":
            if "회의" not in event.title or event.status == "cancelled":
                issues.append("WRONG_EVENT_SUBJECT_OR_STATUS")
            if 2 not in event.segment_ids:
                issues.append("MISSING_EVENT_EVIDENCE")
            if time_pair(event) == expected["time_pairs"][0] and 3 in event.segment_ids:
                issues.append("UNRELATED_EVENT_EVIDENCE")
        if case_id not in {"T10", "T09"} and "start_at" in expected:
            if time_pair(event) != [expected["start_at"], expected["end_at"]]:
                issues.append("WRONG_EVENT_TIME")
        if case_id == "T09" and time_pair(event) != expected["cancelled_time_pair"]:
            issues.append("WRONG_CANCELLED_EVENT_TIME")
        if "date_text" in expected and expected["date_text"] != event.date_text:
            issues.append("MISSING_ORIGINAL_DATE")
    wanted_tasks = expected.get("tasks", [])
    if len(result.tasks) != len(wanted_tasks):
        issues.append("WRONG_TASK_COUNT")
    for task in wanted_tasks:
        if not any(
            task["segment_id"] in candidate.segment_ids
            and task["title_contains"] in candidate.title
            and task["owner"] == candidate.assignee
            and candidate.status in task["statuses"]
            and task["due_at"] == (candidate.due_at.isoformat() if candidate.due_at else None)
            for candidate in result.tasks
        ):
            issues.append(f'WRONG_TASK_{task["segment_id"]}')
    if case_id in {"T05", "T08"}:
        second_text = "확정하지 않았" if case_id == "T05" else "이십만 원"
        for segment_id, quote in [(1, "십만 원"), (2, second_text)]:
            pattern = rf"(?<![가-힣]){re.escape(quote)}"
            if not any(segment_id in item.segment_ids and re.search(pattern, item.text)
                       for item in result.contract_mentions):
                issues.append(f"MISSING_MONEY_EVIDENCE_{segment_id}")
    elif result.contract_mentions:
        issues.append("UNEXPECTED_CONTRACT")
    reviews = expected.get("reviews", [])
    if case_id in {"T05", "T08"}:
        reviews = [[1, 2]]
    elif case_id == "T02":
        reviews = [[1]]
    for ids in reviews:
        if not any(set(ids).issubset(item.segment_ids) for item in result.needs_review):
            issues.append("MISSING_REVIEW_EVIDENCE")
    return sorted(set(issues))


def model_identity(port):
    with httpx.Client(trust_env=False, timeout=5) as http:
        response = http.get(f"http://127.0.0.1:{port}/api/version")
        response.raise_for_status()
        version = response.json()["version"]
    with local_client(port, 5) as client:
        installed = next((item for item in client.list().models if item.model == MODEL_NAME), None)
    if installed is None or installed.details is None:
        raise ValueError("Pinned Qwen model is missing")
    quantization = installed.details.quantization_level
    if (version, installed.digest, quantization) != (OLLAMA_VERSION, MODEL_DIGEST, QUANTIZATION):
        raise ValueError("Ollama/model identity differs from the fixed evaluation settings")
    return {"ollama_version": version, "model_digest": installed.digest, "quantization": quantization}


def model_placement(port):
    try:
        with local_client(port, 5) as client:
            model = next((item for item in client.ps().models if item.model == MODEL_NAME), None)
        if model is not None:
            return model.model_dump(include={"size", "size_vram", "context_length"})
    except (ConnectionError, httpx.HTTPError):
        pass
    return None


def evaluate_case(case, *, port, mode):
    transcript = make_transcript(case, long=mode == "long")
    report = {
        "case": case["id"], "mode": mode, "expected": case["expected"],
        "input_segment_count": len(transcript.segments),
        "input_sha256": hashlib.sha256(transcript.model_dump_json().encode()).hexdigest(),
    }
    responses = []

    def observe(request, content):
        responses.append({"stage": request.stage, "content": content})

    started = time.monotonic()
    try:
        run = analyze_transcript(
            transcript, port=port, chunk_segments=SPLIT_CASES[case["id"]] if mode == "split" else None,
            observe=observe,
        )
    except AnalysisFailure as error:
        report.update(
            status="fail", error=error.code, stage=error.stage, segment_ids=error.segment_ids,
            requests=[asdict(item) for item in error.requests],
        )
    else:
        issues = score(case, run.result, long=mode == "long")
        processed = {segment_id for item in run.requests if item.stage.startswith("chunk:")
                     for segment_id in item.segment_ids}
        if processed != {segment.segment_id for segment in transcript.segments}:
            issues.append("INCOMPLETE_INPUT_COVERAGE")
        report.update(
            status="fail" if issues else "pass", issues=issues,
            processed_segment_count=len(processed),
            result=run.result.model_dump(mode="json"), requests=[asdict(item) for item in run.requests],
        )
    report["seconds"] = round(time.monotonic() - started, 3)
    report["placement"] = model_placement(port)
    report["responses"] = responses
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    catalog = load_cases()
    parser.add_argument("cases", choices=list(catalog), nargs="*")
    parser.add_argument("--port", type=int, default=11434, help="Port of a cloud-disabled Ollama on 127.0.0.1")
    parser.add_argument("--repeat", type=int, choices=(1, 3), default=1)
    parser.add_argument("--mode", choices=("all", "standard", "split", "long"), default="all")
    args = parser.parse_args(argv)
    cases = args.cases or FIRST_CASES
    variants = {"split": SPLIT_CASES, "long": LONG_CASES}
    if not 1 <= args.port <= 65535:
        parser.error("invalid loopback port")
    if args.mode in variants and any(case not in variants[args.mode] for case in cases):
        parser.error("selected case does not have the requested variant")
    try:
        identity = model_identity(args.port)
    except (ConnectionError, httpx.HTTPError, ValueError) as error:
        print(f"FAIL: evaluation preflight ({type(error).__name__})", file=sys.stderr)
        return 1
    print(json.dumps({"settings": analysis_settings() | identity}, ensure_ascii=False), flush=True)
    passed = True
    for case in cases:
        modes = [args.mode]
        if args.mode == "all":
            modes = ["standard", "split"] if case in SPLIT_CASES else ["standard"]
        for mode in modes:
            for repetition in range(1, args.repeat + 1):
                report = evaluate_case(catalog[case], port=args.port, mode=mode)
                report["repeat"] = repetition
                print(json.dumps(report, ensure_ascii=False), flush=True)
                passed &= report["status"] == "pass"
                if report.get("error") == "CANCELLED":
                    return 130
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
