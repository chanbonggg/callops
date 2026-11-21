import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(WORKER_ROOT.parent / "src"))
from app.schemas import CallAnalysis
from callops.processing.transcript import validate_result


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.chat = Mock()
        with patch.dict(sys.modules, {"ollama": SimpleNamespace(chat=self.chat)}):
            self.analyze = runpy.run_path(str(WORKER_ROOT / "app" / "analyzer.py"))["analyze_transcript"]

    def test_sends_transcript_and_schema_and_parses_structured_result(self):
        transcript = "[1] 내일 오후 4시에 미팅하죠.\n[2] 오늘 저녁까지 자료를 보낼게요."
        payload = {
            "summary": "미팅과 자료 공유 일정을 정했다.",
            "events": [{
                "title": "미팅",
                "date_text": "내일",
                "time_text": "오후 4시",
                "status": "confirmed",
                "evidence": {"segment_ids": [1]},
            }],
            "tasks": [{
                "title": "자료 전달",
                "deadline_text": "오늘 저녁까지",
                "status": "confirmed",
                "evidence": {"segment_ids": [2]},
            }],
        }
        self.chat.return_value.message.content = json.dumps(payload, ensure_ascii=False)

        result = self.analyze(transcript)

        self.assertIsInstance(result, CallAnalysis)
        self.assertEqual(result.model_dump(include=set(payload)), payload)
        self.chat.assert_called_once()
        request = self.chat.call_args.kwargs
        self.assertEqual(request["model"], "qwen3:8b")
        self.assertEqual(request["format"], CallAnalysis.model_json_schema())
        self.assertEqual(request["options"], {"temperature": 0, "num_ctx": 8192})
        system, user = request["messages"]
        self.assertEqual(system["role"], "system")
        prompt = (WORKER_ROOT / "prompts" / "analysis_v1.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(system["content"], prompt)
        self.assertEqual(user["role"], "user")
        self.assertIn(f"<transcript>\n{transcript}\n</transcript>", user["content"])

    def test_empty_transcript_is_rejected_before_model_call(self):
        with self.assertRaisesRegex(ValueError, "Transcript is empty"):
            self.analyze(" \n\t")
        self.chat.assert_not_called()

    def test_invalid_model_json_and_schema_are_rejected(self):
        empty_evidence = {
            "summary": "자료를 보내기로 했다.",
            "tasks": [{"title": "자료 전달", "status": "confirmed", "evidence": {"segment_ids": []}}],
        }
        for content, error_type, location in (
            ("invalid JSON", "json_invalid", ()),
            ("{}", "missing", ("summary",)),
            (json.dumps(empty_evidence), "too_short", ("tasks", 0, "evidence", "segment_ids")),
        ):
            with self.subTest(location=location):
                self.chat.return_value.message.content = content
                with self.assertRaises(ValidationError) as raised:
                    self.analyze("[1] 가상 전사")
                error = raised.exception.errors()[0]
                self.assertEqual(error["type"], error_type)
                self.assertEqual(error["loc"], location)

    def test_server_requires_a_separate_summary_with_evidence(self):
        self.chat.return_value.message.content = '{"summary": "자료를 보내기로 했다."}'
        result = self.analyze("[1] 자료를 보낼게요.")
        envelope = {
            "schema_version": 2,
            "synthetic": True,
            "job_id": "00000000-0000-0000-0000-000000000001",
            "attempt_id": "00000000-0000-0000-0000-000000000002",
            "input_artifact_id": "00000000-0000-0000-0000-000000000003",
            "input_artifact_version": 1,
            "input_sha256": "0" * 64,
            "events": [],
        }
        with self.assertRaisesRegex(ValueError, "Invalid summary"):
            validate_result({**envelope, "summary": result.summary}, "ANALYSIS", 2)
        with self.assertRaisesRegex(ValueError, "Invalid summary"):
            validate_result({**envelope, "summary": {"text": result.summary}}, "ANALYSIS", 2)
        payload = {**envelope, "summary": {"text": result.summary, "segment_ids": [1]}}
        self.assertEqual(validate_result(payload, "ANALYSIS", 2), payload)
