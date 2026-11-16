import io
import json
import subprocess
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.check import ROOT, forbidden, markdown_errors, repository_checks, run_group, run_suite


class HarnessTests(unittest.TestCase):
    def test_private_files_are_rejected_but_examples_and_sources_are_allowed(self):
        for name in (".env", "a/.env.prod", "x.WAV", "spool/a.json", "data/a.txt", "x.sqlite3", "x.key"):
            with self.subTest(name=name):
                self.assertTrue(forbidden(Path(name)))
        for name in (".env.example", "tests/fixtures/scenarios.json", "src/callops/config.py"):
            self.assertFalse(forbidden(Path(name)))

    def test_links_fences_and_escaped_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "some file.md").write_text("ok", encoding="utf-8")
            doc = root / "README.md"
            doc.write_text("[ok](some%20file.md#section)\n```text\n[x](absent)\n```\n", encoding="utf-8")
            self.assertEqual(markdown_errors(doc, root), [])
            doc.write_text("[bad](missing.md)\n```text\n", encoding="utf-8")
            self.assertEqual(len(markdown_errors(doc, root)), 2)

    def test_empty_skipped_and_expected_failure_suites_cannot_pass(self):
        with patch("builtins.print"):
            self.assertFalse(run_suite(unittest.TestSuite(), io.StringIO()))

        class Skipped(unittest.TestCase):
            @unittest.skip("missing environment")
            def test_missing(self):
                pass

        class ExpectedFailure(unittest.TestCase):
            @unittest.expectedFailure
            def test_broken(self):
                self.fail("not implemented")

        for case in (Skipped, ExpectedFailure):
            self.assertFalse(run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(case), io.StringIO()))

    def test_missing_phase_is_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch("builtins.print"):
            self.assertFalse(run_group(Path(temp), "p1"))

    def test_git_inventory_catches_force_added_ignored_data_without_printing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
            (root / ".gitignore").write_text("*.wav\n", encoding="utf-8")
            audio = root / "synthetic.wav"
            audio.write_bytes(b"synthetic fixture, not real audio")
            with patch("builtins.print") as output:
                self.assertTrue(repository_checks(root))
                subprocess.run(["git", "add", "--force", "synthetic.wav"], cwd=root, check=True, capture_output=True)
                self.assertFalse(repository_checks(root))
                self.assertNotIn("synthetic.wav", str(output.call_args_list))
                self.assertNotIn("not real audio", str(output.call_args_list))

    def test_fixture_catalog_matches_plan_and_never_requests_external_calls(self):
        data = json.loads((ROOT / "tests/fixtures/scenarios.json").read_text(encoding="utf-8"))
        self.assertTrue(data["synthetic"])
        cases = data["cases"]
        self.assertEqual({case["id"] for case in cases}, {f"T{i:02d}" for i in range(1, 11)} | {"T01-copy"})
        self.assertEqual(len(cases), 11)
        for case in cases:
            self.assertEqual(case["expected"]["external_calls_before_approval"], 0)
            self.assertTrue(case["input"])
        private = next(case for case in cases if case["id"] == "T04")
        self.assertEqual(private["expected"]["ai_calls"], 0)

    def test_rag_fixtures_have_valid_scoped_evidence(self):
        seen_ids = set()
        seen_texts = set()
        for split in ('development', 'final'):
            with self.subTest(split=split):
                data = json.loads((ROOT / f"tests/fixtures/rag-{split}.json").read_text(encoding="utf-8"))
                self.assertEqual(data['split'], split)
                self.check_rag_fixture(data)
                segments = [s for call in data['calls'] for s in call['segments']]
                ids = {row['id'] for row in data['calls'] + data['questions'] + segments}
                texts = {row['text'].strip() for row in data['questions'] + segments}
                self.assertTrue(seen_ids.isdisjoint(ids))
                self.assertTrue(seen_texts.isdisjoint(texts))
                seen_ids.update(ids)
                seen_texts.update(texts)

    def check_rag_fixture(self, data):
        self.assertIs(data['synthetic'], True)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['timezone'], 'Asia/Seoul')
        self.assertEqual(len(data['calls']), 10)
        self.assertEqual(len(data['questions']), 20)
        self.assertEqual(len({call['id'] for call in data['calls']}), 10)
        self.assertEqual(len({q['id'] for q in data['questions']}), 20)
        self.assertEqual(Counter(q['category'] for q in data['questions']),
                         {'명확한 합의': 7, '상대·기간 필터': 4, '정정·취소': 4,
                          '여러 통화 결합': 2, '근거 부족·모호함': 3})
        korea = timezone(timedelta(hours=9))
        segments = {}
        for call in data['calls']:
            started_at = datetime.fromisoformat(call['call_started_at'])
            self.assertIsNotNone(started_at.utcoffset())
            call_date = started_at.astimezone(korea).date()
            previous_end = 0
            self.assertTrue(call['segments'])
            for segment in call['segments']:
                self.assertTrue(segment['text'].strip())
                self.assertLessEqual(previous_end, segment['start'])
                self.assertLess(segment['start'], segment['end'])
                previous_end = segment['end']
                segments[segment['id']] = (call, call_date)
        self.assertEqual(len(segments), sum(len(call['segments']) for call in data['calls']))
        for question in data['questions']:
            self.assertEqual(bool(question['expected_facts']), question['answerable'])
            self.assertEqual(bool(question['abstention_reason']), not question['answerable'])
            evidence = question['required_evidence_ids']
            self.assertEqual(len(evidence), len(set(evidence)))
            if question['answerable']:
                self.assertTrue(evidence)
            filters = question['filters']
            from_date = date.fromisoformat(filters['from_date'])
            to_date = date.fromisoformat(filters['to_date'])
            self.assertLessEqual(from_date, to_date)
            cited_calls = set()
            for segment_id in evidence:
                call, call_date = segments[segment_id]
                self.assertEqual(call['counterparty'], filters['counterparty'])
                self.assertLessEqual(from_date, call_date)
                self.assertLessEqual(call_date, to_date)
                cited_calls.add(call['id'])
            if question['category'] == '여러 통화 결합':
                self.assertGreaterEqual(len(cited_calls), 2)
