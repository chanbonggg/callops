import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import transcriber


class TranscriberTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio = Path(self.temp.name) / "synthetic.m4a"
        self.audio.write_bytes(b"synthetic input; decoder is mocked")
        self.output = Path(self.temp.name) / "transcript.json"
        self.factory = Mock()
        self.library = SimpleNamespace(WhisperModel=self.factory)
        self.library_patch = patch.dict(sys.modules, {"faster_whisper": self.library})
        self.library_patch.start()
        self.addCleanup(self.library_patch.stop)

    def configure(self, segments=(), duration=2):
        self.factory.return_value.transcribe.return_value = (
            iter(segments), SimpleNamespace(language="ko", duration=duration)
        )

    def test_preserves_settings_and_consumes_segments_before_recording_time(self):
        consumed = []

        def segments():
            yield SimpleNamespace(start=0, end=1, text=" 합성 대본 ")
            consumed.append(True)
            yield SimpleNamespace(start=1, end=2, text="두 번째 문장")

        self.factory.return_value.transcribe.return_value = (
            segments(), SimpleNamespace(language="ko", duration=2)
        )
        with patch.object(transcriber.time, "perf_counter", side_effect=[10, 15]):
            result = transcriber.transcribe_audio(self.audio)
        self.assertEqual(consumed, [True])
        self.assertEqual(result["processing_seconds"], 5)
        self.assertEqual(result["segments"], [
            {"segment_id": 1, "start": 0, "end": 1, "text": "합성 대본"},
            {"segment_id": 2, "start": 1, "end": 2, "text": "두 번째 문장"},
        ])
        self.factory.assert_called_once_with(
            "large-v3-turbo", device="cuda", compute_type="int8_float16", local_files_only=True
        )
        self.factory.return_value.transcribe.assert_called_once_with(
            str(self.audio), language="ko", beam_size=5, vad_filter=False
        )

    def test_missing_input_does_not_load_model(self):
        with self.assertRaises(ValueError):
            transcriber.transcribe_audio(self.audio.with_name("absent.m4a"))
        self.factory.assert_not_called()

    def test_invalid_duration_or_timestamps_are_rejected(self):
        for duration in (float("nan"), float("inf"), -1, 7201):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.configure(duration=duration)
                transcriber.transcribe_audio(self.audio)
        for start, end in ((-1, 1), (1, 0), (0, 3), (float("nan"), 1), (0, float("inf"))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.configure([SimpleNamespace(start=start, end=end, text="합성")])
                transcriber.transcribe_audio(self.audio)
        self.configure([
            SimpleNamespace(start=1, end=2, text="합성"),
            SimpleNamespace(start=0, end=1, text="합성"),
        ])
        with self.assertRaises(ValueError):
            transcriber.transcribe_audio(self.audio)

    def test_empty_output_has_warning(self):
        self.configure()
        result = transcriber.transcribe_audio(self.audio)
        self.assertEqual(result["segments"], [])
        self.assertEqual(result["warnings"], ["NO_SPEECH"])

    def test_atomic_save_preserves_existing_file_and_cleans_failed_write(self):
        transcriber.save_transcript({"segments": []}, self.output)
        self.assertEqual(json.loads(self.output.read_text()), {"segments": []})
        if os.name == "posix":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            transcriber.save_transcript({"changed": True}, self.output)
        self.assertEqual(json.loads(self.output.read_text()), {"segments": []})
        self.output.unlink()
        for operation in ("fsync", "link"):
            with self.subTest(operation=operation):
                with patch.object(transcriber.os, operation, side_effect=OSError("synthetic failure")):
                    with self.assertRaises(OSError):
                        transcriber.save_transcript({"segments": []}, self.output)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.audio.parent.iterdir()), [self.audio])

    def test_cli_writes_json_without_printing_transcript(self):
        self.configure([SimpleNamespace(start=0, end=1, text="synthetic sensitive text")])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(transcriber.main([str(self.audio), str(self.output)]), 0)
        self.assertEqual(stdout.getvalue(), "Saved transcription: segments=1, warnings=none\n")
        self.assertEqual(json.loads(self.output.read_text())["segments"][0]["segment_id"], 1)

    def test_cli_hides_decoder_errors_and_writes_nothing_on_incomplete_iteration(self):
        def broken_segments():
            yield SimpleNamespace(start=0, end=1, text="synthetic sensitive text")
            raise RuntimeError("synthetic sensitive filename")

        self.factory.return_value.transcribe.return_value = (
            broken_segments(), SimpleNamespace(language="ko", duration=2)
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(transcriber.main([str(self.audio), str(self.output)]), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "FAIL: transcription (RuntimeError); details withheld\n")

    def test_cli_rejects_output_in_repository_before_loading_model(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(transcriber.main([
                str(self.audio), str(transcriber.REPO_ROOT / "transcript.json")
            ]), 1)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
