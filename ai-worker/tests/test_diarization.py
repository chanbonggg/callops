from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.diarization import annotate_segments


class DiarizationTests(unittest.TestCase):
    def test_labels_only_segments_with_one_covered_speaker(self):
        detected = [
            SimpleNamespace(start=1.5, end=4.1, speaker=12),
            SimpleNamespace(start=0.2, end=2.0, speaker=3),
        ]
        segments = [
            {"segment_id": 1, "start": 0.1, "end": 1.4},
            {"segment_id": 2, "start": 1.4, "end": 2.2},
            {"segment_id": 3, "start": 2.3, "end": 3.9},
            {"segment_id": 4, "start": 3.9, "end": 4.5},
        ]
        turns, assignments, warnings = annotate_segments(segments, detected, duration=4)
        self.assertEqual(turns[-1], {"start": 1.5, "end": 4, "speaker": "B"})
        self.assertEqual([row["speaker"] for row in assignments], ["A", None, "B", None])
        self.assertEqual(warnings, ["SPEECH_OVERLAP", "SPEAKER_UNCERTAIN"])

    def test_sequential_turns_within_one_segment_leave_speaker_unknown(self):
        detected = [
            SimpleNamespace(start=0.0, end=0.5, speaker=3),
            SimpleNamespace(start=0.5, end=1.0, speaker=12),
        ]
        segments = [{"segment_id": 1, "start": 0.0, "end": 1.0}]
        _, assignments, warnings = annotate_segments(segments, detected, duration=1)
        self.assertEqual(assignments, [{"segment_id": 1, "speaker": None}])
        self.assertEqual(warnings, ["SPEAKER_UNCERTAIN"])

    def test_single_detected_cluster_does_not_assign_a_two_speaker_label(self):
        detected = [SimpleNamespace(start=0.0, end=1.0, speaker=12)]
        segments = [{"segment_id": 1, "start": 0.0, "end": 1.0}]
        _, assignments, warnings = annotate_segments(segments, detected, duration=1)
        self.assertIsNone(assignments[0]["speaker"])
        self.assertEqual(warnings, ["SPEAKER_UNCERTAIN"])

    def test_invalid_intervals_are_rejected(self):
        for start, end in ((float("nan"), 1), (-0.1, 1), (0.2, 0.1)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                annotate_segments([], [SimpleNamespace(start=start, end=end, speaker=1)], 2)


if __name__ == "__main__":
    unittest.main()
