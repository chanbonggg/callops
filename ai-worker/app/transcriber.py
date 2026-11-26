"""Standalone GPU transcription for explicitly selected development audio."""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

MODEL_NAME = "large-v3-turbo"
VAD_FILTER = True
CONDITION_ON_PREVIOUS_TEXT = False
INITIAL_PROMPT = "한국어 업무 통화 녹취록."
SAMPLE_RATE = 16000
REPO_ROOT = Path(__file__).resolve().parents[2]


def transcribe_audio(audio_path: Path, *, model_name: str = MODEL_NAME, samples=None, voice_clips=None) -> dict:
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise ValueError("Input must be an existing audio file")

    # Offline tests can import this module without loading CUDA or a model.
    from faster_whisper import WhisperModel

    logging.getLogger("faster_whisper").setLevel(logging.CRITICAL)
    started = time.perf_counter()
    model = WhisperModel(
        model_name, device="cuda", compute_type="int8_float16", local_files_only=True
    )
    clip_options = {"clip_timestamps": voice_clips} if voice_clips else {}
    segments, info = model.transcribe(
        str(audio_path) if samples is None else samples,
        language="ko", beam_size=5, vad_filter=VAD_FILTER,
        condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
        word_timestamps=True,
        initial_prompt=INITIAL_PROMPT,
        **clip_options,
    )
    result_segments = []
    try:
        if not math.isfinite(info.duration) or not 0 <= info.duration <= 7200:
            raise ValueError("Invalid or unsupported audio duration")
        previous_start = 0
        for number, segment in enumerate(segments, start=1):
            if (not math.isfinite(segment.start) or not math.isfinite(segment.end)
                    or not previous_start <= segment.start <= segment.end <= info.duration
                    or not isinstance(segment.text, str)):
                raise ValueError("Invalid transcript segment")
            result_segments.append({
                "segment_id": number, "start": segment.start, "end": segment.end,
                "text": segment.text.strip(),
            })
            previous_start = segment.start
    finally:
        close = getattr(segments, "close", None)
        if close is not None:
            close()
    return {
        "schema_version": 1, "model_name": Path(model_name).name,
        "device": "cuda", "compute_type": "int8_float16",
        "beam_size": 5, "vad_filter": VAD_FILTER,
        "condition_on_previous_text": CONDITION_ON_PREVIOUS_TEXT,
        "word_timestamps": True,
        "initial_prompt": INITIAL_PROMPT,
        "language": info.language, "duration_seconds": info.duration,
        "processing_seconds": time.perf_counter() - started,
        "segments": result_segments,
        "warnings": [] if any(s["text"] for s in result_segments) else ["NO_SPEECH"],
    }


def _low_audio_contrast(samples) -> bool:
    frame_size = SAMPLE_RATE // 50
    frame_count = len(samples) // frame_size
    if frame_count < 25:
        return False
    import numpy as np

    frames = samples[:frame_count * frame_size].reshape(frame_count, frame_size)
    power = np.einsum("ij,ij->i", frames, frames) / frame_size
    quiet, loud = np.percentile(power, (20, 90))
    # A contrast of 15 dB or less flags material for review.
    return bool(loud > 0 and quiet >= loud * 10 ** (-15 / 10))


def save_transcript(result: dict, output_path: Path) -> None:
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    # Publish a complete private file atomically, without overwriting an existing result.
    stream = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent,
        prefix=".transcript-", suffix=".part", delete=False,
    )
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output_path)
    finally:
        temporary.unlink()


def transcribe_file(audio_path, *, model_name, two_speaker_models=None, preserve_voice_intervals=False):
    from faster_whisper.audio import decode_audio

    started = time.perf_counter()
    samples = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
    if len(samples) > 7200 * SAMPLE_RATE:
        raise ValueError("Invalid or unsupported audio duration")
    voice_clips = None
    if preserve_voice_intervals:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        speech = get_speech_timestamps(samples, VadOptions())
        # Decode separate voiced passages at their original offsets. Concatenating
        # them before decoding can skip the earlier proposal in a correction.
        voice_clips = [interval[edge] / SAMPLE_RATE for interval in speech for edge in ('start', 'end')]
    result = transcribe_audio(audio_path, model_name=model_name, samples=samples, voice_clips=voice_clips)
    if result["warnings"] != ["NO_SPEECH"]:
        if _low_audio_contrast(samples):
            result["warnings"].append("LOW_AUDIO_CONTRAST")
        with audio_path.open("rb") as stream:
            if stream.read(6) == b"#!AMR\n":
                result["warnings"].append("NARROWBAND_AMR")
    if two_speaker_models:
        from app.diarization import diarize_two_speakers

        turns, assignments, warnings = diarize_two_speakers(samples, result["segments"], *two_speaker_models)
        result["speaker_turns"] = turns
        result["segment_speakers"] = assignments
        result["warnings"].extend(warnings)
    result["processing_seconds"] = time.perf_counter() - started
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="Explicitly selected test audio")
    parser.add_argument("output", type=Path, help="New JSON file outside the Git checkout")
    parser.add_argument(
        "--model",
        default=str(Path.home() / "callops-runtime" / "d3" / "models" / "large-v3"),
        help="Cached model name or local model directory (default: D3 large-v3)",
    )
    parser.add_argument(
        "--two-speaker-models", nargs=2, type=Path, metavar=("SEGMENTATION", "EMBEDDING"),
        help="Local speaker models for audio known to have two speakers",
    )
    try:
        args = parser.parse_args(argv)
        output = args.output.expanduser().resolve()
        if output.is_relative_to(REPO_ROOT) or output.exists() or not output.parent.is_dir():
            raise ValueError("Output must be a new file in an existing directory outside Git")
        audio_path = args.audio.expanduser().resolve()
        result = transcribe_file(audio_path, model_name=args.model, two_speaker_models=args.two_speaker_models)
        save_transcript(result, output)
    except KeyboardInterrupt:
        print("CANCELLED: transcription", file=sys.stderr)
        return 130
    except Exception as error:
        # Decoder errors can contain filenames or transcript content.
        print(f"FAIL: transcription ({type(error).__name__}); details withheld", file=sys.stderr)
        return 1
    print(f"Saved transcription: segments={len(result['segments'])}, "
          f"warnings={','.join(result['warnings']) or 'none'}")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main())
