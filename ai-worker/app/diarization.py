"""Local speaker timestamps for test audio with two known speakers."""

from math import isfinite


def annotate_segments(segments, detected_turns, duration):
    """Keep diarizer time intervals and attach only unambiguous speaker labels."""
    turns = []
    labels = {}
    last_end = {"A": 0.0, "B": 0.0}
    overlap = False
    for turn in sorted(detected_turns, key=lambda item: (item.start, item.speaker)):
        if not (
            isfinite(turn.start) and isfinite(turn.end) and 0 <= turn.start < turn.end
        ):
            raise ValueError("Invalid speaker interval")
        end = min(turn.end, duration)
        if turn.start >= end:
            continue
        if turn.speaker not in labels:
            if len(labels) == 2:
                raise ValueError("Unexpected number of speaker clusters")
            labels[turn.speaker] = ("A", "B")[len(labels)]
        speaker = labels[turn.speaker]
        other = "B" if speaker == "A" else "A"
        overlap = overlap or turn.start < last_end[other]
        last_end[speaker] = max(last_end[speaker], end)
        turns.append({"start": turn.start, "end": end, "speaker": speaker})

    assignments = []
    cursor = 0
    for segment in segments:
        start, end = segment["start"], segment["end"]
        while cursor < len(turns) and turns[cursor]["end"] <= start:
            cursor += 1
        candidates = set()
        coverage = 0.0
        covered_until = start
        for index in range(cursor, len(turns)):
            turn = turns[index]
            if turn["start"] >= end:
                break
            begin = max(start, turn["start"])
            finish = min(end, turn["end"])
            if begin < finish:
                candidates.add(turn["speaker"])
                coverage += max(0.0, finish - max(begin, covered_until))
                covered_until = max(covered_until, finish)
        speaker = None
        if len(labels) == 2 and len(candidates) == 1 and coverage > (end - start) / 2:
            speaker = candidates.pop()
        assignments.append({"segment_id": segment["segment_id"], "speaker": speaker})

    warnings = []
    if overlap:
        warnings.append("SPEECH_OVERLAP")
    if any(assignment["speaker"] is None for assignment in assignments):
        warnings.append("SPEAKER_UNCERTAIN")
    return turns, assignments, warnings


def diarize_two_speakers(samples, segments, segmentation_model, embedding_model):
    import sherpa_onnx

    segmentation_model = segmentation_model.expanduser().resolve()
    embedding_model = embedding_model.expanduser().resolve()
    if not segmentation_model.is_file() or not embedding_model.is_file():
        raise ValueError("Speaker models must be existing local files")
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation_model), window_shift_ratio=0.1,
            ),
            num_threads=2,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding_model), num_threads=2,
        ),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=2),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise ValueError("Invalid speaker model configuration")
    model = sherpa_onnx.OfflineSpeakerDiarization(config)
    if model.sample_rate != 16000:
        raise ValueError("Unsupported speaker model sample rate")
    turns = model.process(samples).sort_by_start_time() if len(samples) else []
    return annotate_segments(segments, turns, len(samples) / model.sample_rate)
