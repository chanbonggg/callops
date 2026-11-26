import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(WORKER_ROOT.parent / "src"))

from app import analyzer
from app.evaluate_analysis import load_cases, make_transcript, score
from app.schemas import CallAnalysis, Extraction, Transcript
from callops.processing.transcript import validate_result


def transcript(*turns, call_started_at="2026-09-19T09:00:00+09:00"):
    return Transcript.model_validate_json(json.dumps({
        "call_started_at": call_started_at,
        "timezone": "Asia/Seoul",
        "segments": [
            {"segment_id": idx, "start": idx - 1, "end": idx, "speaker": speaker, "text": text}
            for idx, (speaker, text) in enumerate(turns, 1)
        ],
    }))


def event(ids, *, date="2026년 9월 22일", time="오후 3시부터 4시까지", status="confirmed"):
    return {
        "title": "테스트 프로젝트 회의", "date_text": date, "time_text": time,
        "status": status, "segment_ids": ids,
    }


def response(ids, *, events=(), tasks=(), contracts=(), reviews=(), done_reason="stop"):
    return SimpleNamespace(
        done=True, done_reason=done_reason, prompt_eval_count=100, eval_count=50,
        load_duration=1000000, total_duration=5000000,
        message=SimpleNamespace(content=json.dumps({
            "summary": {"text": "테스트 프로젝트를 논의했다.", "segment_ids": ids},
            "events": list(events), "tasks": list(tasks),
            "contract_mentions": list(contracts), "needs_review": list(reviews),
        })),
    )


