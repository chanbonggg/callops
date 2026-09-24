import io
import json
import subprocess
import tempfile
import unittest
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
