import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import evaluate_rag
from app.rag import RagPrompts


def model_response(payload):
    return SimpleNamespace(
        done=True, done_reason="stop", message=SimpleNamespace(
            tool_calls=None, content=json.dumps(payload),
        ),
    )


class RagEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.prompts = RagPrompts(answer="답변", review="근거 검토", evidence="확정 수치 판단")
        self.calls = [
            {
                "id": call_id, "counterparty": name, "call_started_at": "2031-04-02T23:30:00Z",
                "segments": [{"id": f"{call_id}-S1", "start": 0, "end": 3, "text": "합성 통화"}],
            }
            for call_id, name in (("C1", "가상상사"), ("C2", "가상공방"))
        ]
        self.filters = {"counterparty": "가상상사", "from_date": "2031-04-03", "to_date": "2031-04-03"}
        self.question = {"text": "어떤 날짜로 정했나요?", "filters": self.filters}
        self.windows = list(evaluate_rag.source_windows(self.calls, 1))

    def test_scope_uses_korean_date_and_precedes_ranking(self):
        scope = evaluate_rag.calls_in_scope(self.calls, self.filters, ZoneInfo("Asia/Seoul"))
        matches = evaluate_rag.rank_windows([1, 0], self.windows, [[0, 1], [1, 0]], scope)
        self.assertEqual(evaluate_rag.retrieved_ids(matches), {"C1-S1"})

    def test_no_evidence_abstains_without_model_call(self):
        client = Mock()
        answer = evaluate_rag.answer_question(client, self.question, [], self.prompts)
        client.chat.assert_not_called()
        self.assertIsNone(answer.answer)
        self.assertEqual(answer.segment_ids, [])

    def test_only_ranked_evidence_can_be_cited(self):
        client = Mock()
        client.chat.return_value = model_response({
            "answer": "4월 3일입니다.", "reason": None, "segment_ids": ["C2-S1"],
        })
        with self.assertRaisesRegex(ValueError, "Invalid answer evidence"):
            evaluate_rag.answer_question(client, self.question, self.windows[:1], self.prompts)
        self.assertEqual(client.chat.call_count, 2)

    def test_invalid_draft_citation_is_retried_with_the_available_ids(self):
        client = Mock()
        client.chat.side_effect = [
            model_response({"answer": "4월 3일입니다.", "reason": None, "segment_ids": ["C2-S1"]}),
            model_response({"answer": "4월 3일입니다.", "reason": None, "segment_ids": ["C1-S1"]}),
            model_response({"additional_segment_ids": [], "correction": None}),
        ]
        answer = evaluate_rag.answer_question(client, self.question, self.windows[:1], self.prompts)
        retry_schema = client.chat.call_args_list[1].kwargs["format"]
        self.assertEqual(retry_schema["properties"]["segment_ids"]["items"]["enum"], ["C1-S1"])
        self.assertEqual(answer.segment_ids, ["C1-S1"])
        self.assertEqual(client.chat.call_count, 3)

    def test_unresolved_value_blocks_answer_generation(self):
        client = Mock()
        client.chat.return_value = model_response({
            "status": "unavailable", "reason": "다른 조건에 동의했습니다.", "segment_ids": ["C1-S1"],
        })
        question = {
            **self.question, "text": "합의한 배송 수수료는 얼마인가요?",
            "expected_facts": ["채점 자료"], "required_evidence_ids": ["C1-S1"],
        }
        window = {**self.windows[0], "segments": [
            {"id": "C1-S1", "start": 0, "end": 3, "text": "배송 수수료는 아직 정하지 않았습니다."},
        ]}
        answer = evaluate_rag.answer_question(client, question, [window], self.prompts)

        self.assertIsNone(answer.answer)
        self.assertIn("배송 수수료는 아직 정하지 않았습니다.", answer.reason)
        self.assertNotIn("동의했습니다", answer.reason)
        client.chat.assert_called_once()
        payload = json.loads(client.chat.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(set(payload), {"question", "scope", "evidence"})

    def test_supported_answer_includes_agreement_evidence(self):
        client = Mock()
        window = {**self.windows[0], "segments": [
            {"id": "C1-S1", "start": 0, "end": 3, "text": "5월 4일에 만납시다."},
            {"id": "C1-S2", "start": 3, "end": 6, "text": "네, 그날로 확정하죠."},
        ]}
        client.chat.side_effect = [
            model_response({"answer": "5월 4일로 확정됐습니다.", "reason": None, "segment_ids": ["C1-S1"]}),
            model_response({"additional_segment_ids": ["C1-S2"], "correction": None}),
        ]
        answer = evaluate_rag.answer_question(client, self.question, [window], self.prompts)
        self.assertEqual(answer.answer, "5월 4일로 확정됐습니다.")
        self.assertEqual(answer.segment_ids, ["C1-S1", "C1-S2"])

    def test_pending_status_question_can_be_answered_directly(self):
        client = Mock()
        window = {**self.windows[0], "segments": [
            {"id": "C1-S1", "start": 0, "end": 3, "text": "배송 수수료는 아직 정하지 않았습니다."},
        ]}
        question = {**self.question, "text": "배송 수수료가 확정됐나요?"}
        client.chat.side_effect = [
            model_response({"answer": "아직 정하지 않았습니다.", "reason": None, "segment_ids": ["C1-S1"]}),
            model_response({"additional_segment_ids": [], "correction": None}),
        ]
        answer = evaluate_rag.answer_question(client, question, [window], self.prompts)
        self.assertEqual(answer.answer, "아직 정하지 않았습니다.")
        self.assertEqual(client.chat.call_count, 2)
