"""Run the local engines in an isolated process group and normalize their output."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

from app.worker_io import MAX_RESULT, WorkerError, json_bytes, publish, sync_directory


RAG_STAGES = ('RAG_EMBED', 'RAG_ANSWER')


def live_group(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def stop_group(process, grace=30):
    for sig, wait_seconds in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
        process.poll()
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        until = time.monotonic() + wait_seconds
        while time.monotonic() < until:
            process.poll()
            if not live_group(process.pid):
                return
            time.sleep(0.1)
    raise WorkerError('ENGINE_STOP_UNCONFIRMED')


class StageProcess:
    def __init__(self, guard, model, ollama_models, speakers=None):
        self.guard = guard
        self.model = model.expanduser().resolve() if model is not None else None
        self.ollama_models = ollama_models.expanduser().resolve()
        self.speakers = tuple(path.expanduser().resolve() for path in speakers) if speakers else ()

    def ready(self):
        from ctranslate2 import get_cuda_device_count

        manifest = self.ollama_models / 'manifests/registry.ollama.ai/library/qwen3/8b'
        try:
            cuda_devices = get_cuda_device_count()
        except RuntimeError:
            raise WorkerError('MODELS_NOT_READY') from None
        if (self.model is None or not (self.model / 'model.bin').is_file()
                or any(not path.is_file() for path in self.speakers)
                or not manifest.is_file() or shutil.which('ollama') is None or cuda_devices < 1):
            raise WorkerError('MODELS_NOT_READY')

    def run(self, job, directory, check):
        stage = job['stage']
        args = [sys.executable, '-m', 'app.worker_engine', stage, str(directory),
                '--ollama-models', str(self.ollama_models)]
        if stage == 'STT' and self.model is not None:
            args.extend(('--whisper-model', str(self.model)))
            if self.speakers:
                args.extend(('--two-speaker-models', *(str(path) for path in self.speakers)))
        if stage == 'ANALYSIS' and job.get('call_time_trusted') is True and job.get('call_started_at'):
            args.extend(('--call-started-at', job['call_started_at']))
        # Give child engines only runtime paths. In particular they never get the CallOps token.
        names = ('PATH', 'LANG', 'LC_ALL', 'HOME', 'USER', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'CUDA_VISIBLE_DEVICES')
        env = {key: os.environ[key] for key in names if key in os.environ}
        python_paths = [str(Path(__file__).resolve().parents[1])]
        python_paths.extend(str(Path(path).absolute()) for path in env.get('PYTHONPATH', '').split(os.pathsep) if path)
        env.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', PYTHONPATH=os.pathsep.join(python_paths))
        publish(self.guard, b'awaiting confirmed process exit\n')
        process = None
        try:
            try:
                process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           cwd=directory, start_new_session=True)
            except OSError:
                raise WorkerError('MODEL_ERROR') from None
            while process.poll() is None:
                check()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process is not None:
                stop_group(process)
            self.guard.unlink()
            sync_directory(self.guard.parent)
        if process.returncode != 0:
            raise WorkerError('MODEL_ERROR')
        try:
            with (directory / 'result.json').open('rb') as stream:
                data = stream.read(MAX_RESULT + 1)
            if len(data) > MAX_RESULT:
                raise ValueError
            result = json.loads(data)
        except (OSError, ValueError, RecursionError):
            raise WorkerError('MODEL_ERROR') from None
        check()
        return result


def transcript_payload(result):
    labels = {item['segment_id']: item['speaker'] for item in result.get('segment_speakers', ())}
    segments = []
    for segment in result['segments']:
        if segment['text']:
            segments.append({**segment, 'segment_id': len(segments) + 1, 'speaker': labels.get(segment['segment_id'])})
    metadata = {name: result[name] for name in ('model_name', 'language', 'duration_seconds', 'processing_seconds', 'warnings')}
    return metadata | {'segments': segments}


def analysis_payload(result, *, model, prompt, elapsed):
    reviews = [item.model_dump(mode='json') for item in result.needs_review]
    events, tasks = [], []
    for event in result.events:
        if event.status == 'cancelled':
            reviews.append({'reason': f'{event.title} 일정 취소', 'segment_ids': event.segment_ids})
            continue
        times = ' '.join(value for value in (event.date_text, event.time_text) if value)
        events.append({'title': event.title, 'start': event.start_at.isoformat() if event.start_at else None,
                       'end': event.end_at.isoformat() if event.end_at and event.status == 'confirmed' else None,
                       'time_hint': times[:240], 'segment_ids': event.segment_ids})
    for task in result.tasks:
        if task.status == 'cancelled':
            reviews.append({'reason': f'{task.title} 할 일 취소', 'segment_ids': task.segment_ids})
            continue
        due_at = task.due_at.isoformat() if task.due_at and task.status == 'confirmed' else None
        tasks.append({'title': task.title, 'due_at': due_at, 'segment_ids': task.segment_ids})
    return {'model_name': model, 'prompt_version': prompt, 'processing_seconds': elapsed,
            'summary': result.summary.model_dump(mode='json'), 'events': events, 'tasks': tasks,
            'needs_review': reviews, 'contract_mentions': [item.model_dump(mode='json') for item in result.contract_mentions]}


@contextmanager
def local_ollama(models):
    import httpx

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    host = f'127.0.0.1:{port}'
    env = dict(os.environ, OLLAMA_HOST=host, OLLAMA_MODELS=str(models), OLLAMA_NO_CLOUD='1',
               OLLAMA_NOPRUNE='true', OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1',
               OLLAMA_KEEP_ALIVE='5m')
    process = subprocess.Popen(['ollama', 'serve'], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url='http://' + host, timeout=1.0, trust_env=False) as client:
            startup_until = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < startup_until:
                try:
                    if client.get('/api/tags').status_code == 200 and process.poll() is None:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('Unable to start private Ollama')
            yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def require_gpu_model(client, name):
    loaded = client.ps().models
    if not any(item.model == name and item.size and item.size_vram and item.size_vram >= item.size for item in loaded):
        raise RuntimeError('Model was not fully GPU-resident')


def run_analysis(source, call_started_at, models):
    from app.analyzer import MODEL_NAME, PROMPT_VERSION, analyze_transcript, local_client
    from app.schemas import Transcript

    payload = json.loads(source.read_bytes())
    if payload['schema_version'] != 2 or payload['synthetic'] is not False:
        raise ValueError('Analysis requires a real timestamped transcript')
    transcript = Transcript.model_validate_json(json_bytes({
        'segments': payload['segments'], 'call_started_at': call_started_at, 'timezone': 'Asia/Seoul',
    }))
    started = time.monotonic()
    with local_ollama(models) as port:
        result = analyze_transcript(transcript, port=port).result
        with local_client(port, 3) as client:
            require_gpu_model(client, MODEL_NAME)
    return analysis_payload(result, model=MODEL_NAME, prompt=PROMPT_VERSION, elapsed=time.monotonic() - started)


def run_rag(stage, source, models):
    from app import rag
    from app.analyzer import MODEL_DIGEST, MODEL_NAME, local_client

    with source.open('rb') as stream:
        raw = stream.read(rag.MAX_INPUT_BYTES + 1)
    if len(raw) > rag.MAX_INPUT_BYTES:
        raise ValueError('RAG input exceeds its size limit')
    embedding = stage == 'RAG_EMBED'
    if embedding:
        request = rag.EmbeddingRequest.model_validate_json(raw)
        name, digest = rag.EMBED_MODEL, rag.EMBED_DIGEST
        data = {'dimensions': rag.EMBED_DIMENSIONS}
    else:
        request = rag.AnswerRequest.model_validate_json(raw)
        name, digest = MODEL_NAME, MODEL_DIGEST
        data = {'prompt_version': rag.PROMPT_VERSION}
        if not request.evidence:
            return dict(data, schema_version=1, model_name=None, model_digest=None,
                        **rag.no_evidence_answer().model_dump())
    with local_ollama(models) as port, local_client(port, 60) as client:
        if not any(item.model == name and item.digest == digest for item in client.list().models):
            raise ValueError('Local model differs from the validated model')
        if embedding:
            data['embeddings'] = rag.embed_texts(client, request.texts, keep_alive=60)
        else:
            answer = rag.generate_answer(client, request.question, request.scope.model_dump(mode='json', exclude_none=True),
                                         [row.prompt_row() for row in request.evidence], rag.load_prompts())
            data.update(answer.model_dump())
        require_gpu_model(client, name)
    return dict(data, schema_version=1, model_name=name, model_digest=digest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('STT', 'ANALYSIS', *RAG_STAGES))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--whisper-model', type=Path)
    parser.add_argument('--ollama-models', type=Path, required=True)
    parser.add_argument('--two-speaker-models', nargs=2, type=Path)
    parser.add_argument('--call-started-at')
    args = parser.parse_args(argv)
    if args.stage == 'STT' and args.whisper_model is None:
        parser.error('--whisper-model is required for STT')
    try:
        source = args.directory / 'input'
        if args.stage == 'STT':
            from app.transcriber import transcribe_file

            result = transcribe_file(source, model_name=str(args.whisper_model),
                                     two_speaker_models=args.two_speaker_models, preserve_voice_intervals=True)
            data = transcript_payload(result)
        elif args.stage == 'ANALYSIS':
            data = run_analysis(source, args.call_started_at, args.ollama_models)
        else:
            data = run_rag(args.stage, source, args.ollama_models)
        publish(args.directory / 'result.json', json_bytes(data))
    except (Exception, KeyboardInterrupt):
        return 1
    return 0


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main())
