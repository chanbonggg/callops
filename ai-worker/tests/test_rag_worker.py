import json
from math import hypot
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as Record
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import analyzer, worker_engine
from app.worker_io import Copies, WorkerError, json_bytes


EMBED_MODEL = 'bge-m3:567m'
EMBED_DIGEST = '7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab'
QWEN_DIGEST = '500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41'
DIMENSIONS = 1024


def response(data, *, done_reason='stop', tool_calls=None):
    return Record(done=True, done_reason=done_reason,
                  message=Record(tool_calls=tool_calls, content=json.dumps(data)))


class RagWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ollama = self.enterContext(patch.object(worker_engine, 'local_ollama'))
        self.ollama.return_value.__enter__.return_value = 11435
        self.connect = self.enterContext(patch.object(analyzer, 'local_client'))
        self.client = Mock()
        self.connect.return_value.__enter__.return_value = self.client
        self.client.list.return_value = Record(models=[
            Record(model=EMBED_MODEL, digest=EMBED_DIGEST),
            Record(model='qwen3:8b', digest=QWEN_DIGEST),
        ])
        self.client.ps.return_value = Record(models=[
            Record(model=name, size=100, size_vram=100) for name in (EMBED_MODEL, 'qwen3:8b')
        ])
        self.question = {
            'schema_version': 1, 'question': '언제 만나기로 했나요?',
            'scope': {'from_date': '2031-04-03', 'to_date': '2031-04-03'},
            'evidence': [
                {'id': 'C1:S1', 'call_id': 'C1', 'call_started_at': '2031-04-02T23:30:00Z',
                 'start': 1, 'end': 2, 'speaker': 'A', 'text': '4월 5일에 만납시다.'},
                {'id': 'C1:S2', 'call_id': 'C1', 'call_started_at': '2031-04-02T23:30:00Z',
                 'start': 2, 'end': 3, 'speaker': 'B', 'text': '네, 그날로 확정하죠.'},
            ],
        }
        self.draft = {'answer': '4월 5일로 확정됐습니다.', 'reason': None, 'segment_ids': ['C1:S1']}

    def invoke(self, stage, payload, *, success=True, failure_code=1):
        directory = self.root / str(uuid4())
        directory.mkdir(mode=0o700)
        (directory / 'input').write_bytes(json_bytes(payload))
        code = worker_engine.main([stage, str(directory), '--ollama-models', str(self.root)])
        self.assertEqual(code, 0 if success else failure_code)
        result = directory / 'result.json'
        if not success:
            self.assertFalse(result.exists())
            self.assertFalse((directory / 'result.part').exists())
            return None
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        return json.loads(result.read_bytes())

    def test_embedding_batch_is_normalized_in_input_order(self):
        self.client.embed.return_value = Record(embeddings=[
            [3, 4] + [0] * (DIMENSIONS - 2), [0, -2] + [0] * (DIMENSIONS - 2),
        ])
        texts = ['어떤 날짜인가요?', '4월 5일에 만납시다.']
        result = self.invoke('RAG_EMBED', {'schema_version': 1, 'texts': texts})
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['model_name'], EMBED_MODEL)
        self.assertEqual(result['model_digest'], EMBED_DIGEST)
        self.assertEqual(result['dimensions'], DIMENSIONS)
        vectors = result['embeddings']
        self.assertEqual([vector[:2] for vector in vectors], [[0.6, 0.8], [0, -1]])
        for vector in vectors:
            self.assertEqual(len(vector), DIMENSIONS)
            self.assertAlmostEqual(hypot(*vector), 1)
        self.client.embed.assert_called_once_with(model=EMBED_MODEL, input=texts, truncate=False, keep_alive=60)
        self.client.chat.assert_not_called()

    def test_invalid_vectors_never_publish_a_partial_batch(self):
        valid = [1] * DIMENSIONS
        for vectors in ([valid], [valid, [1, 2]], [valid, [0] * DIMENSIONS],
                        [valid, [float('nan')] + valid[1:]], [valid, [float('inf')] + valid[1:]]):
            with self.subTest(second_length=len(vectors[-1]), response_count=len(vectors)):
                self.client.embed.return_value = Record(embeddings=vectors)
                self.invoke('RAG_EMBED', {'schema_version': 1, 'texts': ['질문', '근거']}, success=False)

    def test_invalid_request_is_rejected_before_starting_ollama(self):
        for stage, payload in (
            ('RAG_EMBED', {'schema_version': True, 'texts': ['질문']}),
            ('RAG_EMBED', {'schema_version': 1, 'texts': ['  ']}),
            ('RAG_EMBED', {'schema_version': 1, 'texts': ['가' * 1500]}),
            ('RAG_EMBED', {'schema_version': 1, 'texts': ['질문'] * 17}),
            ('RAG_EMBED', {'schema_version': 1, 'texts': ['근거'], 'model': 'other'}),
            ('RAG_ANSWER', dict(self.question, evidence=[self.question['evidence'][0]] * 2)),
            ('RAG_ANSWER', dict(self.question, evidence=[dict(self.question['evidence'][0], end=0)])),
            ('RAG_ANSWER', dict(self.question, evidence=[dict(self.question['evidence'][0], path='/tmp/data')])),
        ):
            with self.subTest(stage=stage, fields=list(payload)):
                self.invoke(stage, payload, success=False)
        self.ollama.assert_not_called()

    def test_mismatched_model_is_rejected_before_inference(self):
        self.client.list.return_value = Record(models=[Record(model=EMBED_MODEL, digest='different')])
        self.invoke('RAG_EMBED', {'schema_version': 1, 'texts': ['질문']}, success=False)
        self.client.embed.assert_not_called()
        self.client.chat.assert_not_called()

    def test_cpu_fallback_does_not_publish_a_result(self):
        self.client.embed.return_value = Record(embeddings=[[1] * DIMENSIONS])
        self.client.ps.return_value = Record(models=[Record(model=EMBED_MODEL, size=100, size_vram=99)])
        self.invoke('RAG_EMBED', {'schema_version': 1, 'texts': ['질문']}, success=False)
        self.client.embed.assert_called_once()

    def test_answer_keeps_reviewed_citations_and_passes_korean_call_time(self):
        self.client.chat.side_effect = [response(self.draft), response({
            'additional_segment_ids': ['C1:S2'], 'correction': None,
        })]
        result = self.invoke('RAG_ANSWER', self.question)
        self.assertEqual(result, {
            'schema_version': 1, 'model_name': 'qwen3:8b', 'model_digest': QWEN_DIGEST,
            'prompt_version': 'rag-v10', **self.draft, 'segment_ids': ['C1:S1', 'C1:S2'],
        })
        call = self.client.chat.call_args_list[0].kwargs
        self.assertFalse(call['think'])
        self.assertNotIn('tools', call)
        prompt = json.loads(call['messages'][1]['content'])
        self.assertEqual(prompt['evidence'][0]['call_started_at'], '2031-04-03T08:30:00+09:00')
        self.assertEqual(prompt['evidence'][1]['speaker'], 'B')
        self.client.embed.assert_not_called()

    def test_review_cannot_add_an_unprovided_citation(self):
        self.client.chat.side_effect = [response(self.draft), response({
            'additional_segment_ids': ['C2:S1'], 'correction': None,
        })]
        self.invoke('RAG_ANSWER', self.question, success=False)
        self.assertEqual(self.client.chat.call_count, 2)

    def test_uncited_abstention_does_not_return_model_text(self):
        self.client.chat.side_effect = [response({
            'answer': None, 'reason': '근거 없는 고유 문구', 'segment_ids': [],
        }), response({'additional_segment_ids': [], 'correction': None})]
        result = self.invoke('RAG_ANSWER', self.question)
        self.assertEqual(result['reason'], '검색 범위에 답할 수 있는 발화가 없습니다.')
        self.assertIsNone(result['answer'])
        self.assertEqual(result['segment_ids'], [])

    def test_truncated_or_tool_call_responses_are_rejected(self):
        for bad in (response(self.draft, done_reason='length'), response(self.draft, tool_calls=[{}])):
            with self.subTest(reason=bad.done_reason, tools=bad.message.tool_calls):
                self.client.chat.return_value = bad
                self.invoke('RAG_ANSWER', self.question, success=False)

    def test_context_excess_is_rejected_before_chat(self):
        question = dict(self.question, evidence=[
            dict(source, text='가' * 1500) for source in self.question['evidence']
        ])
        self.invoke('RAG_ANSWER', question, success=False, failure_code=worker_engine.RAG_CONTEXT_EXIT)
        self.client.chat.assert_not_called()

    def test_empty_answer_runs_in_the_normal_isolated_engine_without_ollama(self):
        copies = Copies(self.root / 'worker')
        key = str(uuid4())
        payload = dict(self.question, evidence=[])
        expected = self.invoke('RAG_ANSWER', payload)
        self.ollama.assert_not_called()
        # The isolated subprocess must succeed even when Ollama is unavailable.
        with patch.dict('os.environ', {'PATH': str(self.root)}), copies.locked():
            directory = copies.work_dir(key)
            worker_engine.publish(directory / 'input', json_bytes(payload))
            engine = worker_engine.StageProcess(copies.engine_guard, None, self.root / 'no-models')
            result = engine.run({'stage': 'RAG_ANSWER'}, directory, lambda: None)
            self.assertEqual(result, expected)
            self.assertIsNone(result['answer'])
            self.assertIsNone(result['model_name'])
            self.assertEqual(result['segment_ids'], [])
            self.assertFalse(copies.engine_guard.exists())
            copies.remove(key)
        self.assertEqual(list(copies.work.iterdir()), [])

    def test_failed_engine_preserves_the_existing_stop_guard(self):
        copies = Copies(self.root / 'worker')
        directory = copies.work_dir(str(uuid4()))
        engine = worker_engine.StageProcess(copies.engine_guard, None, self.root)
        process = Mock()
        process.poll.return_value = None
        with (patch.object(worker_engine.subprocess, 'Popen', return_value=process) as launch,
              patch.object(worker_engine, 'stop_group', side_effect=WorkerError('ENGINE_STOP_UNCONFIRMED')),
              self.assertRaisesRegex(WorkerError, 'ENGINE_STOP_UNCONFIRMED')):
            engine.run({'stage': 'RAG_EMBED'}, directory, Mock(side_effect=WorkerError('STAGE_TIMEOUT')))
        self.assertTrue(copies.engine_guard.is_file())
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        with self.assertRaisesRegex(WorkerError, 'ENGINE_STOP_UNCONFIRMED'), copies.locked():
            self.fail('New work started before confirmed shutdown')

    def test_late_child_result_is_rejected_after_engine_exit(self):
        copies = Copies(self.root / 'worker')
        directory = copies.work_dir(str(uuid4()))
        (directory / 'result.json').write_bytes(b'{"schema_version":1}')
        engine = worker_engine.StageProcess(copies.engine_guard, None, self.root)
        process = Mock(returncode=0)
        process.poll.return_value = 0
        with (patch.object(worker_engine.subprocess, 'Popen', return_value=process),
              patch.object(worker_engine, 'stop_group') as stop,
              self.assertRaisesRegex(WorkerError, 'STAGE_TIMEOUT')):
            engine.run({'stage': 'RAG_EMBED'}, directory, Mock(side_effect=WorkerError('STAGE_TIMEOUT')))
        stop.assert_called_once_with(process)
        self.assertFalse(copies.engine_guard.exists())