T01 = ("A", "2026년 9월 22일 오후 3시부터 4시까지 테스트 프로젝트 회의를 하겠습니다.")
AGREEMENT = ("B", "네, 그 시간으로 합의합니다.")


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        client = patch.object(analyzer, "Client")
        self.client = client.start()
        self.addCleanup(client.stop)
        self.client.return_value.__enter__.return_value = self.client.return_value
        self.chat = self.client.return_value.chat

    def test_t01_sends_speaker_segments_and_returns_grounded_server_summary(self):
        self.chat.return_value = response([1, 2], events=[event([1, 2])])
        run = analyzer.analyze_transcript(transcript(T01, AGREEMENT))
        self.assertIsInstance(run.result, CallAnalysis)
        self.assertEqual(run.result.events[0].start_at.isoformat(), "2026-09-22T15:00:00+09:00")
        self.assertEqual(run.result.events[0].end_at.isoformat(), "2026-09-22T16:00:00+09:00")
        self.assertEqual(run.requests[0].segment_ids, (1, 2))
        self.chat.assert_called_once()
        request = self.chat.call_args.kwargs
        self.assertEqual(request["model"], "qwen3:8b")
        self.assertEqual(request["format"], Extraction.model_json_schema())
        self.assertEqual(request["options"]["temperature"], 0)
        self.assertEqual(request["options"]["num_ctx"], 8192)
        self.assertFalse(request["stream"])
        self.assertNotIn("tools", request)
        self.assertFalse(self.client.call_args.kwargs["trust_env"])
        sent = request["messages"][1]["content"]
        self.assertIn('"speaker":"A"', sent)
        self.assertIn('"segment_id":2', sent)
        self.assertIn(T01[1], sent)

        envelope = {
            "schema_version": 2,
            "synthetic": True,
            "job_id": "00000000-0000-0000-0000-000000000001",
            "attempt_id": "00000000-0000-0000-0000-000000000002",
            "input_artifact_id": "00000000-0000-0000-0000-000000000003",
            "input_artifact_version": 1,
            "input_sha256": "0" * 64,
            "events": [],
            "summary": run.result.summary.model_dump(),
        }
        self.assertEqual(validate_result(envelope, "ANALYSIS", 2), envelope)

    def test_empty_or_invalid_input_makes_no_model_call(self):
        with self.assertRaises(ValidationError):
            transcript()
        with self.assertRaises(ValidationError):
            transcript(("A", "   "))
        self.chat.assert_not_called()

    def test_invalid_json_schema_citations_and_completion_are_rejected(self):
        for result, code in (
            (response([1], done_reason="length"), "INCOMPLETE_RESPONSE"),
            (response([99]), "INVALID_EVIDENCE"),
            (response([1, 1]), "INVALID_EVIDENCE"),
            (response([1], events=[event([1], date="2026년 9월 24일")]), "UNGROUNDED_VALUE"),
            (response([1], events=[event([1], status="approved")]), "INVALID_JSON"),
        ):
            with self.subTest(code=code):
                self.chat.return_value = result
                with self.assertRaisesRegex(analyzer.AnalysisFailure, code):
                    analyzer.analyze_transcript(transcript(T01))
        for content in ('{"summary":', "{}", '{"approval_status":"APPROVED"}'):
            with self.subTest(content=content):
                self.chat.return_value = response([1])
                self.chat.return_value.message.content = content
                with self.assertRaisesRegex(analyzer.AnalysisFailure, "INVALID_JSON"):
                    analyzer.analyze_transcript(transcript(T01))

    def test_changes_and_cancellations_are_reconciled_after_all_chunks(self):
        for status, turn, final in (
            ("confirmed", "방금 회의는 24일로 변경합니다.", event([1, 2], date="24일")),
            ("cancelled", "방금 회의는 취소합니다.", event([1, 2], status="cancelled")),
        ):
            with self.subTest(status=status):
                local = event([2], date=final["date_text"] if status == "confirmed" else None,
                              time=None, status=status)
                self.chat.side_effect = [
                    response([1], events=[event([1])]),
                    response([2], events=[local]),
                    response([1, 2], events=[final]),
                ]
                run = analyzer.analyze_transcript(transcript(T01, ("B", turn)), chunk_segments=1)
                self.assertEqual(len(run.result.events), 1)
                actual = run.result.events[0]
                self.assertEqual(actual.segment_ids, [1, 2])
                self.assertEqual(actual.status, status)
                self.assertEqual(actual.start_at.day, 24 if status == "confirmed" else 22)
                self.assertEqual([call.segment_ids for call in run.requests], [(1,), (2,), (1, 2)])

    def test_mid_chunk_timeout_and_missing_merge_evidence_fail_whole_analysis(self):
        turns = transcript(T01, AGREEMENT)
        self.chat.side_effect = [response([1], events=[event([1])]), TimeoutError()]
        with self.assertRaisesRegex(analyzer.AnalysisFailure, "TIMEOUT") as context:
            analyzer.analyze_transcript(turns, chunk_segments=1)
        self.assertEqual(context.exception.stage, "chunk:2")
        self.assertEqual(context.exception.segment_ids, (2,))
        self.assertEqual(len(context.exception.requests), 1)
        self.assertEqual(self.chat.call_count, 2)
        self.chat.side_effect = [
            response([1], events=[event([1])]), response([2]), response([2]),
        ]
        with self.assertRaisesRegex(analyzer.AnalysisFailure, "INCOMPLETE_MERGE"):
            analyzer.analyze_transcript(turns, chunk_segments=1)

    def test_merge_reuses_identical_summary_evidence_and_retains_distinct_candidate_ids(self):
        filler = ("B", "결과 파일을 열었습니다.")
        turns = transcript(T01, filler, filler, ("B", "방금 회의는 취소합니다."))
        self.chat.side_effect = [
            response([1], events=[event([1])]),
            response([2], reviews=[{"reason": "파일 확인", "segment_ids": [2]}]),
            response([3], reviews=[{"reason": "파일 확인", "segment_ids": [3]}]),
            response([4], events=[event([4], date=None, time=None, status="cancelled")]),
            response([1, 4], events=[event([1, 4], status="cancelled")]),
        ]
        run = analyzer.analyze_transcript(turns, chunk_segments=1)
        self.assertEqual(run.requests[-1].segment_ids, (1, 2, 4))
        self.assertEqual(run.result.events[0].segment_ids, [1, 4])
        self.assertEqual([item.segment_ids for item in run.result.needs_review], [[2], [3]])

    def test_merge_keeps_an_explicit_review_omitted_from_the_model_merge(self):
        lines = ("제시 금액은 십만 원입니다.", "이십만 원이라고 들었습니다.", "금액을 다시 확인해야 합니다.")
        quotes = [{"text": line, "segment_ids": [index]} for index, line in enumerate(lines[:2], 1)]
        self.chat.side_effect = [
            response([1], contracts=quotes[:1]),
            response([2], contracts=quotes[1:]),
            response([3], reviews=[{"reason": "금액 확인 필요", "segment_ids": [3]}]),
            response([1, 2, 3], contracts=quotes, reviews=[{"reason": "금액 충돌", "segment_ids": [1, 2]}]),
        ]
        result = analyzer.analyze_transcript(transcript(*zip(("A", "B", "A"), lines, strict=True)),
                                             chunk_segments=1).result
        self.assertEqual([item.segment_ids for item in result.needs_review], [[1, 2], [3]])
        self.assertEqual(len(result.contract_mentions), 2)

    def test_chunk_cannot_cite_id_outside_its_input(self):
        self.chat.return_value = response([2])
        with self.assertRaisesRegex(analyzer.AnalysisFailure, "INVALID_EVIDENCE"):
            analyzer.analyze_transcript(transcript(T01, AGREEMENT), chunk_segments=1)
        self.chat.assert_called_once()

    def test_time_grounding_keeps_missing_invalid_and_unsupported_values_for_review(self):
        for date, time, reference, start, end in (
            ("내일", "오후 3시부터 4시까지", None, None, None),
            ("내일", "오후 3시", "2026-12-30T23:30:00Z", "2027-01-01T15:00:00+09:00", None),
            ("2026년 9월 22일", "오후 4시부터 3시까지", None, "2026-09-22T16:00:00+09:00", None),
            ("2026년 9월 22일", "오후 11시부터 오전 1시까지", None, "2026-09-22T23:00:00+09:00", None),
            ("2026년 9월 22일", "오후 11시부터 다음 날 오전 1시까지", None,
             "2026-09-22T23:00:00+09:00", "2026-09-23T01:00:00+09:00"),
            ("2026년 9월 31일", "오후 3시부터 4시까지", None, None, None),
            ("2026년 9월 22일", "종일", None, None, None),
            ("2026년 9월 22일", "매주 오후 3시부터 4시까지", None, None, None),
        ):
            with self.subTest(date=date, time=time, reference=reference):
                turns = transcript(("A", f"{date} {time} 회의를 합니다."), call_started_at=reference)
                self.chat.return_value = response([1], events=[event([1], date=date, time=time)])
                result = analyzer.analyze_transcript(turns).result
                actual = result.events[0].model_dump(mode="json")
                self.assertEqual((actual["start_at"], actual["end_at"]), (start, end))
                self.assertEqual(bool(result.needs_review), end is None)

    def test_task_owner_is_taken_from_the_cited_turn(self):
        turns = transcript(
            ("A", "제가 자료를 내일 오후 2시까지 보내겠습니다."),
            ("B", "제가 회의실을 내일 오후 3시까지 확인하겠습니다."),
            (None, "제가 회의록을 정리하겠습니다."),
        )
        self.chat.return_value = response([1, 2, 3], tasks=[
            {"title": title, "status": "confirmed", "assignee_segment_id": idx,
             "segment_ids": [idx], "deadline_text": deadline}
            for idx, (title, deadline) in enumerate((
                ("자료 전달", "내일 오후 2시까지"), ("회의실 확인", "내일 오후 3시까지"), ("회의록 정리", None),
            ), 1)
        ])
        result = analyzer.analyze_transcript(turns).result
        self.assertEqual([task.assignee for task in result.tasks], ["A", "B", None])
        self.assertEqual([task.due_at.hour if task.due_at else None for task in result.tasks], [14, 15, None])
        self.assertEqual([item.segment_ids for item in result.needs_review], [[3]])

    def test_planner_keeps_overlap_only_when_the_next_utterance_fits(self):
        turns = transcript(T01, AGREEMENT, ("A", "다음 안건으로 넘어가겠습니다."))
        prompt = analyzer.load_system_prompt()
        chunks = analyzer.plan_chunks(turns.segments, prompt, chunk_segments=2)
        self.assertEqual([[s.segment_id for s in chunk] for chunk in chunks], [[1, 2], [2, 3]])
        one = analyzer.compact_json({"segments": [turns.segments[0].model_dump(mode="json")]})
        exact = analyzer.byte_size(prompt) + analyzer.byte_size(one)
        with patch.object(analyzer, "PROMPT_BUDGET", exact):
            self.chat.return_value = response([1])
            run = analyzer.analyze_transcript(transcript(T01))
            self.assertEqual(run.requests[0].prompt_bytes, exact)
        self.chat.reset_mock()
        with patch.object(analyzer, "PROMPT_BUDGET", exact - 1):
            with self.assertRaisesRegex(analyzer.AnalysisFailure, "INPUT_TOO_LONG"):
                analyzer.analyze_transcript(transcript(T01))
        self.chat.assert_not_called()

    def test_mid_chunk_oom_and_cancellation_do_not_run_the_merge(self):
        for error, code in (
            (analyzer.ResponseError("CUDA out of memory", 500), "OOM"),
            (KeyboardInterrupt(), "CANCELLED"),
        ):
            with self.subTest(code=code):
                self.chat.reset_mock()
                self.chat.side_effect = [response([1]), error]
                with self.assertRaisesRegex(analyzer.AnalysisFailure, code):
                    analyzer.analyze_transcript(transcript(T01, AGREEMENT), chunk_segments=1)
                self.assertEqual(self.chat.call_count, 2)

    def test_excess_plan_is_rejected_before_calling_ollama(self):
        turns = transcript(T01, AGREEMENT)
        with patch.object(analyzer, "MAX_CHUNKS", 1):
            with self.assertRaisesRegex(analyzer.AnalysisFailure, "INPUT_TOO_LONG") as context:
                analyzer.analyze_transcript(turns, chunk_segments=1)
        self.assertEqual(context.exception.segment_ids, (2,))
        self.chat.assert_not_called()

    def test_finished_model_response_at_whole_analysis_deadline_is_discarded(self):
        self.chat.return_value = response([1])
        with patch.object(analyzer.time, "monotonic", side_effect=[0, 1799, 1800]):
            with self.assertRaisesRegex(analyzer.AnalysisFailure, "TIMEOUT"):
                analyzer.analyze_transcript(transcript(T01))
        self.assertEqual(self.client.call_args.kwargs["timeout"], 1)

    def test_t01_score_rejects_real_ids_that_do_not_include_the_agreement(self):
        cases = load_cases()
        self.chat.return_value = response([1, 2], events=[event([1])])
        result = analyzer.analyze_transcript(make_transcript(cases["T01"])).result
        self.assertEqual(score(cases["T01"], result), ["MISSING_EVENT_EVIDENCE"])

    def test_abbreviated_day_grammar_does_not_offer_a_composed_date(self):
        turns = transcript(T01, ("B", "방금 회의는 24일로 변경합니다."))
        schema = analyzer.extraction_schema(turns.segments)
        choices = schema["$defs"]["ExtractedEvent"]["properties"]["date_text"]
        self.assertEqual(choices, {"enum": ["2026년 9월 22일", "24일", None]})

    def test_cited_affirmative_day_replacement_cannot_leave_a_stale_date(self):
        for verb, day in (("변경합니다", 24), ("변경하지 않습니다", 22)):
            with self.subTest(verb=verb):
                self.chat.return_value = response([1, 2], events=[event([1, 2])])
                turns = transcript(T01, ("B", f"방금 회의는 22일 대신 24일 오후 3시부터 4시로 {verb}."))
                item = analyzer.analyze_transcript(turns).result.events[0]
                self.assertEqual(item.start_at.day, day)
                self.assertEqual(item.end_at.hour, 16)
                self.assertEqual(item.segment_ids, [1, 2])

    def test_confirmed_day_change_is_visible_in_a_summary_of_the_earlier_proposal(self):
        case = load_cases()["T06"]
        reply = response([1], events=[event([1, 2, 3], date="24일", time="오후 3시부터 4시")])
        payload = json.loads(reply.message.content)
        payload["summary"]["text"] = "2026년 9월 22일 오후 3시 회의를 제안했다."
        reply.message.content = json.dumps(payload)
        self.chat.return_value = reply
        result = analyzer.analyze_transcript(make_transcript(case)).result
        self.assertIn("24일", result.summary.text)
        self.assertEqual(result.summary.segment_ids, [1, 2, 3])
        self.assertEqual(score(case, result), [])

    def test_one_ordinal_appointment_turn_is_not_evidence_for_another_date(self):
        case = load_cases()["T10"]
        self.chat.return_value = response([2, 3], events=[
            event([2, 3], time="오후 3시부터 4시", status="proposed"),
            event([2, 3], date="24일", time="오후 3시부터 4시", status="proposed")
            | {"title": "두 번째 검토 회의"},
        ])
        result = analyzer.analyze_transcript(make_transcript(case)).result
        self.assertEqual([item.segment_ids for item in result.events], [[2], [2, 3]])
        self.assertEqual(score(case, result), [])

    def test_explicit_cancellation_can_join_original_and_later_evidence(self):
        original = event([1], status="proposed")
        cancelled = event([2], status="cancelled")
        self.chat.return_value = response([1, 2], events=[original, cancelled])
        turns = transcript(T01, ("B", "방금 정한 테스트 프로젝트 회의는 취소합니다."))
        run = analyzer.analyze_transcript(turns)
        self.assertEqual(len(run.result.events), 1)
        self.assertEqual(run.result.events[0].status, "cancelled")
        self.assertEqual(run.result.events[0].segment_ids, [1, 2])
        turns = transcript(T01, ("B", "방금 정한 다른 회의는 취소합니다."))
        with self.assertRaisesRegex(analyzer.AnalysisFailure, "UNGROUNDED_VALUE"):
            analyzer.analyze_transcript(turns)
