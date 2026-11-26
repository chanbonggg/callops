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

import numpy as np

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
        self.decode_audio = Mock(return_value=[])
        self.library_patch = patch.dict(sys.modules, {
            "faster_whisper": self.library,
            "faster_whisper.audio": SimpleNamespace(decode_audio=self.decode_audio),
        })
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
            str(self.audio), language="ko", beam_size=5, vad_filter=True,
            condition_on_previous_text=False, word_timestamps=True,
            initial_prompt="한국어 업무 통화 녹취록.",
        )

    def test_long_audio_keeps_absolute_offsets_without_carrying_decoder_context(self):
        self.configure([
            SimpleNamespace(start=0, end=4, text="처음 발화"),
            SimpleNamespace(start=515.82, end=528, text="마지막 발화"),
        ], duration=529.092125)
        result = transcriber.transcribe_audio(self.audio)
        self.assertFalse(self.factory.return_value.transcribe.call_args.kwargs[
            "condition_on_previous_text"
        ])
        self.assertFalse(result["condition_on_previous_text"])
        self.assertEqual(result["segments"][-1]["start"], 515.82)
        self.assertEqual(result["segments"][-1]["end"], 528)

    def test_word_alignment_avoids_decoder_padding_at_short_clip_end(self):
        def transcribe(*args, word_timestamps=False, **kwargs):
            segment = SimpleNamespace(
                start=0.0, end=1.38 if word_timestamps else 1.56, text="가상 발화"
            )
            return iter([segment]), SimpleNamespace(language="ko", duration=1.4)

        self.factory.return_value.transcribe.side_effect = transcribe
        result = transcriber.transcribe_audio(self.audio)
        self.assertTrue(result["word_timestamps"])
        self.assertEqual(result["segments"][0]["end"], 1.38)

    def test_worker_decodes_separate_voice_clips_at_original_offsets(self):
        samples = np.zeros(48000, dtype=np.float32)
        self.decode_audio.return_value = samples
        self.configure([
            SimpleNamespace(start=0.2, end=0.9, text="첫 약속"),
            SimpleNamespace(start=2.1, end=2.8, text="변경 확인"),
        ], duration=3)
        detect = Mock(return_value=[{"start": 0, "end": 16000}, {"start": 32000, "end": 48000}])
        with patch.dict(sys.modules, {"faster_whisper.vad": SimpleNamespace(
            get_speech_timestamps=detect, VadOptions=Mock(),
        )}):
            result = transcriber.transcribe_file(self.audio, model_name="large-v3", preserve_voice_intervals=True)
        self.assertIs(detect.call_args.args[0], samples)
        self.assertEqual(self.factory.return_value.transcribe.call_args.kwargs["clip_timestamps"], [0.0, 1.0, 2.0, 3.0])
        self.assertEqual([(item["start"], item["end"]) for item in result["segments"]], [(0.2, 0.9), (2.1, 2.8)])

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

    def test_invalid_segment_closes_the_model_stream(self):
        close = Mock()

        class ModelStream:
            def __iter__(self):
                yield SimpleNamespace(start=0, end=3, text="가상 발화")

        stream = ModelStream()
        stream.close = close
        self.factory.return_value.transcribe.return_value = (
            stream, SimpleNamespace(language="ko", duration=2)
        )
        with self.assertRaises(ValueError):
            transcriber.transcribe_audio(self.audio)
        close.assert_called_once_with()

    def test_silence_uses_voice_activity_detection_and_has_warning(self):
        self.configure()
        result = transcriber.transcribe_audio(self.audio)
        self.assertTrue(self.factory.return_value.transcribe.call_args.kwargs["vad_filter"])
        self.assertTrue(result["vad_filter"])
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
        model_path = Path.home() / "callops-runtime" / "d3" / "models" / "large-v3"
        self.factory.assert_called_once_with(
            str(model_path), device="cuda", compute_type="int8_float16", local_files_only=True
        )
        self.assertEqual(stdout.getvalue(), "Saved transcription: segments=1, warnings=none\n")
        self.assertEqual(json.loads(self.output.read_text())["segments"][0]["segment_id"], 1)

    def test_cli_reports_low_audio_contrast_with_selected_local_model(self):
        # Background energy remains near the speech level in this probe.
        self.decode_audio.return_value = np.repeat(np.tile([0.05, 0.06, 0.1, 0.12], 25), 320)
        self.configure([SimpleNamespace(start=0, end=1, text="가상 발화")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(transcriber.main([
                str(self.audio), str(self.output), "--model", "large-v3",
            ]), 0)

        self.factory.assert_called_once_with(
            "large-v3", device="cuda", compute_type="int8_float16", local_files_only=True
        )
        self.assertEqual(self.factory.return_value.transcribe.call_args.args[0].shape, (32000,))
        self.assertEqual(
            json.loads(self.output.read_text())["warnings"], ["LOW_AUDIO_CONTRAST"]
        )

    def test_decoder_failure_stops_before_cuda_model_load(self):
        self.decode_audio.side_effect = OSError("synthetic sensitive filename")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(transcriber.main([str(self.audio), str(self.output)]), 1)
        self.factory.assert_not_called()
        self.assertEqual(list(self.audio.parent.iterdir()), [self.audio])
        self.assertEqual(stderr.getvalue(), "FAIL: transcription (OSError); details withheld\n")

    def test_cli_flags_amr_nb_from_the_container_header(self):
        self.audio.write_bytes(b"#!AMR\nsynthetic input; decoder is mocked")
        self.configure([SimpleNamespace(start=0, end=1, text="가상 발화")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(transcriber.main([str(self.audio), str(self.output)]), 0)
        self.assertEqual(json.loads(self.output.read_text())["warnings"], ["NARROWBAND_AMR"])

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

    def test_cancellation_while_saving_discards_partial_result(self):
        self.configure([SimpleNamespace(start=0, end=1, text="가상 발화")])
        stderr = io.StringIO()
        with patch.object(transcriber.os, "fsync", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(transcriber.main([str(self.audio), str(self.output)]), 130)
        self.assertEqual(list(self.audio.parent.iterdir()), [self.audio])
        self.assertEqual(stderr.getvalue(), "CANCELLED: transcription\n")

    def test_cli_rejects_output_in_repository_before_loading_model(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(transcriber.main([
                str(self.audio), str(transcriber.REPO_ROOT / "transcript.json")
            ]), 1)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
