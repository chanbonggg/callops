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
        self.model = model.expanduser().resolve()
        self.ollama_models = ollama_models.expanduser().resolve()
        self.speakers = tuple(path.expanduser().resolve() for path in speakers) if speakers else ()

    def ready(self):
        from ctranslate2 import get_cuda_device_count

        manifest = self.ollama_models / 'manifests/registry.ollama.ai/library/qwen3/8b'
        try:
            cuda_devices = get_cuda_device_count()
        except RuntimeError:
            raise WorkerError('MODELS_NOT_READY') from None
        if (not (self.model / 'model.bin').is_file() or any(not path.is_file() for path in self.speakers)
                or not manifest.is_file() or shutil.which('ollama') is None or cuda_devices < 1):
            raise WorkerError('MODELS_NOT_READY')

    def run(self, job, directory, check):
        args = [sys.executable, '-m', 'app.worker_engine', job['stage'], str(directory),
                '--whisper-model', str(self.model), '--ollama-models', str(self.ollama_models)]
        if self.speakers:
            args.extend(('--two-speaker-models', *(str(path) for path in self.speakers)))
        if job['stage'] == 'ANALYSIS' and job.get('call_time_trusted') is True and job.get('call_started_at'):
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
            return json.loads(data)
        except (OSError, ValueError, RecursionError):
            raise WorkerError('MODEL_ERROR') from None

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
            loaded = client.ps().models
        if not any(item.model == MODEL_NAME and item.size_vram and item.size_vram >= item.size for item in loaded):
            raise RuntimeError('Qwen was not fully GPU-resident')
    return analysis_payload(result, model=MODEL_NAME, prompt=PROMPT_VERSION, elapsed=time.monotonic() - started)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('STT', 'ANALYSIS'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--whisper-model', type=Path, required=True)
    parser.add_argument('--ollama-models', type=Path, required=True)
    parser.add_argument('--two-speaker-models', nargs=2, type=Path)
    parser.add_argument('--call-started-at')
    args = parser.parse_args()
    try:
        source = args.directory / 'input'
        if args.stage == 'STT':
            from app.transcriber import transcribe_file

            result = transcribe_file(source, model_name=str(args.whisper_model),
                                     two_speaker_models=args.two_speaker_models, preserve_voice_intervals=True)
            data = transcript_payload(result)
        else:
            data = run_analysis(source, args.call_started_at, args.ollama_models)
        publish(args.directory / 'result.json', json_bytes(data))
    except (Exception, KeyboardInterrupt):
        return 1
    return 0


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    raise SystemExit(main())
