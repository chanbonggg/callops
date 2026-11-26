"""Exercise the isolated RAG engine on synthetic data with the cached GPU models."""

import argparse
from pathlib import Path
import re
import signal
import sys
import time
from uuid import uuid4

from app.worker_engine import StageProcess
from app.worker_io import Copies, WorkerError, json_bytes, publish


def run_stage(copies, engine, stage, payload):
    key = str(uuid4())
    directory = copies.work_dir(key)
    deadline = time.monotonic() + 120

    def check():
        if time.monotonic() >= deadline:
            raise WorkerError('STAGE_TIMEOUT')

    try:
        publish(directory / 'input', json_bytes({'schema_version': 1, **payload}))
        check()
        return engine.run({'stage': stage}, directory, check)
    finally:
        if not copies.engine_guard.exists():
            copies.remove(key)


def smoke(copies, engine):
    question = '몇 월 며칠 몇 시에 어디에서 만나기로 했나요?'
    evidence = [
        {'id': 'SMOKE:S1', 'call_id': 'SMOKE', 'call_started_at': '2031-04-03T09:00:00+09:00',
         'start': 0, 'end': 5, 'speaker': 'A', 'text': '4월 18일 오전 10시에 중앙회의실에서 만납시다.'},
        {'id': 'SMOKE:S2', 'call_id': 'SMOKE', 'call_started_at': '2031-04-03T09:00:00+09:00',
         'start': 5, 'end': 8, 'speaker': 'B', 'text': '네, 그 시간과 장소로 확정하겠습니다.'},
    ]
    with copies.locked():
        texts = [question, ' '.join(row['text'] for row in evidence)]
        result = run_stage(copies, engine, 'RAG_EMBED', {'texts': texts})
        if result['dimensions'] != 1024 or len(result['embeddings']) != len(texts):
            raise WorkerError('UNEXPECTED_EMBEDDINGS')
        print('RAG_EMBED_OK', flush=True)

        result = run_stage(copies, engine, 'RAG_ANSWER', {'question': question, 'evidence': evidence})
        answer = result['answer']
        expected = (r'(?<!\d)0?4(?:월\s*18일|[-./]18(?!\d))', r'오전\s*10시|(?<!\d)10:00(?!\d)', '중앙회의실')
        if (not answer or set(result['segment_ids']) != {'SMOKE:S1', 'SMOKE:S2'}
                or not all(re.search(pattern, answer) for pattern in expected)):
            raise WorkerError('UNEXPECTED_ANSWER')
        print('RAG_ANSWER_OK', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path, help='Private worker root outside the repository')
    parser.add_argument('--ollama-models', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        copies = Copies(args.root)
        smoke(copies, StageProcess(copies.engine_guard, None, args.ollama_models))
    except KeyboardInterrupt:
        return 130
    except (WorkerError, OSError, ValueError, KeyError, TypeError) as error:
        code = str(error) if isinstance(error, WorkerError) else type(error).__name__
        print(f'FAIL: {code}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main())
