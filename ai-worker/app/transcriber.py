"""Standalone GPU transcription for explicitly selected development audio."""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import time

MODEL_NAME = "large-v3-turbo"
REPO_ROOT = Path(__file__).resolve().parents[2]


def transcribe_audio(audio_path: Path) -> dict:
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise ValueError("Input must be an existing audio file")

    # Offline tests can import this module without loading CUDA or a model.
    from faster_whisper import WhisperModel

    logging.getLogger("faster_whisper").setLevel(logging.CRITICAL)
    started = time.perf_counter()
    model = WhisperModel(
        MODEL_NAME, device="cuda", compute_type="int8_float16", local_files_only=True
    )
    segments, info = model.transcribe(
        str(audio_path), language="ko", beam_size=5, vad_filter=False
    )
    if not math.isfinite(info.duration) or not 0 <= info.duration <= 7200:
        raise ValueError("Invalid or unsupported audio duration")
    result_segments = []
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
    return {
        "schema_version": 1, "model_name": MODEL_NAME,
        "device": "cuda", "compute_type": "int8_float16",
        "beam_size": 5, "vad_filter": False,
        "language": info.language, "duration_seconds": info.duration,
        "processing_seconds": time.perf_counter() - started,
        "segments": result_segments,
        "warnings": [] if any(s["text"] for s in result_segments) else ["NO_SPEECH"],
    }


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="Explicitly selected test audio")
    parser.add_argument("output", type=Path, help="New JSON file outside the Git checkout")
    args = parser.parse_args(argv)
    try:
        output = args.output.expanduser().resolve()
        if output.is_relative_to(REPO_ROOT) or output.exists() or not output.parent.is_dir():
            raise ValueError("Output must be a new file in an existing directory outside Git")
        result = transcribe_audio(args.audio.expanduser().resolve())
        save_transcript(result, output)
    except Exception as error:
        # Decoder errors can contain filenames or transcript content.
        print(f"FAIL: transcription ({type(error).__name__}); details withheld", file=sys.stderr)
        return 1
    print(f"Saved transcription: segments={len(result['segments'])}, "
          f"warnings={','.join(result['warnings']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
